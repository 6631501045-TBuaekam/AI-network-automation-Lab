"""Device inventory (management addresses only - credentials live in the worker's env)."""
import re

import yaml

from .config import settings


def load_inventory(path: str | None = None) -> dict:
    with open(path or settings.inventory_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    devices = data.get("devices") or {}
    out = {}
    for dev_id, d in devices.items():
        d = d or {}
        out[str(dev_id)] = {
            "id": str(dev_id),
            "host": str(d.get("host", "")),
            "port": int(d.get("port", 22)),
            "platform": d.get("platform", "cisco_ios"),
            "mgmt_interface": d.get("mgmt_interface"),
            "interface_hints": d.get("interface_hints") or {},
        }
    return out


def load_pcs(path: str | None = None) -> dict:
    """Optional `pcs:` section: the GNS3 console of each lab PC (VPCS). When a PC is listed,
    Deploy also sets its address, so every plan works without typing on the PCs."""
    try:
        with open(path or settings.inventory_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    out = {}
    for pc_id, d in (data.get("pcs") or {}).items():
        d = d or {}
        if d.get("console_host") and d.get("console_port"):
            out[str(pc_id)] = {"console_host": str(d["console_host"]), "console_port": int(d["console_port"])}
    return out


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    m = re.fullmatch(r"router(\d+)", n)
    return f"r{m.group(1)}" if m else n


def match_inventory(device_id: str, label: str | None, inventory: dict) -> str | None:
    """Map a device named in the diagram (e.g. 'R1' / 'router1') to an inventory id."""
    by_norm = {_norm(k): k for k in inventory}
    for candidate in (device_id, label):
        if candidate and _norm(candidate) in by_norm:
            return by_norm[_norm(candidate)]
    return None
