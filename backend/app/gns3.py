"""Read-only GNS3 lab layer: cross-check the AI topology against the real lab cabling.

Sits between AI analysis and the SSH-discovered interface mapping:
  * reads the project's nodes and links from the GNS3 REST API (v3) - GET requests only;
  * checks that the devices and links the AI found exist in the lab;
  * suggests which router port carries which network, from the real cables.

It never changes the lab and never talks to the routers. SSH discovery still supplies the
real interface names, and the administrator still confirms the mapping and approves.
"""
import re

import httpx

from .config import settings
from .inventory import _norm

SWITCH_TYPES = {"ethernet_switch", "ethernet_hub"}
OUTSIDE_TYPES = {"cloud", "nat"}  # bridges out of the lab: the management network
_ABBREV = {"gi": "gigabitethernet", "gig": "gigabitethernet", "fa": "fastethernet",
           "fas": "fastethernet", "e": "ethernet", "et": "ethernet", "eth": "ethernet",
           "te": "tengigabitethernet", "ten": "tengigabitethernet"}


class GNS3Error(Exception):
    pass


def canonical_ifname(name: str) -> str:
    """'Gi0/1', 'GigabitEthernet0/1' and 'gigabitethernet 0/1' all become 'gigabitethernet0/1'."""
    n = re.sub(r"\s+", "", str(name or "")).lower()
    m = re.fullmatch(r"([a-z]+)([\d/.:]+)", n)
    if not m:
        return n
    kind, num = m.groups()
    return _ABBREV.get(kind, kind) + num


