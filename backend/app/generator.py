"""Interface mapping suggestions, Cisco IOS config rendering and verification expectations."""
import hashlib
import ipaddress
import os
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .inventory import _norm
from .gns3 import canonical_ifname

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), trim_blocks=True, lstrip_blocks=True,
                   undefined=StrictUndefined, keep_trailing_newline=True)

PHYSICAL_RE = re.compile(r"^(GigabitEthernet|FastEthernet|Ethernet|TenGigabitEthernet)\d+(/\d+){0,3}$")


def natural_key(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def wildcard(net: ipaddress.IPv4Network) -> str:
    return str(ipaddress.IPv4Address(int(net.hostmask)))


def _safe_desc(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _./:-]", "", text)[:80].strip() or "link"


def data_interfaces(discovery: dict) -> list[str]:
    mgmt = discovery.get("mgmt_interface")
    names = [i["name"] for i in discovery.get("interfaces", [])
             if PHYSICAL_RE.match(i["name"]) and i["name"] != mgmt]
    return sorted(names, key=natural_key)


def suggest_mapping(model: dict, discoveries: dict, inventory: dict,
                    lab_ports: dict | None = None) -> tuple[dict, list]:
    """Return ({router: {network: ifname}}, notes). Nothing is assumed silently: every
    suggestion is shown to the admin, who must confirm the mapping before approval.

    Evidence, strongest first: inventory hint, GNS3 lab cable (lab_ports), CDP neighbour,
    ordered guess. A name is only used if SSH discovery found it on the router."""
    mapping: dict = {}
    notes: list = []
    for rid, r in model["routers"].items():
        inv_id = r.get("inventory_id")
        disc = discoveries.get(inv_id) if inv_id else None
        if not disc:
            notes.append(f"{rid}: no interface discovery yet - run discovery")
            continue
        free = data_interfaces(disc)
        chosen: dict = {}
        hints = (inventory.get(inv_id) or {}).get("interface_hints") or {}
        for itf in r["interfaces"]:
            h = hints.get(itf["network"])
            if h and h in free:
                chosen[itf["network"]] = h
                free.remove(h)
                notes.append(f"{rid} {itf['network']} -> {h} (inventory hint)")
        # GNS3 lab cabling (read-only cross-check): match the lab port to a discovered name
        for net, port in ((lab_ports or {}).get(rid) or {}).items():
            if net in chosen:
                continue
            match = next((n for n in free if canonical_ifname(n) == canonical_ifname(port)), None)
            if match:
                chosen[net] = match
                free.remove(match)
                notes.append(f"{rid} {net} -> {match} (GNS3 lab cable)")
            else:
                notes.append(f"{rid} {net}: GNS3 port {port} not among the discovered free "
                             f"interfaces - not used")
        # CDP neighbours identify the router-to-router link when interfaces are already up
        for itf in r["interfaces"]:
            if itf["network"] in chosen or itf["role"] != "transit":
                continue
            peers = set()
            for p in itf["peer_routers"]:
                pr = model["routers"].get(p, {})
                peers |= {_norm(p), _norm(pr.get("label") or ""), _norm(pr.get("inventory_id") or "")}
            for c in disc.get("cdp", []):
                dev = _norm(str(c.get("device", "")).split(".")[0])
                if dev in peers and c.get("local_interface") in free:
                    chosen[itf["network"]] = c["local_interface"]
                    free.remove(c["local_interface"])
                    notes.append(f"{rid} {itf['network']} -> {c['local_interface']} (CDP neighbour {c['device']})")
                    break
        # remaining: LANs first, then transit links, in interface order
        rest = sorted((i for i in r["interfaces"] if i["network"] not in chosen),
                      key=lambda i: (i["role"] != "lan", ipaddress.ip_network(i["network"])))
        for itf in rest:
            if not free:
                notes.append(f"{rid}: not enough free data interfaces for {itf['network']}")
                break
            name = free.pop(0)
            chosen[itf["network"]] = name
            notes.append(f"{rid} {itf['network']} -> {name} (ordered suggestion - please confirm cabling)")
        mapping[rid] = chosen
    return mapping, notes


def cleanup_plan(model: dict, rid: str, if_map: dict, discovery: dict | None) -> dict:
    """What the config removes before applying the new one, so every run starts from a clean
    data plane (no leftovers from an earlier plan or a different topology):
    * `no router ospf N` for the plan's process and every OSPF process discovery found
      (IOS accepts it silently when the process does not exist);
    * `default interface X` for every discovered data-plane interface plus the mapped ones.
    The management interface is never included (it is excluded from data_interfaces, and
    the validator refuses any line that names it)."""
    pids = {int(model["routing"]["process_id"])}
    names = set(if_map.values())
    if discovery:
        pids |= {int(p) for p in discovery.get("ospf_processes") or []}
        names |= set(data_interfaces(discovery))
    return {"ospf_processes": sorted(pids), "interfaces": sorted(names, key=natural_key)}


def render_router(model: dict, rid: str, if_map: dict, plan_id: str, p2p_transit: bool,
                  discovery: dict | None = None) -> dict:
    r = model["routers"][rid]
    routing = model["routing"]
    area = int(routing["area"])
    interfaces, networks, passive = [], [], []
    for itf in sorted(r["interfaces"], key=lambda i: natural_key(if_map.get(i["network"], "~"))):
        name = if_map[itf["network"]]
        net = ipaddress.ip_network(itf["network"])
        if itf["role"] == "transit":
            desc = "Link to " + ",".join(itf["peer_routers"])
        else:
            desc = "LAN " + (",".join(itf["hosts"]) or itf["network"])
        interfaces.append({"name": name, "ip": itf["ip"], "netmask": itf["netmask"],
                           "description": _safe_desc(desc),
                           "p2p": p2p_transit and itf["role"] == "transit" and len(itf["peer_routers"]) == 1,
                           "role": itf["role"]})
        networks.append({"address": str(net.network_address), "wildcard": wildcard(net)})
        if itf["role"] == "lan" and routing.get("passive_lan_interfaces", True):
            passive.append(name)
    ctx = {"plan_id": plan_id, "device_id": r["inventory_id"], "hostname": re.sub(r"[^A-Za-z0-9-]", "", rid) or "Router",
           "interfaces": interfaces,
           "cleanup": cleanup_plan(model, rid, if_map, discovery),
           "ospf": {"process_id": int(routing["process_id"]), "router_id": r["router_id"],
                    "area": area, "networks": networks, "passive": passive}}
    text = _env.get_template("cisco_ios_ospf.j2").render(**ctx)
    lines = [ln.rstrip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("!")]
    deploy_text = "\n".join(lines) + "\n"
    return {"device_id": r["inventory_id"], "router": rid, "hostname": ctx["hostname"],
            "text": text, "lines": lines,
            "sha256": hashlib.sha256(deploy_text.encode()).hexdigest(),
            "interfaces": interfaces, "ospf": ctx["ospf"], "cleanup": ctx["cleanup"],
            "expected": expected_state(model, rid, if_map)}


def expected_state(model: dict, rid: str, if_map: dict) -> dict:
    r = model["routers"][rid]
    connected = {i["network"] for i in r["interfaces"]}
    neighbors = sorted({model["routers"][p]["router_id"] for i in r["interfaces"]
                        if i["role"] == "transit" for p in i["peer_routers"]
                        if model["routers"].get(p, {}).get("router_id")})
    remote = sorted(n for n, v in model["networks"].items()
                    if n not in connected and v["role"] in ("lan", "transit"))
    lan_src = next((i["ip"] for i in r["interfaces"] if i["role"] == "lan"), None) or \
        (r["interfaces"][0]["ip"] if r["interfaces"] else None)
    ping = [{"target": h["ip"], "source": lan_src, "host": h["id"]}
            for h in model["hosts"].values() if h["network"] not in connected]
    return {"router_id": r.get("router_id"), "process_id": model["routing"]["process_id"],
            "area": model["routing"]["area"], "neighbors": neighbors,
            "remote_networks": remote, "local_networks": sorted(connected),
            "passive_interfaces": sorted(if_map[i["network"]] for i in r["interfaces"]
                                         if i["role"] == "lan" and i["network"] in if_map),
            "active_interfaces": sorted(if_map[i["network"]] for i in r["interfaces"]
                                        if i["role"] == "transit" and i["network"] in if_map),
            "ping_checks": ping}


def generate_configs(model: dict, interface_map: dict, plan_id: str, p2p_transit: bool,
                     discoveries: dict | None = None) -> dict:
    out = {}
    # No config without a usable OSPF process id and area. The validator already reports
    # them as errors ("OSPF process id not specified" ...) - never crash on them.
    try:
        int(model["routing"].get("process_id"))
        int(model["routing"].get("area"))
    except (TypeError, ValueError):
        return out
    for rid, r in model["routers"].items():
        if not r.get("inventory_id") or not r.get("router_id"):
            continue
        if_map = interface_map.get(rid) or {}
        if any(i["network"] not in if_map for i in r["interfaces"]):
            continue
        out[r["inventory_id"]] = render_router(model, rid, if_map, plan_id, p2p_transit,
                                               (discoveries or {}).get(r["inventory_id"]))
    return out
