"""Python Worker: Kafka consumer -> approval re-check -> Paramiko SSH -> results.

Security properties
* Device addresses come from the worker's own inventory file, credentials from the
  worker's environment; a Kafka message can only name a device_id.
* Before touching a router the worker asks the backend to (1) atomically claim the job
  (duplicate prevention) and (2) re-confirm the approval and return the approved config,
  whose SHA-256 must match the reference in the Kafka message.
* Discovery and verification only run fixed read-only show commands defined here.
"""
import hashlib
import json
import logging
import os
import signal
import socket
import time
import uuid
from collections import OrderedDict

import httpx
import yaml

from parsers import (evaluate_verification, parse_cdp_neighbors, parse_ip_int_brief,
                     parse_ip_interface_address, parse_ospf_processes, parse_ping)
from ssh_client import DeviceError, IOSSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s worker: %(message)s")
log = logging.getLogger("worker")


def env(name, default=None):
    f = os.getenv(f"{name}_FILE")
    if f and os.path.exists(f):
        return open(f, encoding="utf-8").read().strip()
    v = os.getenv(name)
    return v if v not in (None, "") else default


BACKEND_URL = env("BACKEND_URL", "http://backend:8000").rstrip("/")
WORKER_TOKEN = env("WORKER_TOKEN")
WORKER_ID = env("WORKER_ID", f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}")
INVENTORY_PATH = env("INVENTORY_PATH", "/config/devices.yaml")
DATA_DIR = env("DATA_DIR", "/data")
SSH_TIMEOUT = float(env("SSH_TIMEOUT", "15"))
# 5 retries (2+4+...+10 s, ~30 s): an emulated router (dynamips/IOSv) can refuse SSH for
# several seconds right after a deploy while it releases the previous VTY sessions.
SSH_RETRIES = int(env("SSH_RETRIES", "5"))
HOST_KEY_POLICY = env("SSH_HOST_KEY_POLICY", "accept-new")
VERIFY_TIMEOUT = float(env("VERIFY_TIMEOUT", "180"))
VERIFY_INTERVAL = float(env("VERIFY_INTERVAL", "10"))
SCHEMA = "network-config-job/v1"

READ_ONLY_DISCOVERY = ["show ip interface brief", "show cdp neighbors", "show version | include IOS|uptime",
                       "show ip protocols"]
VERIFY_COMMANDS = ["show ip ospf neighbor", "show ip route ospf", "show ip protocols"]


class Backend:
    def __init__(self, url=BACKEND_URL, token=WORKER_TOKEN, client=None):
        self.url = url
        self.http = client or httpx.Client(base_url=url, timeout=20,
                                           headers={"X-Worker-Token": token or ""})

    def _call(self, method, path, **kw):
        last = None
        for attempt in range(5):
            try:
                r = self.http.request(method, path, **kw)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError("server error", request=r.request, response=r)
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"backend unreachable: {last}")

    def claim(self, job_id):
        r = self._call("POST", f"/api/worker/jobs/{job_id}/claim", json={"worker_id": WORKER_ID})
        if r.status_code == 404:
            return {"claimed": False, "reason": "unknown job"}
        r.raise_for_status()
        return r.json()

    def authorization(self, job_id):
        r = self._call("GET", f"/api/worker/jobs/{job_id}/authorization", params={"worker_id": WORKER_ID})
        r.raise_for_status()
        return r.json()

    def event(self, job_id, message, level="info"):
        log.info("[%s] %s", job_id, message)
        try:
            self._call("POST", f"/api/worker/jobs/{job_id}/events",
                       json={"worker_id": WORKER_ID, "message": message, "level": level})
        except Exception as exc:  # progress reporting must never kill a job
            log.warning("could not report event: %s", exc)

    def result(self, job_id, status, output="", result=None, error=None):
        r = self._call("POST", f"/api/worker/jobs/{job_id}/result",
                       json={"worker_id": WORKER_ID, "status": status, "output": output,
                             "result": result, "error": error})
        if r.status_code >= 400:
            log.error("result rejected for %s: %s", job_id, r.text)


