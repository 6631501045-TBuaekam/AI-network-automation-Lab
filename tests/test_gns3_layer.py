"""Read-only GNS3 lab cross-check: lab parsing, cross-check, mapping suggestions, safety."""
import json
import os
import time

import httpx
import pytest

from app import gns3
from app.config import reload_settings
from app.generator import suggest_mapping
from app.topology import build_model
from app.validator import validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPO = json.load(open(os.path.join(ROOT, "backend", "fixtures", "mock_ai_response.json")))
INV = {"R1": {"id": "R1", "host": "192.168.192.11", "port": 22, "interface_hints": {}},
       "R2": {"id": "R2", "host": "192.168.192.12", "port": 22, "interface_hints": {}}}
PID = "709c9268-55ef-4941-b221-70caa5eec57f"


def _router(nid, name):
    return {"node_id": nid, "name": name, "node_type": "dynamips", "status": "started", "ports": [
        {"name": "FastEthernet0/0", "adapter_number": 0, "port_number": 0},
        {"name": "FastEthernet1/0", "adapter_number": 1, "port_number": 0},
        {"name": "FastEthernet1/1", "adapter_number": 1, "port_number": 1}]}


def _simple(nid, name, ntype, n_ports=1):
    return {"node_id": nid, "name": name, "node_type": ntype, "status": "started",
            "ports": [{"name": f"Ethernet{i}", "adapter_number": 0, "port_number": i} for i in range(n_ports)]}


def _link(a, ap, b, bp):
    return {"nodes": [{"node_id": a, "adapter_number": ap[0], "port_number": ap[1]},
                      {"node_id": b, "adapter_number": bp[0], "port_number": bp[1]}]}


def lab_api(swap_r1=False, extra_cable=False, drop_r2_pc2=False, project_status="opened"):
    """Fake GNS3 v3 API for the exam lab. swap_r1 puts PC1 on Fa1/0 and R2 on Fa0/0."""
    nodes = [_router("r1", "R1"), _router("r2", "R2"), _simple("pc1", "PC1", "vpcs"),
             _simple("pc2", "PC2", "vpcs"), _simple("sw", "MGMT-SW", "ethernet_switch", 8),
             _simple("cl", "MGMT-CLOUD", "cloud", 2)]
    r1_lan, r1_wan = ((1, 0), (0, 0)) if swap_r1 else ((0, 0), (1, 0))
    links = [_link("pc1", (0, 0), "r1", r1_lan), _link("r1", r1_wan, "r2", (1, 0)),
             _link("r1", (1, 1), "sw", (0, 1)), _link("r2", (1, 1), "sw", (0, 2)),
             _link("cl", (0, 0), "sw", (0, 3))]
    if not drop_r2_pc2:
        links.append(_link("r2", (0, 0), "pc2", (0, 0)))
    if extra_cable:
        nodes[0]["ports"].append({"name": "FastEthernet2/0", "adapter_number": 2, "port_number": 0})
        links.append(_link("r1", (2, 0), "pc2", (0, 0)))
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path))
        if req.method != "GET":
            return httpx.Response(405)
        if req.url.path == "/v3/projects":
            return httpx.Response(200, json=[{"name": "ai-netauto-ospf", "project_id": PID, "status": project_status}])
        if req.url.path == f"/v3/projects/{PID}/nodes":
            return httpx.Response(200, json=nodes)
        if req.url.path == f"/v3/projects/{PID}/links":
            return httpx.Response(200, json=links)
        return httpx.Response(404)
    return httpx.MockTransport(handler), seen


def _lab(**kw):
    transport, seen = lab_api(**kw)
    client = gns3.GNS3Client("http://gns3.test", token="t", transport=transport)
    return gns3.fetch_lab(client, "ai-netauto-ospf"), seen


def _disc(order):
    return {"interfaces": [{"name": n, "ip": None} for n in order] +
                          [{"name": "FastEthernet1/1", "ip": "192.168.192.11"}],
            "mgmt_interface": "FastEthernet1/1", "mgmt_network": "192.168.192.0/24",
            "cdp": [], "_finished_at": time.time()}


def test_canonical_ifname():
    c = gns3.canonical_ifname
    assert c("Gi0/1") == c("GigabitEthernet0/1") == c("gigabitethernet 0/1") == "gigabitethernet0/1"
    assert c("Fa1/0") == c("FastEthernet1/0")
    assert c("e0") == "ethernet0"


