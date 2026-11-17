"""Full workflow against simulated IOS routers reachable over real SSH:
upload -> generate (test AI provider) -> discovery via worker -> review -> confirm mapping -> approve
-> deploy (Kafka message) -> worker (Paramiko) -> verify (OSPF FULL, routes, ping)."""
import json
import os
import threading
import time

import httpx
import pytest
import uvicorn

from fake_ios import Lab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = {"X-Admin-Token": "test-admin-token"}
PORT = 18765


@pytest.fixture(scope="module")
def env():
    lab = Lab(adjacency_delay=2.0)  # adjacency takes a moment -> exercises verification retries
    lab.add_router("R1", "127.0.0.1")
    lab.add_router("R2", "127.0.0.1")
    lab.add_host("10.0.0.10", "10.0.0.0/24", "10.0.0.1")
    lab.add_host("10.2.0.10", "10.2.0.0/24", "10.2.0.2")
    p1, p2 = lab.serve("R1"), lab.serve("R2")
    with open(os.environ["INVENTORY_PATH"], "w") as fh:
        fh.write(f"devices:\n  R1:\n    host: 127.0.0.1\n    port: {p1}\n  R2:\n    host: 127.0.0.1\n    port: {p2}\n")

    from app import kafka_producer as kp
    from app.main import app
    mem = kp.InMemoryProducer()
    kp.set_producer(mem)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    import worker
    be = worker.Backend(url=f"http://127.0.0.1:{PORT}", token="test-worker-token")
    yield {"lab": lab, "mem": mem, "proc": worker.Processor(be), "worker": worker,
           "http": httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=120)}
    server.should_exit = True


def drain(e, start=0):
    """Act as the Kafka consumer: feed every produced message to the worker."""
    i = start
    while i < len(e["mem"].messages):
        e["proc"].handle(json.dumps(e["mem"].messages[i]).encode())
        i += 1
    return i


def test_full_workflow(env):
    h = env["http"]
    assert h.get("/").status_code == 200

    # 1 upload
    with open(os.path.join(ROOT, "samples", "demo", "D1-exam-official.png"), "rb") as fh:
        plan = h.post("/api/diagrams", files={"file": ("topo.png", fh, "image/png")}).json()
    assert plan["status"] == "UPLOADED"
    bad = h.post("/api/diagrams", files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 415
    pid = plan["id"]

    # 2 generate: AI + discovery jobs on Kafka
    plan = h.post(f"/api/plans/{pid}/generate").json()
    assert plan["status"] == "ANALYZED" and plan["topology"]["routing"]["process_id"] == 1
    msgs = env["mem"].messages
    assert {m["job_type"] for m in msgs} == {"discover"} and len(msgs) == 2
    pos = drain(env)
    disc = {d["device_id"]: d for d in h.get("/api/discovery").json()}
    assert disc["R1"]["result"]["mgmt_interface"] == "GigabitEthernet0/3"
    assert disc["R1"]["data_interfaces"] == ["GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2"]

    # 3 review: suggestion made but approval blocked until mapping confirmed
    plan = h.post(f"/api/plans/{pid}/review").json()
    assert plan["status"] == "REVIEW_FAILED"
    assert any("confirmed" in e["message"] for e in plan["validation"]["errors"])
    r = h.post(f"/api/plans/{pid}/approve", json={"approver": "x", "confirm": True}, headers=ADMIN)
    assert r.status_code == 422
    assert h.put(f"/api/plans/{pid}/interface-map", json={"interface_map": plan["interface_map"],
                                                           "confirmed": True}).status_code == 401
    plan = h.put(f"/api/plans/{pid}/interface-map", headers=ADMIN,
                 json={"interface_map": plan["interface_map"], "confirmed": True}).json()
    assert plan["status"] == "REVIEWED", plan["validation"]["errors"]

    # deploy before approval is refused
    assert h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).status_code == 409
    # 4 approve
    assert h.post(f"/api/plans/{pid}/approve", json={"approver": "x", "confirm": False}, headers=ADMIN).status_code == 400
    plan = h.post(f"/api/plans/{pid}/approve", json={"approver": "Lab Admin", "confirm": True}, headers=ADMIN).json()
    assert plan["status"] == "APPROVED" and plan["approval_hash"]

    # 5 deploy -> Kafka
    dep = h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).json()
    assert len(dep["jobs"]) == 2 and all(j["status"] == "PENDING" for j in dep["jobs"])
    deploy_msg = env["mem"].messages[pos]
    assert deploy_msg["job_type"] == "deploy" and deploy_msg["config_ref"]["sha256"]
    assert "config_lines" not in deploy_msg  # message carries only a reference
    assert h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).status_code == 409  # already running

    # 6 worker: deploy, then verification jobs get queued and processed
    pos = drain(env, pos)
    dep = h.get(f"/api/deployments/{dep['id']}").json()
    statuses = {(j["job_type"], j["device_id"]): j["status"] for j in dep["jobs"]}
    assert dep["status"] == "SUCCESS", (statuses, [j["error"] for j in dep["jobs"]])
    assert statuses == {("deploy", "R1"): "SUCCESS", ("deploy", "R2"): "SUCCESS",
                        ("verify", "R1"): "SUCCESS", ("verify", "R2"): "SUCCESS"}

    # routers actually configured by the worker over SSH
    r1 = env["lab"].routers["R1"]
    assert r1.hostname == "R1" and r1.saved
    assert r1.interfaces["GigabitEthernet0/0"]["ip"] == "10.0.0.1"
    assert r1.ospf["rid"] == "1.1.1.1" and r1.ospf["passive"] == {"GigabitEthernet0/0"}
    assert r1.interfaces["GigabitEthernet0/3"]["ip"] == "127.0.0.1"  # management untouched

    ver = next(j for j in dep["jobs"] if j["job_type"] == "verify" and j["device_id"] == "R1")
    assert ver["result"]["ok"]
    assert [n["neighbor_id"] for n in ver["result"]["neighbors"] if n["full"]] == ["2.2.2.2"]
    assert [x["prefix"] for x in ver["result"]["ospf_routes"]] == ["10.2.0.0/24"]
    assert ver["result"]["ping_checks"][0]["success_rate"] == 100
    assert h.get(f"/api/plans/{pid}").json()["status"] == "DEPLOYED"

    # duplicate delivery of an already-executed message does nothing
    before = len(env["mem"].messages)
    fresh_proc = env["worker"].Processor(env["proc"].be)  # new worker, empty local cache
    fresh_proc.handle(json.dumps(deploy_msg).encode())
    job = h.get(f"/api/jobs/{deploy_msg['job_id']}").json()
    assert job["attempts"] == 1 and len(env["mem"].messages) == before


