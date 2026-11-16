"""Topology model, interface mapping, config generation and validation."""
import copy
import json
import os
import time

from app.generator import generate_configs, suggest_mapping
from app.topology import build_model
from app.validator import validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPO = json.load(open(os.path.join(ROOT, "tests", "fixtures", "topology.json")))
INV = {"R1": {"id": "R1", "host": "192.168.100.11", "port": 22, "interface_hints": {}},
       "R2": {"id": "R2", "host": "192.168.100.12", "port": 22, "interface_hints": {}}}


def disc(mgmt_ip):
    return {"interfaces": [{"name": f"GigabitEthernet0/{i}", "ip": None} for i in range(3)] +
                          [{"name": "GigabitEthernet0/3", "ip": mgmt_ip}],
            "mgmt_interface": "GigabitEthernet0/3", "mgmt_network": "192.168.100.0/24",
            "cdp": [], "_finished_at": time.time()}


DISC = {"R1": disc("192.168.100.11"), "R2": disc("192.168.100.12")}


def run(topo=TOPO, confirmed=True, mapping=None, discoveries=DISC):
    model, issues = build_model(topo, INV)
    imap = mapping or suggest_mapping(model, discoveries, INV)[0]
    configs = generate_configs(model, imap, "plan-test", True, discoveries)
    return model, imap, configs, validate(model, issues, imap, confirmed, configs, discoveries, INV, 60)


def test_config_starts_with_cleanup_of_previous_plan():
    model, imap, configs, v = run()
    assert v["ok"], v["errors"]
    r1 = configs["R1"]["lines"]
    first = r1[:r1.index("hostname R1")]
    assert first == ["no router ospf 1", "default interface GigabitEthernet0/0",
                     "default interface GigabitEthernet0/1", "default interface GigabitEthernet0/2"]
    assert not any("GigabitEthernet0/3" in ln for ln in r1)  # management never reset


def test_cleanup_removes_other_discovered_ospf_processes():
    d = copy.deepcopy(DISC)
    d["R1"]["ospf_processes"] = [7, 1]
    model, imap, configs, v = run(discoveries=d)
    assert v["ok"], v["errors"]
    r1 = configs["R1"]["lines"]
    assert r1[:2] == ["no router ospf 1", "no router ospf 7"]
    assert "no router ospf 7" not in configs["R2"]["lines"]


def test_cleanup_of_management_interface_is_refused():
    model, issues = build_model(TOPO, INV)
    imap = suggest_mapping(model, DISC, INV)[0]
    configs = generate_configs(model, imap, "p", True, DISC)
    configs["R1"]["lines"].insert(0, "default interface GigabitEthernet0/3")
    v = validate(model, issues, imap, True, configs, DISC, INV, 60)
    assert not v["ok"]
    assert any("management interface" in e["message"] for e in v["errors"])


def test_exam_topology_is_valid():
    model, imap, configs, v = run()
    assert v["ok"], v["errors"]
    assert imap["R1"] == {"10.0.0.0/24": "GigabitEthernet0/0", "10.1.0.0/24": "GigabitEthernet0/1"}
    r1 = configs["R1"]["lines"]
    assert "router ospf 1" in r1 and " router-id 1.1.1.1" in r1
    assert " network 10.0.0.0 0.0.0.255 area 0" in r1 and " network 10.1.0.0 0.0.0.255 area 0" in r1
    assert " passive-interface GigabitEthernet0/0" in r1
    assert " passive-interface GigabitEthernet0/1" not in r1
    assert not any("ip route" in ln or "0/3" in ln for ln in r1)
    assert configs["R1"]["expected"]["remote_networks"] == ["10.2.0.0/24"]
    assert configs["R2"]["expected"]["neighbors"] == ["1.1.1.1"]


def test_unconfirmed_mapping_blocks():
    v = run(confirmed=False)[3]
    assert not v["ok"] and any("confirmed" in e["message"] for e in v["errors"])


def test_management_interface_refused():
    bad = {"R1": {"10.0.0.0/24": "GigabitEthernet0/3", "10.1.0.0/24": "GigabitEthernet0/1"},
           "R2": {"10.1.0.0/24": "GigabitEthernet0/1", "10.2.0.0/24": "GigabitEthernet0/0"}}
    v = run(mapping=bad)[3]
    assert any("MANAGEMENT" in e["message"] for e in v["errors"])