def test_lab_read_uses_get_only_and_cross_check_is_clean():
    lab, seen = _lab()
    assert {m for m, _ in seen} == {"GET"}
    model, _ = build_model(TOPO, INV)
    res = gns3.cross_check(model, lab)
    assert res["issues"] == []
    assert res["ports"]["R1"] == {"10.0.0.0/24": "FastEthernet0/0", "10.1.0.0/24": "FastEthernet1/0"}
    assert res["ports"]["R2"] == {"10.1.0.0/24": "FastEthernet1/0", "10.2.0.0/24": "FastEthernet0/0"}


def test_switch_is_looked_through():
    lab, _ = _lab()
    neigh = gns3.port_neighbours(lab)
    assert neigh["R1"]["FastEthernet1/1"] == {"R2", "MGMT-CLOUD"}  # through MGMT-SW
    assert "MGMT-SW" not in neigh


def test_gns3_cabling_beats_ordered_guess():
    lab, _ = _lab(swap_r1=True)  # PC1 on Fa1/0, R2 on Fa0/0
    model, _ = build_model(TOPO, INV)
    res = gns3.cross_check(model, lab)
    discs = {"R1": _disc(["FastEthernet0/0", "FastEthernet1/0"]),
             "R2": _disc(["FastEthernet0/0", "FastEthernet1/0"])}
    guess, _ = suggest_mapping(model, discs, INV)
    assert guess["R1"]["10.0.0.0/24"] == "FastEthernet0/0"  # the old ordered guess: wrong here
    imap, notes = suggest_mapping(model, discs, INV, res["ports"])
    assert imap["R1"] == {"10.0.0.0/24": "FastEthernet1/0", "10.1.0.0/24": "FastEthernet0/0"}
    assert any("GNS3 lab cable" in n for n in notes)


def test_gns3_port_missing_from_discovery_is_not_used():
    lab, _ = _lab()
    model, _ = build_model(TOPO, INV)
    res = gns3.cross_check(model, lab)
    discs = {"R1": _disc(["GigabitEthernet0/0", "GigabitEthernet0/1"]), "R2": _disc(["FastEthernet0/0", "FastEthernet1/0"])}
    imap, notes = suggest_mapping(model, discs, INV, res["ports"])
    assert set(imap["R1"].values()) <= {"GigabitEthernet0/0", "GigabitEthernet0/1"}
    assert any("not among the discovered free interfaces" in n for n in notes)


def test_missing_and_extra_cables_are_warnings():
    lab, _ = _lab(drop_r2_pc2=True, extra_cable=True)
    model, _ = build_model(TOPO, INV)
    res = gns3.cross_check(model, lab)
    msgs = [i["message"] for i in res["issues"]]
    assert all(i["severity"] == "warning" for i in res["issues"])
    assert any("R2 10.2.0.0/24: no GNS3 cable from R2 to PC2" in m for m in msgs)
    assert any("cable R1 FastEthernet2/0 -> PC2 that is not in the topology" in m for m in msgs)


def test_validator_warns_when_mapping_disagrees_with_lab():
    lab, _ = _lab()
    model, issues = build_model(TOPO, INV)
    res = gns3.cross_check(model, lab)
    imap = {"R1": {"10.0.0.0/24": "FastEthernet1/0", "10.1.0.0/24": "FastEthernet0/0"}}
    out = validate(model, issues + res["issues"], imap, True, {}, {}, INV, 120, res)
    lab_check = next(c for c in out["checks"] if c["name"] == "gns3_lab")
    assert lab_check["status"] == "warn"
    assert any("GNS3 cable for it is on FastEthernet0/0" in w for w in lab_check["warnings"])
    assert not any(e["category"] == "gns3_lab" for e in out["errors"])  # never blocks by itself
    assert "gns3_lab" not in [c["name"] for c in validate(model, issues, imap, True, {}, {}, INV, 120)["checks"]]


def test_lab_check_disabled_and_unreachable(monkeypatch):
    model, _ = build_model(TOPO, INV)
    monkeypatch.setenv("GNS3_ENABLED", "false")
    reload_settings()
    assert gns3.lab_check(model) is None
    monkeypatch.setenv("GNS3_ENABLED", "true")
    monkeypatch.setenv("GNS3_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GNS3_TIMEOUT", "1")
    reload_settings()
    try:
        res = gns3.lab_check(model)
        assert res["ports"] == {}
        assert res["issues"][0]["severity"] == "warning" and "skipped" in res["issues"][0]["message"]
    finally:
        monkeypatch.delenv("GNS3_ENABLED")
        monkeypatch.delenv("GNS3_URL")
        monkeypatch.delenv("GNS3_TIMEOUT")
        reload_settings()


def test_closed_project_is_reported():
    transport, _ = lab_api(project_status="closed")
    client = gns3.GNS3Client("http://gns3.test", token="t", transport=transport)
    with pytest.raises(gns3.GNS3Error, match="closed"):
        gns3.fetch_lab(client, "ai-netauto-ospf")
