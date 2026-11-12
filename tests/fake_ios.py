"""A small simulated Cisco IOS lab served over real SSH (Paramiko server).

Used only by the automated tests: it lets the worker's Paramiko code, prompt handling,
config push and OSPF verification parsers run end-to-end without GNS3. It models just
enough IOS behaviour: modes/prompts, interface + OSPF config, 'show' output in IOS
format, adjacency (non-passive interfaces in a shared subnet) and route learning.
"""
import ipaddress
import socket
import threading
import time

import paramiko

INVALID = "% Invalid input detected at '^' marker."


class FakeRouter:
    def __init__(self, lab, name, mgmt_ip, data_ifs=("GigabitEthernet0/0", "GigabitEthernet0/1",
                                                     "GigabitEthernet0/2"), mgmt_if="GigabitEthernet0/3"):
        self.lab, self.hostname = lab, "Router"
        self.name = name
        self.interfaces = {n: {"ip": None, "mask": None, "shutdown": True, "p2p": False} for n in data_ifs}
        self.interfaces[mgmt_if] = {"ip": mgmt_ip, "mask": "255.255.255.0", "shutdown": False, "p2p": False}
        self.mgmt_if = mgmt_if
        self.ospf = None  # {"pid","rid","networks":[(net,area)],"passive":set()}
        self.configured_at = None
        self.saved = False

    # ---------- derived state
    def _net(self, itf):
        i = self.interfaces[itf]
        return ipaddress.ip_interface(f"{i['ip']}/{i['mask']}").network if i["ip"] else None

    def ospf_ifaces(self):
        if not self.ospf:
            return []
        res = []
        for name, i in self.interfaces.items():
            if not i["ip"] or i["shutdown"]:
                continue
            ip = ipaddress.ip_address(i["ip"])
            if any(ip in n for n, _ in self.ospf["networks"]):
                res.append(name)
        return res

    def neighbors(self):
        out = []
        if not self.ospf:
            return out
        for name in self.ospf_ifaces():
            if name in self.ospf["passive"]:
                continue
            net = self._net(name)
            for other in self.lab.routers.values():
                if other is self or not other.ospf:
                    continue
                for oname in other.ospf_ifaces():
                    if oname in other.ospf["passive"] or other._net(oname) != net:
                        continue
                    ready = time.time() - max(self.configured_at or 0, other.configured_at or 0) >= self.lab.adjacency_delay
                    p2p = self.interfaces[name]["p2p"] and other.interfaces[oname]["p2p"]
                    state = ("FULL/  -" if p2p else "FULL/DR") if ready else ("INIT/  -" if p2p else "INIT/DROTHER")
                    out.append({"rid": other.ospf["rid"], "state": state, "addr": other.interfaces[oname]["ip"],
                                "intf": name, "router": other, "full": ready})
        return out

    def advertised(self):
        return {self._net(n) for n in self.ospf_ifaces()}

    def connected(self):
        return {self._net(n) for n, i in self.interfaces.items() if i["ip"] and not i["shutdown"]}

    def ospf_routes(self):
        # BFS over FULL adjacencies
        routes, seen = {}, {self}
        frontier = [(n["router"], n) for n in self.neighbors() if n["full"]]
        while frontier:
            r, first_hop = frontier.pop(0)
            if r in seen:
                continue
            seen.add(r)
            for net in r.advertised():
                if net not in self.connected() and net not in routes:
                    routes[net] = first_hop
            frontier += [(n["router"], first_hop) for n in r.neighbors() if n["full"]]
        return routes

    # ---------- show commands
    def show(self, cmd):
        c = " ".join(cmd.split())
        if c == "show ip interface brief":
            lines = ["Interface              IP-Address      OK? Method Status                Protocol"]
            for n, i in sorted(self.interfaces.items()):
                st = "administratively down" if i["shutdown"] else "up"
                pr = "down" if i["shutdown"] else "up"
                lines.append(f"{n:<23}{(i['ip'] or 'unassigned'):<16}YES {'manual' if i['ip'] else 'unset':<7}"
                             f"{st:<22}{pr}")
            return "\n".join(lines)
        if c == "show cdp neighbors":
            return ("Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge\n\n"
                    "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID\n\n"
                    "Total cdp entries displayed : 0")
        if c.startswith("show version"):
            return "Cisco IOS Software, IOSv Software (VIOS-ADVENTERPRISEK9-M), Version 15.9(3)M6\nRouter uptime is 1 hour"
        if c.startswith("show ip interface "):
            n = c.split()[-1]
            i = self.interfaces.get(n)
            if not i:
                return INVALID
            pl = ipaddress.ip_network(f"0.0.0.0/{i['mask']}").prefixlen if i["mask"] else 0
            return f"{n} is up, line protocol is up\n  Internet address is {i['ip']}/{pl}"
        if c == "show running-config":
            return self.running_config()
        if c == "show clock":
            return time.strftime("*%H:%M:%S.000 UTC %a %b %d %Y")
        if c == "show ip ospf neighbor":
            if not self.ospf:
                return ""
            lines = ["Neighbor ID     Pri   State           Dead Time   Address         Interface"]
            for n in self.neighbors():
                pri = "0" if "  -" in n["state"] else "1"
                lines.append(f"{n['rid']:<16}{pri:>3}   {n['state']:<16}00:00:35    {n['addr']:<16}{n['intf']}")
            return "\n".join(lines)
        if c == "show ip route ospf":
            hdr = ("Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP\n"
                   "       O - OSPF, IA - OSPF inter area\n\nGateway of last resort is not set\n")
            routes = self.ospf_routes()
            if not routes:
                return hdr
            body = [f"      10.0.0.0/8 is variably subnetted, {len(routes)} subnets, 2 masks"] \
                if self.lab.variably_subnetted else [f"      10.0.0.0/24 is subnetted, {len(routes)} subnets"]
            for net, nb in sorted(routes.items()):
                pfx = str(net) if self.lab.variably_subnetted else str(net.network_address)
                body.append(f"O        {pfx} [110/2] via {nb['addr']}, 00:00:12, {nb['intf']}")
            return hdr + "\n".join(body)
        if c == "show ip protocols":
            if not self.ospf:
                return ""
            o = self.ospf
            lines = [f'*** IP Routing is NSF aware ***\n\nRouting Protocol is "ospf {o["pid"]}"',
                     "  Outgoing update filter list for all interfaces is not set",
                     f"  Router ID {o['rid']}",
                     "  Number of areas in this router is 1. 1 normal 0 stub 0 nssa",
                     "  Maximum path: 4", "  Routing for Networks:"]
            for n, a in o["networks"]:
                lines.append(f"    {n.network_address} {n.hostmask} area {a}")
            if o["passive"]:
                lines.append("  Passive Interface(s):")
                lines += [f"    {p}" for p in sorted(o["passive"])]
            lines += ["  Routing Information Sources:", "    Gateway         Distance      Last Update",
                      "  Distance: (default is 110)"]
            return "\n".join(lines)
        if c.startswith("ping "):
            parts = c.split()
            target = ipaddress.ip_address(parts[1])
            src = ipaddress.ip_address(parts[parts.index("source") + 1]) if "source" in parts else None
            ok = self.lab.can_reach(self, target, src)
            return ("Type escape sequence to abort.\nSending 5, 100-byte ICMP Echos to "
                    f"{target}, timeout is 1 seconds:\n" + ("!!!!!" if ok else ".....") +
                    f"\nSuccess rate is {100 if ok else 0} percent ({5 if ok else 0}/5)")
        return INVALID

    def running_config(self):
        lines = ["Building configuration...", "", "!", f"hostname {self.hostname}", "!"]
        for n, i in sorted(self.interfaces.items()):
            lines.append(f"interface {n}")
            lines.append(f" ip address {i['ip']} {i['mask']}" if i["ip"] else " no ip address")
            if i["shutdown"]:
                lines.append(" shutdown")
            lines.append("!")
        lines.append("end")
        return "\n".join(lines)