def test_tampered_config_is_refused(env):
    """Editing the plan after approval must stop the worker from deploying it."""
    h = env["http"]
    pid = h.get("/api/plans").json()[0]["id"]
    from app import db
    plan = db.one("SELECT * FROM plans WHERE id=?", (pid,))
    pos = len(env["mem"].messages)
    dep = h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).json()
    cfgs = plan["configs"]
    cfgs["R1"]["lines"].append("ip route 0.0.0.0 0.0.0.0 10.1.0.2")  # sneaky change in DB
    db.execute("UPDATE plans SET configs=? WHERE id=?", (cfgs, pid))
    drain(env, pos)
    dep = h.get(f"/api/deployments/{dep['id']}").json()
    assert dep["status"] == "FAILED"
    assert all("not authorized" in (j["error"] or "") for j in dep["jobs"] if j["job_type"] == "deploy")
    assert not any("ip route" in str(x) for x in env["lab"].routers["R1"].running_config())


def test_bad_credentials_reported(env, monkeypatch):
    h = env["http"]
    monkeypatch.setenv("ROUTER_SSH_PASSWORD", "wrong")
    pos = len(env["mem"].messages)
    jobs = h.post("/api/discovery", json={"devices": ["R1"]}).json()
    drain(env, pos)
    j = h.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert j["status"] == "FAILED" and j["error"].startswith("auth:")


