"""Set a lab PC (GNS3 VPCS) through its console: telnet to the GNS3 server's console port.

Only used for PCs listed under `pcs:` in the inventory, and only with the approved
`ip <address>/<prefix> <gateway>` line of the plan."""
import re
import socket
import time


def _strip_telnet(data: bytes) -> str:
    """Drop telnet option negotiation bytes (IAC ...) and decode the rest."""
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == 255 and i + 1 < len(data):
            i += 3 if data[i + 1] in (251, 252, 253, 254) else 2
            continue
        out.append(data[i])
        i += 1
    return out.decode("utf-8", "replace")


class PCConsole:
    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(0.5)
        self.read(1.0)  # whatever is on the console already

    def read(self, seconds: float) -> str:
        data, end = b"", time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.sock.recv(65535)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                pass
        return _strip_telnet(data)

    def send(self, line: str, wait: float = 2.5) -> str:
        self.sock.sendall(line.encode() + b"\r\n")
        return self.read(wait)

    def close(self) -> None:
        self.sock.close()


def parse_show_ip(text: str) -> dict:
    """VPCS `show ip`: IP/MASK and GATEWAY."""
    ip = re.search(r"IP/MASK\s*:\s*([\d.]+)/(\d+)", text)
    gw = re.search(r"GATEWAY\s*:\s*([\d.]+)", text)
    return {"ip": ip.group(1) if ip else None, "prefix": int(ip.group(2)) if ip else None,
            "gateway": gw.group(1) if gw else None}