def load_inventory():
    with open(INVENTORY_PATH, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("devices") or {}


def credentials(device_id: str) -> dict:
    key = device_id.upper().replace("-", "_")
    user = env(f"{key}_SSH_USERNAME") or env("ROUTER_SSH_USERNAME")
    pw = env(f"{key}_SSH_PASSWORD") or env("ROUTER_SSH_PASSWORD")
    if not user or not pw:
        raise DeviceError("router SSH credentials are not configured in the worker environment")
    return {"username": user, "password": pw,
            "enable_secret": env(f"{key}_ENABLE_SECRET") or env("ROUTER_ENABLE_SECRET")}


def open_session(device_id: str, inv: dict) -> IOSSession:
    d = inv.get(device_id)
    if not d or not d.get("host"):
        raise DeviceError(f"{device_id} is not in the worker inventory")
    last = None
    for attempt in range(SSH_RETRIES + 1):
        try:
            return IOSSession(str(d["host"]), int(d.get("port", 22)), timeout=SSH_TIMEOUT,
                              host_key_policy=HOST_KEY_POLICY,
                              known_hosts=os.path.join(DATA_DIR, "known_hosts"),
                              **credentials(device_id)).connect()
        except DeviceError as exc:
            last = exc
            if exc.kind == "auth":
                raise  # retrying bad credentials only risks a lockout
            time.sleep(2 * (attempt + 1))
    raise last


# ------------------------------------------------------------------ job handlers
def do_discover(job, auth, be, inv):
    dev = job["device_id"]
    out = []
    with open_session(dev, inv) as s:
        be.event(job["job_id"], f"SSH connected to {dev} ({inv[dev]['host']}) - read-only discovery")
        res = {}
        for cmd in READ_ONLY_DISCOVERY:
            res[cmd] = s.send(cmd)
            out.append(f"{dev}# {cmd}\n{res[cmd]}")
        interfaces = parse_ip_int_brief(res["show ip interface brief"])
        mgmt = inv[dev].get("mgmt_interface") or next(
            (i["name"] for i in interfaces if i["ip"] == str(inv[dev]["host"])), None)
        if not mgmt:
            with_ip = [i["name"] for i in interfaces if i["ip"]]
            mgmt = with_ip[0] if len(with_ip) == 1 else None
        mgmt_net = None
        if mgmt:
            txt = s.send(f"show ip interface {mgmt}")
            out.append(f"{dev}# show ip interface {mgmt}\n{txt}")
            mgmt_net = parse_ip_interface_address(txt)
    result = {"interfaces": interfaces, "mgmt_interface": mgmt, "mgmt_network": mgmt_net,
              "cdp": parse_cdp_neighbors(res["show cdp neighbors"]),
              # OSPF processes already running: the next plan's config removes them first
              "ospf_processes": parse_ospf_processes(res["show ip protocols"]),
              "version": res["show version | include IOS|uptime"][:500]}
    if not mgmt:
        return "FAILED", "\n\n".join(out), result, "could not identify the management interface"
    be.event(job["job_id"], f"found {len(interfaces)} interfaces, management = {mgmt} ({mgmt_net})")
    return "SUCCESS", "\n\n".join(out), result, None


def do_deploy(job, auth, be, inv):
    dev, jid = job["device_id"], job["job_id"]
    lines = auth["config_lines"]
    out = []
    os.makedirs(os.path.join(DATA_DIR, "backups"), exist_ok=True)
    with open_session(dev, inv) as s:
        be.event(jid, f"SSH connected to {dev}; approved by {auth.get('approved_by')}")
        backup = s.send("show running-config", timeout=60)
        path = os.path.join(DATA_DIR, "backups", f"{dev}-{jid}-pre.cfg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(backup)
        be.event(jid, f"pre-change running-config backed up ({len(backup)} bytes) to {path}")
        be.event(jid, f"applying {len(lines)} approved configuration lines")
        cfg_log, errors = s.configure(lines)
        out.append(cfg_log)
        if errors:
            out.append("\n".join(errors))
            return "FAILED", "\n".join(out), {"backup_file": path, "cli_errors": errors}, \
                f"IOS rejected a command: {errors[0]}. Running config was NOT saved; " \
                f"recover with the backup in {path}"
        saved = s.save()
        out.append(f"write memory\n{saved}")
        be.event(jid, "configuration applied and saved (write memory)")
        brief = s.send("show ip interface brief")
        out.append(f"show ip interface brief\n{brief}")
    # management survived? open a fresh SSH session to prove it
    with open_session(dev, inv) as s2:
        s2.send("show clock")
    be.event(jid, "post-change management SSH check passed")
    return "SUCCESS", "\n\n".join(out), {"backup_file": path,
                                         "interfaces": parse_ip_int_brief(brief)}, None


def do_verify(job, auth, be, inv):
    dev, jid = job["device_id"], job["job_id"]
    exp = auth["expected"]
    deadline = time.time() + VERIFY_TIMEOUT
    attempt, evaluation, raw = 0, None, {}
    while True:
        attempt += 1
        with open_session(dev, inv) as s:
            raw = {cmd: s.send(cmd) for cmd in VERIFY_COMMANDS}
            evaluation = evaluate_verification(exp, *[raw[c] for c in VERIFY_COMMANDS])
            if evaluation["ok"]:
                pings = []
                for p in exp.get("ping_checks", []):
                    if not p.get("source"):
                        continue
                    txt = s.send(f"ping {p['target']} source {p['source']} repeat 5 timeout 1", timeout=30)
                    pings.append({**p, **parse_ping(txt), "output": txt})
                evaluation["ping_checks"] = pings
        failed = [c["name"] for c in evaluation["checks"] if not c["ok"]]
        be.event(jid, f"verification attempt {attempt}: "
                      + ("all checks passed" if not failed else f"waiting for: {', '.join(failed)}"))
        if evaluation["ok"] or time.time() + VERIFY_INTERVAL > deadline:
            break
        time.sleep(VERIFY_INTERVAL)
    output = "\n\n".join(f"{dev}# {c}\n{raw[c]}" for c in VERIFY_COMMANDS)
    for p in evaluation.get("ping_checks", []):
        output += f"\n\n{dev}# ping {p['target']} source {p['source']}\n{p['output']}"
        rate = p.get("success_rate")
        be.event(jid, f"router ping {p['source']} -> {p['target']} ({p['host']}): "
                      f"{'%s%%' % rate if rate is not None else 'no result'} (informational)")
    if evaluation["ok"]:
        return "SUCCESS", output, evaluation, None
    return "FAILED", output, evaluation, f"verification failed after {attempt} attempts: {failed}"


HANDLERS = {"discover": do_discover, "deploy": do_deploy, "verify": do_verify}


# ------------------------------------------------------------------ message processing
class Processor:
    def __init__(self, backend: Backend | None = None):
        self.be = backend or Backend()
        self.seen: OrderedDict = OrderedDict()  # local guard for redelivered messages

    def handle(self, payload: bytes) -> None:
        try:
            job = json.loads(payload)
        except (ValueError, TypeError):
            log.error("discarding non-JSON message")
            return
        if job.get("schema") != SCHEMA or job.get("job_type") not in HANDLERS or not job.get("job_id"):
            log.error("discarding message with unknown schema/type: %s", str(job)[:200])
            return
        jid = job["job_id"]
        if jid in self.seen:
            log.info("[%s] duplicate delivery ignored (already processed here)", jid)
            return
        self.seen[jid] = time.time()
        while len(self.seen) > 5000:
            self.seen.popitem(last=False)

        claim = self.be.claim(jid)
        if not claim.get("claimed"):
            log.info("[%s] not claimed: %s - skipping (duplicate prevention)", jid, claim.get("reason"))
            return
        try:
            auth = self.be.authorization(jid)
        except Exception as exc:
            self.be.result(jid, "FAILED", error=f"approval re-check failed: {exc}")
            return
        if not auth.get("authorized"):
            self.be.event(jid, f"REFUSED: {auth.get('reason')}", "error")
            self.be.result(jid, "FAILED", error=f"not authorized: {auth.get('reason')}")
            return
        if auth.get("device_id") != job.get("device_id"):
            self.be.result(jid, "FAILED", error="device mismatch between Kafka message and backend")
            return
        if job["job_type"] in ("deploy", "verify"):
            ref = (job.get("config_ref") or {}).get("sha256")
            if not ref or ref != auth.get("config_sha256"):
                self.be.result(jid, "FAILED", error="config reference in Kafka message does not match approved config")
                return
            if job["job_type"] == "deploy":
                lines = auth.get("config_lines") or []
                digest = hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()
                if digest != ref:
                    self.be.result(jid, "FAILED", error="received config does not hash to the approved sha256")
                    return
            self.be.event(jid, f"approval re-confirmed with backend (config sha256 {ref[:12]}...)")
        try:
            status, output, result, error = HANDLERS[job["job_type"]](job, auth, self.be, load_inventory())
        except DeviceError as exc:
            status, output, result, error = "FAILED", "", {"error_kind": exc.kind}, f"{exc.kind}: {exc}"
        except Exception as exc:  # never leave a job stuck in RUNNING
            log.exception("job %s crashed", jid)
            status, output, result, error = "FAILED", "", None, f"worker error: {exc}"
        self.be.result(jid, status, output=output, result=result, error=error)


def main():
    from confluent_kafka import Consumer, KafkaException
    if not WORKER_TOKEN:
        raise SystemExit("WORKER_TOKEN is not set")
    consumer = Consumer({
        "bootstrap.servers": env("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        "group.id": env("KAFKA_GROUP_ID", "netauto-workers"),
        "client.id": WORKER_ID,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "max.poll.interval.ms": 900000,
    })
    topic = env("KAFKA_TOPIC", "network-config-jobs")
    consumer.subscribe([topic])
    proc = Processor()
    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker %s consuming %s", WORKER_ID, topic)
    try:
        while running:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                log.warning("kafka: %s", msg.error())
                continue
            log.info("message partition=%s offset=%s key=%s", msg.partition(), msg.offset(),
                     (msg.key() or b"").decode())
            proc.handle(msg.value())
            try:
                consumer.commit(message=msg, asynchronous=False)  # at-least-once + claim = no duplicates
            except KafkaException as exc:
                log.warning("commit failed: %s", exc)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