def test_nonexistent_interface_refused():
    bad = {"R1": {"10.0.0.0/24": "GigabitEthernet0/7", "10.1.0.0/24": "GigabitEthernet0/1"},
           "R2": {"10.1.0.0/24": "GigabitEthernet0/1", "10.2.0.0/24": "GigabitEthernet0/0"}}
    v = run(mapping=bad)[3]
    assert any("does not exist" in e["message"] for e in v["errors"])


def test_missing_router_id_not_guessed():
    t = copy.deepcopy(TOPO)
    del t["routing"]["router_ids"]["R2"]
    v = run(topo=t)[3]
    assert any("router-id for R2" in e["message"] for e in v["errors"])


def test_subnet_mismatch_detected():
    t = copy.deepcopy(TOPO)
    t["interfaces"][2]["ip"] = "10.9.0.2"
    v = run(topo=t)[3]
    assert any("not inside" in e["message"] for e in v["errors"])


def test_wrong_gateway_detected():
    t = copy.deepcopy(TOPO)
    t["hosts"][0]["gateway"] = "10.0.0.254"
    v = run(topo=t)[3]
    assert any("gateway" in e["message"] for e in v["errors"])


def test_blocking_ambiguity_blocks():
    t = copy.deepcopy(TOPO)
    t["ambiguities"] = [{"item": "R2 address", "issue": "unreadable", "severity": "blocking"}]
    v = run(topo=t)[3]
    assert not v["ok"]


def test_no_discovery_blocks():
    v = run(discoveries={})[3]
    assert any(e["category"] == "management" for e in v["errors"])


def test_alternative_topology_three_routers():
    """Instructor may test a different plan: 3 routers in a chain, different addressing."""
    t = {
        "devices": [{"id": x, "type": "router"} for x in ("R1", "R2", "R3")] +
                   [{"id": "PCA", "type": "host"}, {"id": "PCB", "type": "host"}],
        "networks": [{"cidr": c} for c in ("172.16.1.0/24", "172.16.12.0/30", "172.16.23.0/30", "172.16.3.0/24")],
        "interfaces": [
            {"device": "R1", "network": "172.16.1.0/24", "ip": "172.16.1.1"},
            {"device": "R1", "network": "172.16.12.0/30", "ip": "172.16.12.1"},
            {"device": "R2", "network": "172.16.12.0/30", "ip": "172.16.12.2"},
            {"device": "R2", "network": "172.16.23.0/30", "ip": "172.16.23.1"},
            {"device": "R3", "network": "172.16.23.0/30", "ip": "172.16.23.2"},
            {"device": "R3", "network": "172.16.3.0/24", "ip": "172.16.3.1"}],
        "hosts": [{"id": "PCA", "ip": "172.16.1.10", "gateway": "172.16.1.1"},
                  {"id": "PCB", "ip": "172.16.3.10", "gateway": "172.16.3.1"}],
        "links": [], "ambiguities": [],
        "routing": {"protocol": "OSPFv2", "process_id": 10, "area": 0,
                    "router_ids": {"R1": "1.1.1.1", "R2": "2.2.2.2", "R3": "3.3.3.3"}},
    }
    inv = dict(INV, R3={"id": "R3", "host": "192.168.100.13", "port": 22, "interface_hints": {}})
    d = dict(DISC, R3=disc("192.168.100.13"))
    model, issues = build_model(t, inv)
    imap = suggest_mapping(model, d, inv)[0]
    configs = generate_configs(model, imap, "p", True)
    v = validate(model, issues, imap, True, configs, d, inv, 60)
    assert v["ok"], v["errors"]
    r2 = configs["R2"]
    assert r2["ospf"]["passive"] == []  # R2 has no LAN
    assert " network 172.16.12.0 0.0.0.3 area 0" in r2["lines"]
    assert configs["R1"]["expected"]["remote_networks"] == ["172.16.23.0/30", "172.16.3.0/24"]
    assert configs["R2"]["expected"]["neighbors"] == ["1.1.1.1", "3.3.3.3"]


def test_picture_without_routing_info_gives_errors_not_a_crash():
    """Diagram-only picture: the AI leaves protocol/process/area empty. Review must show
    validation errors (fix with Edit JSON), not crash with a server error."""
    t = copy.deepcopy(TOPO)
    t["routing"] = {"protocol": None, "process_id": None, "area": None,
                    "router_ids": {"R1": "1.1.1.1", "R2": "2.2.2.2"}}
    model, imap, configs, v = run(topo=t)
    assert configs == {}
    msgs = [e["message"] for e in v["errors"]]
    assert any("process id not specified" in m for m in msgs)
    assert any("area not specified" in m for m in msgs)
