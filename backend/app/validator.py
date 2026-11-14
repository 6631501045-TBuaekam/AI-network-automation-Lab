"""Pre-approval validation: addressing, subnets, interface mapping, OSPF, management, safety."""
import ipaddress
import re
import time

from .generator import PHYSICAL_RE
from .gns3 import canonical_ifname

IP = r"(?:\d{1,3}\.){3}\d{1,3}"
IFN = r"(?:GigabitEthernet|FastEthernet|Ethernet|TenGigabitEthernet)\d+(?:/\d+){0,3}"
ALLOWED = [re.compile(p) for p in (
    r"^hostname [A-Za-z0-9][A-Za-z0-9-]{0,62}$",
    rf"^interface {IFN}$",
    r"^no router ospf \d{1,5}$",          # clean-up of a previous plan (OSPF only)
    rf"^default interface {IFN}$",        # clean-up of a data-plane interface (never mgmt)
    r"^ description [A-Za-z0-9 _./:,-]{1,200}$",
    rf"^ ip address {IP} {IP}$",
    r"^ ip ospf network point-to-point$",
    r"^ no shutdown$",
    r"^ exit$",
    r"^router ospf \d{1,5}$",
    rf"^ router-id {IP}$",
    rf"^ network {IP} {IP} area \d+$",
    rf"^ passive-interface {IFN}$",
    r"^end$",
)]

CATEGORIES = ["addressing", "subnet", "interface_mapping", "ospf", "management", "safety", "ai_ambiguity"]
LAB_CATEGORY = "gns3_lab"  # shown only when the GNS3 cross-check is enabled