class GNS3Client:
    """Minimal GNS3 v3 client. Only GET is ever sent to the lab API; the single POST is the
    login call that turns a username/password into a token (it changes nothing in the lab)."""

    def __init__(self, url: str, token: str | None = None, username: str | None = None,
                 password: str | None = None, timeout: float = 5.0, transport=None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._token = token
        self._username, self._password = username, password

    def _headers(self) -> dict:
        if not self._token and self._username:
            r = self._client.post(f"{self.url}/v3/access/users/authenticate",
                                  json={"username": self._username, "password": self._password})
            if r.status_code >= 400:
                raise GNS3Error(f"GNS3 login failed ({r.status_code})")
            self._token = r.json().get("access_token")
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def get(self, path: str):
        try:
            r = self._client.get(f"{self.url}{path}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise GNS3Error(f"GNS3 server {self.url} unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise GNS3Error(f"GNS3 GET {path} failed ({r.status_code}): {r.text[:200]}")
        return r.json()


def fetch_lab(client: GNS3Client, project: str) -> dict:
    """Return {project, nodes: {name: {type, ports: {(adapter, port): port_name}}}, links: [...]}."""
    projects = client.get("/v3/projects")
    proj = next((p for p in projects if project in (p.get("name"), p.get("project_id"))), None)
    if proj is None:
        raise GNS3Error(f"GNS3 project '{project}' not found")
    if proj.get("status") != "opened":
        raise GNS3Error(f"GNS3 project '{proj['name']}' is closed - open it in GNS3 first")
    pid = proj["project_id"]
    nodes_raw = client.get(f"/v3/projects/{pid}/nodes")
    links_raw = client.get(f"/v3/projects/{pid}/links")
    nodes, by_id = {}, {}
    for n in nodes_raw:
        ports = {(p["adapter_number"], p["port_number"]): p.get("name") or p.get("short_name")
                 for p in n.get("ports", [])}
        nodes[n["name"]] = {"type": n.get("node_type"), "status": n.get("status"), "ports": ports}
        by_id[n["node_id"]] = n["name"]
    links = []
    for lk in links_raw:
        ends = []
        for e in lk.get("nodes", []):
            name = by_id.get(e["node_id"])
            if name is None:
                continue
            port = nodes[name]["ports"].get((e["adapter_number"], e["port_number"]),
                                            f"{e['adapter_number']}/{e['port_number']}")
            ends.append((name, port))
        if len(ends) == 2:
            links.append(ends)
    return {"project": proj["name"], "nodes": nodes, "links": links}


def port_neighbours(lab: dict) -> dict:
    """{node: {port: set(end devices reachable through that port)}}. Switches and hubs are
    looked through, so a router port on a LAN switch maps to every PC on that switch."""
    nodes = lab["nodes"]
    adj: dict = {}
    for (a, pa), (b, pb) in lab["links"]:
        adj.setdefault(a, []).append((pa, b))
        adj.setdefault(b, []).append((pb, a))

    def behind(start: str, came_from: str) -> set:
        if nodes.get(start, {}).get("type") not in SWITCH_TYPES:
            return {start}
        seen, stack, out = {came_from, start}, [start], set()
        while stack:
            cur = stack.pop()
            for _port, nxt in adj.get(cur, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                if nodes.get(nxt, {}).get("type") in SWITCH_TYPES:
                    stack.append(nxt)
                else:
                    out.add(nxt)
        return out

    result: dict = {}
    for node, edges in adj.items():
        if nodes.get(node, {}).get("type") in SWITCH_TYPES:
            continue
        ports = result.setdefault(node, {})
        for port, peer in edges:
            ports.setdefault(port, set()).update(behind(peer, node))
    return result


def cross_check(model: dict, lab: dict) -> dict:
    """Compare the AI/admin topology model with the lab. Returns
    {issues: [...], ports: {router: {network: gns3_port}}, notes: [...], summary: {...}}."""
    issues, notes = [], []
    ports: dict = {}
    lab_by_norm = {_norm(n): n for n in lab["nodes"]}

    def lab_name(dev_id: str, label: str | None = None) -> str | None:
        for cand in (dev_id, label):
            if cand and _norm(cand) in lab_by_norm:
                return lab_by_norm[_norm(cand)]
        return None

    def warn(msg):
        issues.append({"category": "gns3_lab", "severity": "warning", "message": msg})

    routers, hosts = model.get("routers", {}), model.get("hosts", {})
    names = {rid: lab_name(rid, r.get("label")) for rid, r in routers.items()}
    names.update({hid: lab_name(hid) for hid in hosts})
    for dev, ln in names.items():
        if ln is None:
            warn(f"{dev} is in the topology but not in GNS3 project '{lab['project']}'")

    neigh = port_neighbours(lab)
    direct: dict = {}
    for (a, pa), (b, pb) in lab["links"]:
        direct.setdefault(a, {})[pa] = b
        direct.setdefault(b, {})[pb] = a
    outside = {n for n, d in lab["nodes"].items() if d.get("type") in OUTSIDE_TYPES}
    for rid, r in routers.items():
        ln = names.get(rid)
        if not ln:
            continue
        # a port whose segment leads out of the lab (Cloud/NAT) is the management network
        rports = {p: peers for p, peers in neigh.get(ln, {}).items() if not peers & outside}
        chosen: dict = {}
        for itf in r["interfaces"]:
            expected = {names[p] for p in itf.get("peer_routers", []) + itf.get("hosts", []) if names.get(p)}
            if not expected:
                continue
            hits = [p for p, peers in rports.items() if peers & expected]
            if len(hits) > 1:  # prefer a direct cable over a path through a switch
                direct_hits = [p for p in hits if direct.get(ln, {}).get(p) in expected]
                hits = direct_hits if len(direct_hits) == 1 else hits
            if len(hits) == 1:
                chosen[itf["network"]] = hits[0]
                peers = ", ".join(sorted(rports[hits[0]] & expected))
                notes.append(f"{rid} {itf['network']}: GNS3 cable {ln} {hits[0]} -> {peers}")
            elif not hits:
                warn(f"{rid} {itf['network']}: no GNS3 cable from {ln} to {', '.join(sorted(expected))}")
            else:
                warn(f"{rid} {itf['network']}: several {ln} ports reach {', '.join(sorted(expected))} "
                     f"({', '.join(hits)}) - mapping left to discovery")
        if len(set(chosen.values())) < len(chosen):
            warn(f"{rid}: two networks map to the same GNS3 port - check the topology")
        ports[rid] = chosen

    # cables between topology devices that the topology does not have
    known = {v: k for k, v in names.items() if v}
    pairs = set()
    for rid, r in routers.items():
        for itf in r["interfaces"]:
            for p in itf.get("peer_routers", []) + itf.get("hosts", []):
                pairs.add(frozenset((rid, p)))
    for node, pmap in neigh.items():
        if node not in known:
            continue
        for port, peers in pmap.items():
            for peer in peers:
                if peer in known and frozenset((known[node], known[peer])) not in pairs \
                        and known[node] in routers:
                    warn(f"GNS3 has a cable {node} {port} -> {peer} that is not in the topology")
    stopped = [n for n in known if lab["nodes"][n].get("status") != "started"]
    if stopped:
        warn(f"GNS3 nodes not started: {', '.join(sorted(stopped))}")
    return {"issues": issues, "ports": ports, "notes": notes,
            "summary": {"project": lab["project"], "matched": sorted(k for k, v in names.items() if v)}}


def lab_check(model: dict) -> dict | None:
    """Run the cross-check with the configured server. None when the layer is disabled.
    A GNS3 problem is reported as a warning - it never blocks the review on its own."""
    if not settings.gns3_enabled:
        return None
    try:
        client = GNS3Client(settings.gns3_url, settings.gns3_token, settings.gns3_username,
                            settings.gns3_password, settings.gns3_timeout)
        return cross_check(model, fetch_lab(client, settings.gns3_project))
    except GNS3Error as exc:
        return {"issues": [{"category": "gns3_lab", "severity": "warning",
                            "message": f"GNS3 cross-check skipped: {exc}"}],
                "ports": {}, "notes": [], "summary": {"project": settings.gns3_project, "error": str(exc)}}
