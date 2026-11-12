"""Parsers for Cisco IOS show-command output and OSPF verification logic."""
import ipaddress
import re

IP = r"\d{1,3}(?:\.\d{1,3}){3}"

ABBREV = {"gi": "GigabitEthernet", "gig": "GigabitEthernet", "fa": "FastEthernet", "fas": "FastEthernet",
          "eth": "Ethernet", "et": "Ethernet", "e": "Ethernet", "te": "TenGigabitEthernet",
          "ten": "TenGigabitEthernet", "lo": "Loopback"}


def expand_ifname(name: str) -> str:
    name = name.replace(" ", "")
    m = re.match(r"^([A-Za-z]+)([\d/.:]+)$", name)
    if not m:
        return name
    prefix, num = m.groups()
    for full in ABBREV.values():
        if full.lower() == prefix.lower():
            return full + num
    return ABBREV.get(prefix.lower(), prefix) + num


def parse_ip_int_brief(text: str) -> list[dict]:
    out = []
    rx = re.compile(r"^(\S+)\s+(\S+)\s+(YES|NO)\s+(\S+)\s+(administratively down|up|down|deleted)\s+(up|down)\s*$")
    for line in text.splitlines():
        m = rx.match(line.strip())
        if m:
            name, ip, _, method, status, proto = m.groups()
            out.append({"name": name, "ip": None if ip == "unassigned" else ip,
                        "method": method, "status": status, "protocol": proto})
    return out


def parse_cdp_neighbors(text: str) -> list[dict]:
    out = []
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Device ID")) + 1
    except StopIteration:
        return out
    rx = re.compile(r"^(?:(\S+)\s+)?((?:Gig|Fas|Eth|Ten|Gi|Fa|Et|Te)\s?[\d/]+)\s+\d+\s+")
    pending = None
    for line in lines[start:]:
        if not line.strip() or line.strip().startswith("Total cdp"):
            continue
        m = rx.match(line.strip())
        if m:
            dev = m.group(1) or pending
            if dev:
                out.append({"device": dev, "local_interface": expand_ifname(m.group(2))})
            pending = None
        elif len(line.strip().split()) == 1:
            pending = line.strip()  # long device id wraps onto its own line
    return out


def parse_ip_interface_address(text: str) -> str | None:
    m = re.search(rf"Internet address is ({IP}/\d+)", text)
    return m.group(1) if m else None


def parse_ospf_neighbors(text: str) -> list[dict]:
    rx = re.compile(rf"^(?P<nid>{IP})\s+(?P<pri>\d+)\s+(?P<state>\w+/\s*[\w-]+)\s+(?P<dead>\S+)\s+"
                    rf"(?P<addr>{IP})\s+(?P<intf>\S+)")
    out = []
    for line in text.splitlines():
        m = rx.match(line.strip())
        if m:
            st = re.sub(r"\s+", "", m.group("state"))
            out.append({"neighbor_id": m.group("nid"), "state": st, "full": st.upper().startswith("FULL"),
                        "address": m.group("addr"), "interface": m.group("intf")})
    return out


def parse_ospf_routes(text: str) -> list[dict]:
    """Handles both 'x/8 is subnetted' (mask in header) and 'variably subnetted' formats."""
    out = []
    header_mask = None
    hdr = re.compile(rf"^\s*({IP})/(\d+) is subnetted")
    route = re.compile(rf"^(O(?:\s?(?:IA|E1|E2|N1|N2))?)\s+({IP})(/\d+)?\s+\[(\d+)/(\d+)\]\s+via\s+({IP})(?:,\s*\S+,\s*(\S+))?")
    for line in text.splitlines():
        h = hdr.match(line)
        if h:
            header_mask = int(h.group(2))
            continue
        if "variably subnetted" in line:
            header_mask = None
            continue
        m = route.match(line.strip())
        if m:
            code, net, mask, ad, metric, via, intf = m.groups()
            plen = int(mask[1:]) if mask else header_mask
            if plen is None:
                continue
            out.append({"code": code.replace(" ", ""), "prefix": f"{net}/{plen}", "via": via,
                        "interface": intf, "ad": int(ad), "metric": int(metric)})
    return out


def parse_ip_protocols(text: str) -> dict:
    res = {"process_id": None, "router_id": None, "networks": [], "passive_interfaces": []}
    m = re.search(r'Routing Protocol is "ospf (\d+)"', text)
    if m:
        res["process_id"] = int(m.group(1))
    m = re.search(rf"Router ID ({IP})", text)
    if m:
        res["router_id"] = m.group(1)
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Routing for Networks"):
            section = "networks"
            continue
        if s.startswith("Passive Interface"):
            section = "passive"
            continue
        if s.endswith(":") or s.startswith(("Routing Information Sources", "Distance", "Routing Protocol")):
            section = None
            continue
        if section == "networks":
            n = re.match(rf"^({IP})\s+({IP})\s+area\s+(\S+)", s)
            if n:
                res["networks"].append({"address": n.group(1), "wildcard": n.group(2), "area": n.group(3)})
            else:
                section = None
        elif section == "passive":
            if re.match(r"^[A-Za-z]+[\d/]+$", s):
                res["passive_interfaces"].append(expand_ifname(s))
            else:
                section = None
    return res


def parse_ping(text: str) -> dict:
    m = re.search(r"Success rate is (\d+) percent \((\d+)/(\d+)\)", text)
    if not m:
        return {"success_rate": None}
    return {"success_rate": int(m.group(1)), "received": int(m.group(2)), "sent": int(m.group(3))}


def evaluate_verification(expected: dict, neigh_txt: str, routes_txt: str, proto_txt: str) -> dict:
    neighbors = parse_ospf_neighbors(neigh_txt)
    routes = parse_ospf_routes(routes_txt)
    proto = parse_ip_protocols(proto_txt)
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    full_ids = {n["neighbor_id"] for n in neighbors if n["full"]}
    for nid in expected.get("neighbors", []):
        st = next((n["state"] for n in neighbors if n["neighbor_id"] == nid), "absent")
        check(f"OSPF neighbor {nid} FULL", nid in full_ids, f"state {st}")
    learned = {r["prefix"] for r in routes if r["code"].startswith("O")}
    for net in expected.get("remote_networks", []):
        check(f"route {net} learned via OSPF", str(ipaddress.ip_network(net)) in learned,
              "present" if net in learned else f"not in OSPF routes ({sorted(learned) or 'none'})")
    check("OSPF process id", proto["process_id"] == int(expected.get("process_id") or -1),
          f"running ospf {proto['process_id']}")
    check("OSPF router-id", proto["router_id"] == expected.get("router_id"), f"router-id {proto['router_id']}")
    passive = set(proto["passive_interfaces"])
    for p in expected.get("passive_interfaces", []):
        check(f"{p} passive", p in passive, "passive" if p in passive else "not passive")
    for a in expected.get("active_interfaces", []):
        check(f"{a} not passive (adjacency link)", a not in passive, "active" if a not in passive else "PASSIVE")
    advertised = set()
    for n in proto["networks"]:
        wc = int(ipaddress.IPv4Address(n["wildcard"]))
        advertised.add(f"{n['address']}/{32 - wc.bit_length()}")
    for net in expected.get("local_networks", []):
        check(f"{net} advertised", net in advertised, "in 'Routing for Networks'" if net in advertised else "missing")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "neighbors": neighbors,
            "ospf_routes": routes, "protocols": proto}