def validate(model: dict, issues: list, interface_map: dict, mapping_confirmed: bool,
             configs: dict, discoveries: dict, inventory: dict, discovery_max_age_min: int,
             lab: dict | None = None) -> dict:
    found = list(issues)

    def add(cat, sev, msg):
        found.append({"category": cat, "severity": sev, "message": msg})

    routers = model.get("routers", {})
    if not routers:
        add("addressing", "error", "no routers found in topology")
    if not model.get("hosts"):
        add("addressing", "warning", "no end hosts found in topology")

    # ---- management reachability (proved by a recent successful read-only SSH discovery)
    mgmt_nets = {}
    for rid, r in routers.items():
        inv = r.get("inventory_id")
        if not inv:
            continue
        d = discoveries.get(inv)
        if not d:
            add("management", "error", f"{rid}: no successful SSH discovery - management reachability unproven")
            continue
        age_min = (time.time() - d.get("_finished_at", 0)) / 60
        if age_min > discovery_max_age_min:
            add("management", "error", f"{rid}: last discovery is {age_min:.0f} min old - run discovery again")
        if not d.get("mgmt_interface"):
            add("management", "error", f"{rid}: management interface could not be identified")
        if d.get("mgmt_network"):
            try:
                mgmt_nets[rid] = ipaddress.ip_network(d["mgmt_network"], strict=False)
            except ValueError:
                pass
    for rid, mnet in mgmt_nets.items():
        for n in model.get("networks", {}):
            if ipaddress.ip_network(n).overlaps(mnet):
                add("management", "error", f"data network {n} overlaps management network {mnet} on {rid}")

    # ---- interface mapping against the real (discovered) interface names
    for rid, r in routers.items():
        inv = r.get("inventory_id")
        rmap = (interface_map or {}).get(rid) or {}
        disc = discoveries.get(inv) if inv else None
        names = {i["name"] for i in (disc or {}).get("interfaces", [])}
        mgmt = (disc or {}).get("mgmt_interface")
        used = {}
        for itf in r["interfaces"]:
            name = rmap.get(itf["network"])
            if not name:
                add("interface_mapping", "error", f"{rid}: no interface mapped for {itf['network']}")
                continue
            if not PHYSICAL_RE.match(name):
                add("interface_mapping", "error", f"{rid}: '{name}' is not a physical Ethernet interface")
            if disc and name not in names:
                add("interface_mapping", "error", f"{rid}: {name} does not exist on the router (discovered: {', '.join(sorted(names))})")
            if mgmt and name == mgmt:
                add("interface_mapping", "error", f"{rid}: {name} is the MANAGEMENT interface - refusing to reconfigure it")
            if name in used:
                add("interface_mapping", "error", f"{rid}: {name} mapped to both {used[name]} and {itf['network']}")
            used[name] = itf["network"]
    if routers and not mapping_confirmed:
        add("interface_mapping", "error", "interface mapping has not been confirmed by the administrator")

    # ---- OSPF settings
    routing = model.get("routing", {})
    try:
        pid = int(routing.get("process_id"))
        if not 1 <= pid <= 65535:
            add("ospf", "error", f"OSPF process id {pid} out of range")
    except (TypeError, ValueError):
        pass
    if routing.get("area") not in (None, "") and str(routing.get("area")) not in ("0", "0.0.0.0"):
        add("ospf", "warning", f"area {routing.get('area')} - the requirement is Area 0 (backbone)")
    transit = [n for n, v in model.get("networks", {}).items() if v["role"] == "transit"]
    lans = [n for n, v in model.get("networks", {}).items() if v["role"] == "lan"]
    if len(routers) > 1 and not transit:
        add("ospf", "error", "no router-to-router link: OSPF adjacency is impossible")
    if not routing.get("passive_lan_interfaces", True):
        add("ospf", "warning", "LAN interfaces will not be passive")
    for dev, cfg in (configs or {}).items():
        expected = {ipaddress.ip_network(i["network"]) for i in routers[cfg["router"]]["interfaces"]}
        adv_nets = {ipaddress.ip_network(f"{n['address']}/{_wc_to_prefix(n['wildcard'])}") for n in cfg["ospf"]["networks"]}
        if adv_nets != expected:
            add("ospf", "error", f"{dev}: advertised networks {sorted(map(str, adv_nets))} != connected {sorted(map(str, expected))}")
        lan_names = {i["name"] for i in cfg["interfaces"] if i["role"] == "lan"}
        if routing.get("passive_lan_interfaces", True) and set(cfg["ospf"]["passive"]) != lan_names:
            add("ospf", "error", f"{dev}: passive interfaces do not match LAN interfaces")
        if any(i["name"] in cfg["ospf"]["passive"] for i in cfg["interfaces"] if i["role"] == "transit"):
            add("ospf", "error", f"{dev}: a router-to-router interface is passive - adjacency would fail")
    all_adv = set()
    for cfg in (configs or {}).values():
        all_adv |= {f"{n['address']}/{_wc_to_prefix(n['wildcard'])}" for n in cfg["ospf"]["networks"]}
    for n in lans + transit:
        if configs and n not in all_adv:
            add("ospf", "error", f"network {n} is not advertised by any router")

    # ---- safety: command allow-list, no static routes, never touch management
    for dev, cfg in (configs or {}).items():
        mgmt = (discoveries.get(dev) or {}).get("mgmt_interface")
        for ln in cfg["lines"]:
            if not any(p.match(ln) for p in ALLOWED):
                add("safety", "error", f"{dev}: command not in allow-list: '{ln}'")
            if re.match(r"^\s*ip route", ln):
                add("safety", "error", f"{dev}: static route present - OSPF only")
            if mgmt and re.search(rf"\b{re.escape(mgmt)}$", ln):
                add("safety", "error", f"{dev}: config references management interface {mgmt}")
    for rid, r in routers.items():
        if r.get("inventory_id") and r.get("inventory_id") not in (configs or {}):
            add("safety", "error", f"{rid}: no configuration could be generated (fix errors above)")

    # GNS3 lab cross-check: warnings only. It can flag a cable mapped to the wrong port, but
    # the admin decides (mapping confirmation + approval).
    if lab is not None:
        for net_map_rid, ports in (lab.get("ports") or {}).items():
            for net, port in ports.items():
                mapped = ((interface_map or {}).get(net_map_rid) or {}).get(net)
                if mapped and canonical_ifname(mapped) != canonical_ifname(port):
                    add(LAB_CATEGORY, "warning", f"{net_map_rid} {net} mapped to {mapped}, "
                                                 f"but the GNS3 cable for it is on {port}")

    errors = [f for f in found if f["severity"] == "error"]
    warnings = [f for f in found if f["severity"] == "warning"]
    checks = []
    for cat in CATEGORIES + ([LAB_CATEGORY] if lab is not None else []):
        e = [f["message"] for f in errors if f["category"] == cat]
        w = [f["message"] for f in warnings if f["category"] == cat]
        checks.append({"name": cat, "status": "fail" if e else ("warn" if w else "pass"),
                       "errors": e, "warnings": w})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks,
            "checked_at": time.time()}


def _wc_to_prefix(wc: str) -> int:
    inv = int(ipaddress.IPv4Address(wc))
    return 32 - inv.bit_length()
