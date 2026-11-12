"""Turn the AI's JSON (possibly edited by the admin) into a checked, normalized model.

Nothing here is specific to the exam topology: any number of routers / LANs / transit links
is handled, so an alternative addressing plan from the instructor works the same way.
"""
import ipaddress

from .inventory import match_inventory


def _issue(issues, category, severity, message):
    issues.append({"category": category, "severity": severity, "message": message})


def _net(cidr, issues, where):
    try:
        return ipaddress.ip_network(str(cidr).strip(), strict=True)
    except ValueError:
        try:
            n = ipaddress.ip_network(str(cidr).strip(), strict=False)
            _issue(issues, "addressing", "error",
                   f"{where}: '{cidr}' has host bits set (did you mean {n}?)")
        except ValueError:
            _issue(issues, "addressing", "error", f"{where}: '{cidr}' is not a valid IPv4 network")
        return None


def _ip(value, issues, where):
    try:
        ip = ipaddress.ip_address(str(value).strip())
        if ip.version != 4:
            raise ValueError
        return ip
    except ValueError:
        _issue(issues, "addressing", "error", f"{where}: '{value}' is not a valid IPv4 address")
        return None


def build_model(topology: dict, inventory: dict) -> tuple[dict, list]:
    issues: list = []
    topology = topology or {}
    devices = {str(d.get("id")): d for d in topology.get("devices", []) if d.get("id")}
    routers_declared = {k for k, d in devices.items() if str(d.get("type", "")).lower() == "router"}

    networks: dict = {}
    for n in topology.get("networks", []):
        net = _net(n.get("cidr"), issues, "network")
        if net is not None:
            if str(net) in networks:
                _issue(issues, "addressing", "error", f"network {net} is listed twice")
            networks[str(net)] = {"cidr": str(net), "net": net, "routers": [], "hosts": [],
                                  "description": n.get("description") or ""}

    # overlapping networks
    nets = list(networks.values())
    for i, a in enumerate(nets):
        for b in nets[i + 1:]:
            if a["net"].overlaps(b["net"]):
                _issue(issues, "subnet", "error", f"networks {a['cidr']} and {b['cidr']} overlap")

    used_ips: dict = {}
    routers: dict = {}
    for i in topology.get("interfaces", []):
        dev = str(i.get("device"))
        where = f"{dev} interface on {i.get('network')}"
        if dev not in devices:
            _issue(issues, "addressing", "error", f"{where}: device '{dev}' is not in devices[]")
        net = _net(i.get("network"), issues, where)
        ip = _ip(i.get("ip"), issues, where)
        if net is None or ip is None:
            continue
        if str(net) not in networks:
            _issue(issues, "subnet", "warning", f"{where}: network {net} added (missing from networks[])")
            networks[str(net)] = {"cidr": str(net), "net": net, "routers": [], "hosts": [], "description": ""}
        prefix = i.get("prefix_length")
        if prefix is not None and int(prefix) != net.prefixlen:
            _issue(issues, "subnet", "error",
                   f"{where}: prefix /{prefix} does not match network {net}")
        if ip not in net:
            _issue(issues, "subnet", "error", f"{where}: {ip} is not inside {net}")
        elif ip in (net.network_address, net.broadcast_address):
            _issue(issues, "addressing", "error", f"{where}: {ip} is the network/broadcast address")
        if str(ip) in used_ips:
            _issue(issues, "addressing", "error",
                   f"duplicate IP {ip} ({used_ips[str(ip)]} and {dev})")
        used_ips[str(ip)] = dev
        r = routers.setdefault(dev, {"id": dev, "label": devices.get(dev, {}).get("label"),
                                     "interfaces": []})
        if any(x["network"] == str(net) for x in r["interfaces"]):
            _issue(issues, "subnet", "error", f"{dev} has two interfaces in {net}")
        peer = i.get("peer")
        r["interfaces"].append({"network": str(net), "ip": str(ip), "prefix": net.prefixlen,
                                "netmask": str(net.netmask),
                                "peers": [peer] if peer else []})
        networks[str(net)]["routers"].append(dev)

    for missing in routers_declared - routers.keys():
        _issue(issues, "addressing", "error", f"router {missing} has no addressed interfaces")

    hosts: dict = {}
    for h in topology.get("hosts", []):
        hid = str(h.get("id"))
        where = f"host {hid}"
        ip = _ip(h.get("ip"), issues, where)
        if ip is None:
            continue
        if str(ip) in used_ips:
            _issue(issues, "addressing", "error", f"duplicate IP {ip} ({used_ips[str(ip)]} and {hid})")
        used_ips[str(ip)] = hid
        home = next((n for n in networks.values() if ip in n["net"]), None)
        if home is None:
            _issue(issues, "subnet", "error", f"{where}: {ip} is not inside any known network")
            continue
        if h.get("prefix_length") is not None and int(h["prefix_length"]) != home["net"].prefixlen:
            _issue(issues, "subnet", "error",
                   f"{where}: prefix /{h['prefix_length']} does not match {home['cidr']}")
        gw = h.get("gateway")
        if not gw:
            _issue(issues, "addressing", "warning", f"{where}: no default gateway given")
        else:
            gw_ip = _ip(gw, issues, f"{where} gateway")
            if gw_ip is not None:
                if gw_ip not in home["net"]:
                    _issue(issues, "subnet", "error", f"{where}: gateway {gw_ip} not in {home['cidr']}")
                elif used_ips.get(str(gw_ip)) not in routers:
                    _issue(issues, "addressing", "error",
                           f"{where}: gateway {gw_ip} is not a router interface address")
        hosts[hid] = {"id": hid, "ip": str(ip), "network": home["cidr"], "gateway": gw}
        home["hosts"].append(hid)

    # role of each network: transit = 2+ routers, lan = 1 router
    for n in networks.values():
        n["role"] = "transit" if len(n["routers"]) >= 2 else ("lan" if n["routers"] else "orphan")
        if n["role"] == "orphan":
            _issue(issues, "subnet", "warning", f"network {n['cidr']} has no router interface")
    for r in routers.values():
        for itf in r["interfaces"]:
            itf["role"] = networks[itf["network"]]["role"]
            itf["peer_routers"] = [x for x in networks[itf["network"]]["routers"] if x != r["id"]]
            itf["hosts"] = networks[itf["network"]]["hosts"]

    # links must connect devices that sit on the stated network
    for link in topology.get("links", []):
        eps = [str(e) for e in (link.get("endpoints") or [])]
        net = networks.get(str(link.get("network")))
        if net is None:
            _issue(issues, "subnet", "warning", f"link {eps} references unknown network {link.get('network')}")
            continue
        for e in eps:
            if e not in net["routers"] and e not in net["hosts"] and \
                    str(devices.get(e, {}).get("type", "")).lower() != "switch":
                _issue(issues, "subnet", "error",
                       f"link {eps}: {e} has no address in {net['cidr']}")

    # routing requirements
    routing = dict(topology.get("routing") or {})
    proto = str(routing.get("protocol") or "")
    if "ospf" not in proto.lower():
        _issue(issues, "ospf", "error", f"routing protocol must be OSPFv2 (got '{proto or 'null'}')")
    if routing.get("process_id") in (None, ""):
        _issue(issues, "ospf", "error", "OSPF process id not specified")
    if routing.get("area") in (None, ""):
        _issue(issues, "ospf", "error", "OSPF area not specified")
    rids = routing.get("router_ids") or {}
    seen_rids: dict = {}
    for rid_dev in routers:
        rid = rids.get(rid_dev)
        if rid is None and routers[rid_dev].get("label"):
            rid = rids.get(routers[rid_dev]["label"])
        if not rid:
            _issue(issues, "ospf", "error", f"router-id for {rid_dev} not specified (not guessed)")
            continue
        rid_ip = _ip(rid, issues, f"{rid_dev} router-id")
        if rid_ip is None:
            continue
        if str(rid_ip) in seen_rids:
            _issue(issues, "ospf", "error", f"router-id {rid_ip} used by {seen_rids[str(rid_ip)]} and {rid_dev}")
        seen_rids[str(rid_ip)] = rid_dev
        routers[rid_dev]["router_id"] = str(rid_ip)
    if routing.get("static_routes_allowed"):
        _issue(issues, "ospf", "warning", "static routes are not used by this system (OSPF only)")

    # inventory mapping
    for r in routers.values():
        inv = match_inventory(r["id"], r.get("label"), inventory)
        r["inventory_id"] = inv
        if inv is None:
            _issue(issues, "management", "error",
                   f"router {r['id']} is not in the device inventory (no management address)")

    for a in topology.get("ambiguities", []) or []:
        sev = "error" if str(a.get("severity", "")).lower() == "blocking" else "warning"
        _issue(issues, "ai_ambiguity", sev,
               f"AI flagged: {a.get('item', '?')} - {a.get('issue', '')}")

    model = {
        "routers": routers,
        "hosts": hosts,
        "networks": {k: {kk: vv for kk, vv in v.items() if kk != "net"} for k, v in networks.items()},
        "routing": {
            "protocol": proto,
            "process_id": routing.get("process_id"),
            "area": routing.get("area"),
            "passive_lan_interfaces": routing.get("passive_lan_interfaces", True),
        },
    }
    return model, issues