class Lab:
    def __init__(self, adjacency_delay=0.0, variably_subnetted=False, password="lab-pass", username="netadmin"):
        self.routers: dict = {}
        self.hosts: dict = {}  # ip -> network
        self.adjacency_delay = adjacency_delay
        self.variably_subnetted = variably_subnetted
        self.username, self.password = username, password
        self.host_key = paramiko.RSAKey.generate(2048)
        self._servers = []

    def add_router(self, name, mgmt_ip="127.0.0.1"):
        r = FakeRouter(self, name, mgmt_ip)
        self.routers[name] = r
        return r

    def add_host(self, ip, cidr, gateway):
        self.hosts[ipaddress.ip_address(ip)] = (ipaddress.ip_network(cidr), ipaddress.ip_address(gateway))

    def can_reach(self, router, target, src):
        if target not in self.hosts:
            return False
        tnet, gw = self.hosts[target]
        if tnet not in router.connected() and tnet not in router.ospf_routes():
            return False
        home = next((r for r in self.routers.values() if tnet in r.connected()
                     and any(i["ip"] == str(gw) for i in r.interfaces.values())), None)
        if home is None:
            return False
        if src is None or home is router:
            return True
        snet = next((router._net(n) for n, i in router.interfaces.items() if i["ip"] == str(src)), None)
        return snet is not None and (snet in home.connected() or snet in home.ospf_routes())

    # ---------- SSH plumbing
    def serve(self, router_name, host="127.0.0.1", port=0) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(20)
        self._servers.append(sock)
        threading.Thread(target=self._accept_loop, args=(sock, router_name), daemon=True).start()
        return sock.getsockname()[1]

    def _accept_loop(self, sock, name):
        while True:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client, name), daemon=True).start()

    def _session(self, client, name):
        lab = self

        class Srv(paramiko.ServerInterface):
            def __init__(self):
                self.event = threading.Event()

            def check_auth_password(self, u, p):
                return paramiko.AUTH_SUCCESSFUL if (u, p) == (lab.username, lab.password) else paramiko.AUTH_FAILED

            def get_allowed_auths(self, u):
                return "password"

            def check_channel_request(self, kind, cid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_pty_request(self, *a):
                return True

            def check_channel_shell_request(self, ch):
                self.event.set()
                return True

        t = paramiko.Transport(client)
        t.add_server_key(self.host_key)
        srv = Srv()
        try:
            t.start_server(server=srv)
        except Exception:
            return
        chan = t.accept(20)
        if chan is None:
            return
        srv.event.wait(10)
        CliSession(self.routers[name], chan).run()


class CliSession:
    def __init__(self, router: FakeRouter, chan):
        self.r, self.chan = router, chan
        self.mode, self.ctx = "exec", None

    def prompt(self):
        suffix = {"exec": "#", "config": "(config)#", "if": "(config-if)#", "router": "(config-router)#"}[self.mode]
        return self.r.hostname + suffix

    def write(self, s):
        self.chan.send(s.replace("\n", "\r\n"))

    def run(self):
        self.write("\n" + self.prompt())
        buf = ""
        try:
            while True:
                data = self.chan.recv(1024)
                if not data:
                    return
                for ch in data.decode(errors="ignore"):
                    if ch in "\r\n":
                        line, buf = buf, ""
                        self.write("\n")
                        out = self.execute(line)
                        if out is None:
                            self.chan.close()
                            return
                        if out:
                            self.write(out + "\n")
                        self.write(self.prompt())
                    else:
                        buf += ch
                        self.write(ch)  # echo
        except (OSError, EOFError):
            return

    def execute(self, line):
        s = line.strip()
        r = self.r
        if not s:
            return ""
        if self.mode == "exec":
            if s in ("exit", "logout"):
                return None
            if s.startswith("terminal "):
                return ""
            if s == "configure terminal":
                self.mode = "config"
                return "Enter configuration commands, one per line.  End with CNTL/Z."
            if s == "write memory":
                r.saved = True
                return "Building configuration...\n[OK]"
            return r.show(s)
        if s == "end":
            self.mode, self.ctx = "exec", None
            return ""
        if s == "exit":
            self.mode, self.ctx = ("config", None) if self.mode in ("if", "router") else ("exec", None)
            return ""
        w = s.split()
        if w[0] == "hostname" and len(w) == 2:
            r.hostname = w[1]
            return ""
        if w[0] == "interface" and len(w) == 2:
            if w[1] not in r.interfaces:
                return INVALID
            self.mode, self.ctx = "if", w[1]
            return ""
        if w[:2] == ["router", "ospf"] and len(w) == 3:
            if r.ospf is None:
                r.ospf = {"pid": int(w[2]), "rid": None, "networks": [], "passive": set()}
            self.mode = "router"
            return ""
        if self.mode == "if":
            i = r.interfaces[self.ctx]
            if w[0] == "description":
                return ""
            if w[:2] == ["ip", "address"] and len(w) == 4:
                i["ip"], i["mask"] = w[2], w[3]
                return ""
            if s == "ip ospf network point-to-point":
                i["p2p"] = True
                return ""
            if s == "no shutdown":
                i["shutdown"] = False
                r.configured_at = time.time()
                return f"\n*%LINK-3-UPDOWN: Interface {self.ctx}, changed state to up"
            if s == "shutdown":
                i["shutdown"] = True
                return ""
            return INVALID
        if self.mode == "router":
            if w[0] == "router-id" and len(w) == 2:
                r.ospf["rid"] = w[1]
                return ""
            if w[0] == "network" and len(w) == 5 and w[3] == "area":
                wc = int(ipaddress.IPv4Address(w[2]))
                r.ospf["networks"].append((ipaddress.ip_network(f"{w[1]}/{32 - wc.bit_length()}"), w[4]))
                r.configured_at = time.time()
                return ""
            if w[0] == "passive-interface" and len(w) == 2:
                if w[1] not in r.interfaces:
                    return INVALID
                r.ospf["passive"].add(w[1])
                return ""
            return INVALID
        return INVALID