def test_second_run_replaces_previous_config(env):
    """A new run over routers that already carry an earlier plan: the approved config first
    removes the old OSPF and resets the data interfaces, so nothing stale is left over."""
    h, r1 = env["http"], env["lab"].routers["R1"]
    assert r1.ospf and "10.1.0.0/24" in {str(n) for n, _ in r1.ospf["networks"]}  # from run 1
    pos = len(env["mem"].messages)
    with open(os.path.join(ROOT, "samples", "demo", "D1-exam-official.png"), "rb") as fh:
        plan = h.post("/api/diagrams", files={"file": ("topo.png", fh, "image/png")}).json()
    pid = plan["id"]
    plan = h.post(f"/api/plans/{pid}/generate").json()
    # the instructor's new plan moves the R1-R2 link to 10.9.0.0/24
    topo = json.loads(json.dumps(plan["topology"]).replace("10.1.0.", "10.9.0."))
    h.put(f"/api/plans/{pid}/topology", headers=ADMIN, json={"topology": topo})
    drain(env, pos)
    plan = h.post(f"/api/plans/{pid}/review").json()
    plan = h.put(f"/api/plans/{pid}/interface-map", headers=ADMIN,
                 json={"interface_map": plan["interface_map"], "confirmed": True}).json()
    assert plan["status"] == "REVIEWED", plan["validation"]["errors"]
    lines = plan["configs"]["R1"]["lines"]
    assert lines[:lines.index("hostname R1")] == [
        "no router ospf 1", "default interface GigabitEthernet0/0",
        "default interface GigabitEthernet0/1", "default interface GigabitEthernet0/2"]
    # nothing reaches the routers before approval
    assert h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).status_code == 409
    h.post(f"/api/plans/{pid}/approve", headers=ADMIN, json={"approver": "Lab Admin", "confirm": True})
    pos = len(env["mem"].messages)
    dep = h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).json()
    drain(env, pos)
    dep = h.get(f"/api/deployments/{dep['id']}").json()
    assert dep["status"] == "SUCCESS", [(j["job_type"], j["device_id"], j["error"]) for j in dep["jobs"]]
    assert {str(n) for n, _ in r1.ospf["networks"]} == {"10.0.0.0/24", "10.9.0.0/24"}  # old link gone
    assert r1.interfaces["GigabitEthernet0/1"]["ip"] == "10.9.0.1"
    assert r1.interfaces["GigabitEthernet0/3"]["ip"] == "127.0.0.1"  # management untouched
    assert "GigabitEthernet0/3" not in r1.defaulted


def _deploy_new_plan(env, change):
    """Upload, generate (with a topology edit), confirm, approve and deploy one plan."""
    h = env["http"]
    pos = len(env["mem"].messages)
    with open(os.path.join(ROOT, "samples", "demo", "D1-exam-official.png"), "rb") as fh:
        pid = h.post("/api/diagrams", files={"file": ("topo.png", fh, "image/png")}).json()["id"]
    plan = h.post(f"/api/plans/{pid}/generate").json()
    h.put(f"/api/plans/{pid}/topology", headers=ADMIN, json={"topology": change(plan["topology"])})
    drain(env, pos)
    plan = h.post(f"/api/plans/{pid}/review").json()
    plan = h.put(f"/api/plans/{pid}/interface-map", headers=ADMIN,
                 json={"interface_map": plan["interface_map"], "confirmed": True}).json()
    assert plan["status"] == "REVIEWED", plan["validation"]["errors"]
    h.post(f"/api/plans/{pid}/approve", headers=ADMIN, json={"approver": "Lab Admin", "confirm": True})
    pos = len(env["mem"].messages)
    dep = h.post(f"/api/plans/{pid}/deploy", headers=ADMIN).json()
    drain(env, pos)
    return plan, h.get(f"/api/deployments/{dep['id']}").json()


def test_next_plan_removes_ospf_process_of_previous_deploy(env):
    """The router list read before a deploy must not be reused after it: the next plan
    would not know the OSPF process that deploy added, and would leave it running."""
    def process_7(topo):
        topo["routing"]["process_id"] = 7
        return topo
    _, dep = _deploy_new_plan(env, process_7)
    assert dep["status"] == "SUCCESS"
    assert env["lab"].routers["R1"].ospf["pid"] == 7
    plan, dep = _deploy_new_plan(env, lambda topo: topo)  # back to process 1, right away
    lines = plan["configs"]["R1"]["lines"]
    assert {"no router ospf 1", "no router ospf 7"} <= set(lines[:lines.index("hostname R1")])
    assert dep["status"] == "SUCCESS"


def test_generate_uses_requirements_pasted_after_upload(env):
    h = env["http"]
    with open(os.path.join(ROOT, "samples", "demo", "D1-exam-official.png"), "rb") as fh:
        plan = h.post("/api/diagrams", files={"file": ("topo.png", fh, "image/png")}).json()
    assert plan["requirements"] == ""
    text = open(os.path.join(ROOT, "tests", "fixtures", "exam-requirements.txt"), encoding="utf-8").read()
    plan = h.post(f"/api/plans/{plan['id']}/generate", json={"requirements": text}).json()
    assert plan["requirements"] == text and plan["status"] == "ANALYZED"
    plan = h.post(f"/api/plans/{plan['id']}/generate").json()  # no body: keeps the stored text
    assert plan["requirements"] == text
