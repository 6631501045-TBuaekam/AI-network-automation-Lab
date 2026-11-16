"""Parser tests using output in the exact format printed by Cisco IOSv 15.x."""
from parsers import (evaluate_verification, expand_ifname, parse_cdp_neighbors, parse_ip_int_brief,
                     parse_ip_protocols, parse_ospf_neighbors, parse_ospf_routes, parse_ping)

BRIEF = """Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         unassigned      YES unset  administratively down down
GigabitEthernet0/1         unassigned      YES unset  administratively down down
GigabitEthernet0/2         unassigned      YES unset  administratively down down
GigabitEthernet0/3         192.168.100.11  YES NVRAM  up                    up"""

NEIGH_BCAST = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.1.0.2        GigabitEthernet0/1"""

NEIGH_P2P = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           0   FULL/  -        00:00:39    10.1.0.2        GigabitEthernet0/1"""

ROUTES_SUBNETTED = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       O - OSPF, IA - OSPF inter area

Gateway of last resort is not set

      10.0.0.0/24 is subnetted, 1 subnets
O        10.2.0.0 [110/2] via 10.1.0.2, 00:01:12, GigabitEthernet0/1"""

ROUTES_VARIABLY = """Gateway of last resort is not set

      10.0.0.0/8 is variably subnetted, 5 subnets, 2 masks
O        10.2.0.0/24 [110/2] via 10.1.0.2, 00:01:12, GigabitEthernet0/1"""

PROTOCOLS = """*** IP Routing is NSF aware ***

Routing Protocol is "application"
  Sending updates every 0 seconds
  Maximum path: 32
  Routing for Networks:
  Routing Information Sources:
    Gateway         Distance      Last Update
  Distance: (default is 4)

Routing Protocol is "ospf 1"
  Outgoing update filter list for all interfaces is not set
  Incoming update filter list for all interfaces is not set
  Router ID 1.1.1.1
  Number of areas in this router is 1. 1 normal 0 stub 0 nssa
  Maximum path: 4
  Routing for Networks:
    10.0.0.0 0.0.0.255 area 0
    10.1.0.0 0.0.0.255 area 0
  Passive Interface(s):
    GigabitEthernet0/0
  Routing Information Sources:
    Gateway         Distance      Last Update
    2.2.2.2              110      00:00:46
  Distance: (default is 110)
"""

CDP = """Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge

Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID
R2.lab.local     Gig 0/1           163              R B             Gig 0/1
very-long-hostname.example.com
                 Gig 0/2           150              R B             Gig 0/0

Total cdp entries displayed : 2"""


def test_brief():
    itfs = parse_ip_int_brief(BRIEF)
    assert [i["name"] for i in itfs] == [f"GigabitEthernet0/{n}" for n in range(4)]
    assert itfs[3]["ip"] == "192.168.100.11" and itfs[0]["ip"] is None
    assert itfs[0]["status"] == "administratively down"


def test_cdp():
    c = parse_cdp_neighbors(CDP)
    assert c == [{"device": "R2.lab.local", "local_interface": "GigabitEthernet0/1"},
                 {"device": "very-long-hostname.example.com", "local_interface": "GigabitEthernet0/2"}]
    assert expand_ifname("Fa 0/0") == "FastEthernet0/0"


def test_neighbors_both_formats():
    for text in (NEIGH_BCAST, NEIGH_P2P):
        n = parse_ospf_neighbors(text)
        assert len(n) == 1 and n[0]["neighbor_id"] == "2.2.2.2" and n[0]["full"]
        assert n[0]["interface"] == "GigabitEthernet0/1"


def test_routes_both_formats():
    for text in (ROUTES_SUBNETTED, ROUTES_VARIABLY):
        r = parse_ospf_routes(text)
        assert [x["prefix"] for x in r] == ["10.2.0.0/24"]


def test_protocols():
    p = parse_ip_protocols(PROTOCOLS)
    assert p["process_id"] == 1 and p["router_id"] == "1.1.1.1"
    assert p["passive_interfaces"] == ["GigabitEthernet0/0"]
    assert len(p["networks"]) == 2


def test_ping():
    assert parse_ping("Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/4 ms")["success_rate"] == 100


def test_evaluate():
    exp = {"neighbors": ["2.2.2.2"], "remote_networks": ["10.2.0.0/24"], "process_id": 1,
           "router_id": "1.1.1.1", "passive_interfaces": ["GigabitEthernet0/0"],
           "active_interfaces": ["GigabitEthernet0/1"], "local_networks": ["10.0.0.0/24", "10.1.0.0/24"]}
    ev = evaluate_verification(exp, NEIGH_P2P, ROUTES_SUBNETTED, PROTOCOLS)
    assert ev["ok"], ev["checks"]
    ev = evaluate_verification(exp, "", ROUTES_SUBNETTED, PROTOCOLS)
    assert not ev["ok"]


def test_ospf_processes_from_show_ip_protocols():
    from parsers import parse_ospf_processes
    txt = ('Routing Protocol is "ospf 1"\n  Router ID 1.1.1.1\n'
           'Routing Protocol is "ospf 7"\n  Router ID 7.7.7.7\n')
    assert parse_ospf_processes(txt) == [1, 7]
    assert parse_ospf_processes("") == []


def test_verification_reads_the_expected_process_and_flags_leftovers():
    from parsers import evaluate_verification
    proto = ('Routing Protocol is "ospf 70"\n  Router ID 7.0.0.1\n'
             'Routing Protocol is "ospf 1"\n  Router ID 1.1.1.1\n')
    ev = evaluate_verification({"process_id": 1, "router_id": "1.1.1.1"}, "", "", proto)
    checks = {c["name"]: c for c in ev["checks"]}
    assert checks["OSPF process id"]["ok"] and checks["OSPF router-id"]["ok"]
    assert not checks["no other OSPF process"]["ok"]
    assert "70" in checks["no other OSPF process"]["detail"]
