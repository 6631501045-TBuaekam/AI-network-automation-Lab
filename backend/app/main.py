"""AI Network Automation System - backend API (FastAPI).

Workflow: Upload -> Generate (AI analysis + discovery) -> Review (configs + validation)
-> Approve (admin) -> Deploy (Kafka) -> Worker (Paramiko) -> Verify (OSPF + ping).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from . import kafka_producer as kp
from .ai import AIError, analyze_diagram
from .config import settings
from .generator import data_interfaces, generate_configs, suggest_mapping
from .gns3 import lab_check
from .inventory import load_inventory
from .topology import build_model
from .validator import validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("netauto.api")

@asynccontextmanager
async def lifespan(_app):
    os.makedirs(settings.upload_dir, exist_ok=True)
    db.init_db()
    if not settings.admin_token or not settings.worker_token:
        log.warning("ADMIN_TOKEN / WORKER_TOKEN not set - approval and worker APIs will refuse requests")
    yield


app = FastAPI(title="AI Network Automation System", version="1.0.0", lifespan=lifespan)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class NoCacheStatic(StaticFiles):
    """Browsers must re-check the page files, so an update is never hidden by a cached app.js."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

JOB_SCHEMA = "network-config-job/v1"
ACTIVE = ("PENDING", "RUNNING")


# ------------------------------------------------------------------ auth helpers
def _check(token: str | None, expected: str | None, what: str) -> None:
    if not expected:
        raise HTTPException(503, f"{what} token is not configured on the server")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, f"invalid {what} token")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    _check(x_admin_token, settings.admin_token, "admin")


def require_worker(x_worker_token: str | None = Header(default=None)) -> None:
    _check(x_worker_token, settings.worker_token, "worker")


# ------------------------------------------------------------------ helpers
def _plan(plan_id: str) -> dict:
    p = db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not p:
        raise HTTPException(404, "plan not found")
    return p


def _job(job_id: str) -> dict:
    j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise HTTPException(404, "job not found")
    return j


def _inventory() -> dict:
    try:
        return load_inventory()
    except FileNotFoundError:
        raise HTTPException(500, f"inventory file not found: {settings.inventory_path}")


def latest_discoveries() -> dict:
    rows = db.query("SELECT * FROM jobs WHERE job_type='discover' AND status='SUCCESS' "
                    "ORDER BY finished_at DESC")
    out: dict = {}
    for r in rows:
        if r["device_id"] not in out and isinstance(r.get("result"), dict):
            d = dict(r["result"])
            d["_finished_at"] = r["finished_at"]
            d["_job_id"] = r["id"]
            out[r["device_id"]] = d
    return out


def config_digest(lines: list) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def approval_hash(plan_id: str, configs: dict) -> str:
    material = json.dumps({"plan": plan_id, "configs": {k: v["sha256"] for k, v in sorted(configs.items())}},
                          sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


def dispatch(job_type: str, device_id: str, plan_id: str | None = None,
             deployment_id: str | None = None, config_sha256: str | None = None,
             approval: str | None = None) -> dict:
    job_id = db.new_id("job")
    ts = db.now()
    db.execute("INSERT INTO jobs(id, job_type, plan_id, deployment_id, device_id, status, "
               "config_sha256, created_at) VALUES(?,?,?,?,?,?,?,?)",
               (job_id, job_type, plan_id, deployment_id, device_id, "PENDING", config_sha256, ts))
    message = {
        "schema": JOB_SCHEMA, "job_id": job_id, "job_type": job_type, "device_id": device_id,
        "plan_id": plan_id, "deployment_id": deployment_id, "created_at": ts,
        "config_ref": ({"url": f"{settings.public_base_url}/api/plans/{plan_id}/configs/{device_id}",
                        "sha256": config_sha256} if config_sha256 else None),
        "approval_ref": ({"plan_id": plan_id, "approval_hash": approval} if approval else None),
    }
    try:
        meta = kp.producer.send(message)
        db.execute("UPDATE jobs SET kafka_meta=? WHERE id=?", ({**meta, "message": message}, job_id))
        db.add_event(job_id, f"queued on Kafka {meta.get('topic')} partition {meta.get('partition')} "
                             f"offset {meta.get('offset')}")
    except Exception as exc:  # broker down etc.
        db.execute("UPDATE jobs SET status='FAILED', error=?, finished_at=? WHERE id=?",
                   (f"Kafka dispatch failed: {exc}", db.now(), job_id))
        db.add_event(job_id, f"Kafka dispatch failed: {exc}", "error")
    return _job(job_id)


def _with_events(job: dict) -> dict:
    job["events"] = db.query("SELECT ts, level, message FROM job_events WHERE job_id=? ORDER BY id",
                             (job["id"],))
    return job


# ------------------------------------------------------------------ basic endpoints
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"status": "ok", "ai_provider": settings.ai_provider, "kafka_topic": settings.kafka_topic,
            "kafka_enabled": settings.kafka_enabled,
            "gns3": ({"project": settings.gns3_project} if settings.gns3_enabled else None),
            "time": time.time()}


@app.get("/api/inventory")
def inventory():
    inv = _inventory()
    return {"devices": [{"id": d["id"], "host": d["host"], "port": d["port"]} for d in inv.values()]}


# ------------------------------------------------------------------ 1. upload
@app.post("/api/diagrams")
async def upload_diagram(file: UploadFile = File(...), requirements: str = Form(default="")):
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"image larger than {settings.max_upload_mb} MB")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        media, ext = "image/png", "png"
    elif data[:3] == b"\xff\xd8\xff":
        media, ext = "image/jpeg", "jpg"
    else:
        raise HTTPException(415, "only PNG or JPG images are accepted")
    diagram_id = db.new_id("dia")
    path = os.path.join(settings.upload_dir, f"{diagram_id}.{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    ts = db.now()
    db.execute("INSERT INTO diagrams VALUES(?,?,?,?,?,?)",
               (diagram_id, os.path.basename(file.filename or "diagram"), path,
                hashlib.sha256(data).hexdigest(), media, ts))
    plan_id = db.new_id("plan")
    db.execute("INSERT INTO plans(id, diagram_id, status, requirements, created_at, updated_at) "
               "VALUES(?,?,?,?,?,?)", (plan_id, diagram_id, "UPLOADED", requirements[:4000], ts, ts))
    return _plan(plan_id)


@app.get("/api/diagrams/{diagram_id}/image")
def diagram_image(diagram_id: str):
    d = db.one("SELECT * FROM diagrams WHERE id=?", (diagram_id,))
    if not d:
        raise HTTPException(404, "diagram not found")
    return FileResponse(d["path"], media_type=d["content_type"])


@app.get("/api/plans")
def list_plans(limit: int = 20):
    return db.query("SELECT id, diagram_id, status, created_at, updated_at, approved_by, approved_at "
                    "FROM plans ORDER BY created_at DESC LIMIT ?", (min(limit, 100),))


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str):
    return _plan(plan_id)


# ------------------------------------------------------------------ 2. generate (AI)
@app.post("/api/plans/{plan_id}/generate")
def generate(plan_id: str):
    plan = _plan(plan_id)
    if plan["status"] in ("DEPLOYING",):
        raise HTTPException(409, "plan is being deployed")
    d = db.one("SELECT * FROM diagrams WHERE id=?", (plan["diagram_id"],))
    with open(d["path"], "rb") as fh:
        image = fh.read()
    try:
        res = analyze_diagram(image, d["content_type"], plan.get("requirements"))
    except AIError as exc:
        raise HTTPException(502, f"AI analysis failed: {exc}")
    db.execute("UPDATE plans SET status='ANALYZED', ai_provider=?, ai_model=?, ai_raw=?, topology=?, "
               "topology_source='ai', interface_map=NULL, mapping_confirmed=0, configs=NULL, "
               "validation=NULL, approval_hash=NULL, approved_by=NULL, approved_at=NULL, updated_at=? "
               "WHERE id=?",
               (res["provider"], res["model"], res["raw"][:200000], res["topology"], db.now(), plan_id))
    # read-only discovery of real interface names (through Kafka + worker, never by the AI)
    _ensure_discovery(res["topology"])
    return _plan(plan_id)


def _ensure_discovery(topology: dict) -> list:
    inv = _inventory()
    model, _ = build_model(topology, inv)
    fresh = latest_discoveries()
    jobs = []
    for r in model["routers"].values():
        dev = r.get("inventory_id")
        if not dev:
            continue
        recent = fresh.get(dev)
        if recent and time.time() - recent["_finished_at"] < settings.discovery_max_age_min * 60:
            continue
        if db.one("SELECT id FROM jobs WHERE job_type='discover' AND device_id=? AND status IN ('PENDING','RUNNING')",
                  (dev,)):
            continue
        jobs.append(dispatch("discover", dev))
    return jobs


class DiscoveryRequest(BaseModel):
    devices: list[str] | None = None


@app.post("/api/discovery")
def run_discovery(req: DiscoveryRequest):
    inv = _inventory()
    devices = req.devices or list(inv)
    unknown = [d for d in devices if d not in inv]
    if unknown:
        raise HTTPException(400, f"not in inventory: {unknown}")
    return [dispatch("discover", d) for d in devices]


@app.get("/api/discovery")
def get_discovery():
    inv = _inventory()
    latest = latest_discoveries()
    out = []
    for dev in inv:
        last_job = db.one("SELECT id, status, created_at, finished_at, error FROM jobs "
                          "WHERE job_type='discover' AND device_id=? ORDER BY created_at DESC LIMIT 1", (dev,))
        d = latest.get(dev)
        out.append({"device_id": dev, "host": inv[dev]["host"], "last_job": last_job,
                    "result": d, "data_interfaces": data_interfaces(d) if d else []})
    return out


# ------------------------------------------------------------------ 3. review
class TopologyEdit(BaseModel):
    topology: dict


@app.put("/api/plans/{plan_id}/topology", dependencies=[Depends(require_admin)])
def edit_topology(plan_id: str, body: TopologyEdit):
    _plan(plan_id)
    db.execute("UPDATE plans SET topology=?, topology_source='admin-edited', interface_map=NULL, "
               "mapping_confirmed=0, configs=NULL, validation=NULL, approval_hash=NULL, approved_by=NULL, "
               "approved_at=NULL, status='ANALYZED', updated_at=? WHERE id=?",
               (body.topology, db.now(), plan_id))
    _ensure_discovery(body.topology)
    return _plan(plan_id)


class MappingEdit(BaseModel):
    interface_map: dict
    confirmed: bool = False


@app.put("/api/plans/{plan_id}/interface-map", dependencies=[Depends(require_admin)])
def edit_mapping(plan_id: str, body: MappingEdit):
    plan = _plan(plan_id)
    if not plan.get("topology"):
        raise HTTPException(409, "run Generate first")
    db.execute("UPDATE plans SET interface_map=?, mapping_confirmed=?, updated_at=? WHERE id=?",
               (body.interface_map, 1 if body.confirmed else 0, db.now(), plan_id))
    return review(plan_id)


@app.post("/api/plans/{plan_id}/review")
def review(plan_id: str):
    plan = _plan(plan_id)
    if not plan.get("topology"):
        raise HTTPException(409, "run Generate first")
    inv = _inventory()
    model, issues = build_model(plan["topology"], inv)
    discoveries = latest_discoveries()
    imap = plan.get("interface_map") or {}
    confirmed = bool(plan.get("mapping_confirmed"))
    notes: list = []
    # read-only GNS3 cross-check between AI analysis and the discovered mapping (optional)
    lab = lab_check(model)
    if lab is not None:
        issues = issues + lab["issues"]
    complete = all(all(i["network"] in (imap.get(rid) or {}) for i in r["interfaces"])
                   for rid, r in model["routers"].items())
    if not imap or not complete:
        imap, notes = suggest_mapping(model, discoveries, inv, lab["ports"] if lab else None)
        confirmed = False
    configs = generate_configs(model, imap, plan_id, settings.ospf_transit_p2p, discoveries)
    result = validate(model, issues, imap, confirmed, configs, discoveries, inv,
                      settings.discovery_max_age_min, lab)
    result["mapping_notes"] = notes
    result["model"] = model
    result["gns3"] = ({"notes": lab["notes"], "summary": lab["summary"], "ports": lab["ports"]}
                      if lab is not None else None)
    status = "REVIEWED" if result["ok"] else "REVIEW_FAILED"
    keep_approval = (plan.get("approval_hash") and configs and result["ok"]
                     and approval_hash(plan_id, configs) == plan["approval_hash"])
    if keep_approval and plan["status"] in ("APPROVED", "DEPLOYING", "DEPLOYED", "DEPLOY_FAILED"):
        status = plan["status"]
        db.execute("UPDATE plans SET interface_map=?, mapping_confirmed=?, configs=?, validation=?, "
                   "status=?, updated_at=? WHERE id=?",
                   (imap, int(confirmed), configs, result, status, db.now(), plan_id))
    else:
        db.execute("UPDATE plans SET interface_map=?, mapping_confirmed=?, configs=?, validation=?, "
                   "status=?, approval_hash=NULL, approved_by=NULL, approved_at=NULL, updated_at=? WHERE id=?",
                   (imap, int(confirmed), configs, result, status, db.now(), plan_id))
    out = _plan(plan_id)
    out["discovery"] = {k: {"mgmt_interface": v.get("mgmt_interface"),
                            "data_interfaces": data_interfaces(v)} for k, v in discoveries.items()}
    return out


@app.get("/api/plans/{plan_id}/configs/{device_id}", response_class=PlainTextResponse)
def get_config(plan_id: str, device_id: str):
    cfg = (_plan(plan_id).get("configs") or {}).get(device_id)
    if not cfg:
        raise HTTPException(404, "no configuration for that device")
    return "\n".join(cfg["lines"]) + "\n"


# ------------------------------------------------------------------ 4. approve
class Approval(BaseModel):
    approver: str
    confirm: bool
    note: str = ""


@app.post("/api/plans/{plan_id}/approve", dependencies=[Depends(require_admin)])
def approve(plan_id: str, body: Approval):
    if not body.confirm or not body.approver.strip():
        raise HTTPException(400, "explicit confirmation and approver name are required")
    plan = review(plan_id)  # always re-validate against current discovery before approving
    if not plan["validation"]["ok"]:
        raise HTTPException(422, {"message": "validation failed - cannot approve",
                                  "errors": plan["validation"]["errors"]})
    h = approval_hash(plan_id, plan["configs"])
    db.execute("UPDATE plans SET status='APPROVED', approval_hash=?, approved_by=?, approved_at=?, "
               "approval_note=?, updated_at=? WHERE id=?",
               (h, body.approver.strip()[:80], db.now(), body.note[:500], db.now(), plan_id))
    return _plan(plan_id)


# ------------------------------------------------------------------ 5. deploy (Kafka producer)
@app.post("/api/plans/{plan_id}/deploy", dependencies=[Depends(require_admin)])
def deploy(plan_id: str):
    plan = _plan(plan_id)
    if plan["status"] not in ("APPROVED", "DEPLOYED", "DEPLOY_FAILED") or not plan.get("approval_hash"):
        raise HTTPException(409, "plan is not approved")
    if approval_hash(plan_id, plan["configs"]) != plan["approval_hash"]:
        raise HTTPException(409, "configuration changed after approval - review and approve again")
    if db.one("SELECT id FROM deployments WHERE plan_id=? AND status IN ('RUNNING','VERIFYING')", (plan_id,)):
        raise HTTPException(409, "a deployment of this plan is already in progress")
    dep_id = db.new_id("dep")
    db.execute("INSERT INTO deployments VALUES(?,?,?,?,?)", (dep_id, plan_id, "RUNNING", db.now(), None))
    db.execute("UPDATE plans SET status='DEPLOYING', updated_at=? WHERE id=?", (db.now(), plan_id))
    jobs = [dispatch("deploy", dev, plan_id, dep_id, cfg["sha256"], plan["approval_hash"])
            for dev, cfg in sorted(plan["configs"].items())]
    if any(j["status"] == "FAILED" for j in jobs):
        _finish_deployment(dep_id, "FAILED")
    return get_deployment(dep_id)


def _finish_deployment(dep_id: str, status: str) -> None:
    dep = db.one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    if not dep or dep["status"] in ("SUCCESS", "FAILED"):
        return
    db.execute("UPDATE deployments SET status=?, finished_at=? WHERE id=?", (status, db.now(), dep_id))
    db.execute("UPDATE plans SET status=?, updated_at=? WHERE id=?",
               ("DEPLOYED" if status == "SUCCESS" else "DEPLOY_FAILED", db.now(), dep["plan_id"]))


@app.get("/api/deployments/{dep_id}")
def get_deployment(dep_id: str):
    dep = db.one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    if not dep:
        raise HTTPException(404, "deployment not found")
    dep["jobs"] = [_with_events(j) for j in
                   db.query("SELECT * FROM jobs WHERE deployment_id=? ORDER BY created_at", (dep_id,))]
    return dep


# ------------------------------------------------------------------ 9. monitoring
@app.get("/api/jobs")
def list_jobs(plan_id: str | None = None, limit: int = 50):
    if plan_id:
        rows = db.query("SELECT * FROM jobs WHERE plan_id=? OR (job_type='discover' AND created_at >= "
                        "(SELECT created_at FROM plans WHERE id=?)) ORDER BY created_at DESC LIMIT ?",
                        (plan_id, plan_id, min(limit, 200)))
    else:
        rows = db.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))
    return [_with_events(j) for j in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _with_events(_job(job_id))


# ------------------------------------------------------------------ worker API (6.)
class Claim(BaseModel):
    worker_id: str


@app.post("/api/worker/jobs/{job_id}/claim", dependencies=[Depends(require_worker)])
def claim(job_id: str, body: Claim):
    """Atomic PENDING->RUNNING transition. A second consumer of the same message gets
    claimed=false, which is how duplicate execution is prevented."""
    with db.transaction() as conn:
        row = conn.execute("SELECT status, lease_until, worker_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        status, lease, owner = row["status"], row["lease_until"], row["worker_id"]
        now = time.time()
        stale = status == "RUNNING" and lease is not None and lease < now
        if status != "PENDING" and not stale:
            return {"claimed": False, "reason": f"job is {status}" + (f" (owner {owner})" if owner else "")}
        conn.execute("UPDATE jobs SET status='RUNNING', worker_id=?, started_at=?, attempts=attempts+1, "
                     "lease_until=? WHERE id=?",
                     (body.worker_id, now, now + settings.job_lease_seconds, job_id))
    db.add_event(job_id, f"claimed by worker {body.worker_id}" + (" (stale lease recovered)" if stale else ""))
    return {"claimed": True}


@app.get("/api/worker/jobs/{job_id}/authorization", dependencies=[Depends(require_worker)])
def authorization(job_id: str, worker_id: str):
    job = _job(job_id)
    deny = lambda reason: {"authorized": False, "reason": reason}  # noqa: E731
    if job["status"] != "RUNNING" or job["worker_id"] != worker_id:
        return deny("job is not running under this worker")
    base = {"authorized": True, "job_type": job["job_type"], "device_id": job["device_id"]}
    if job["job_type"] == "discover":
        return base  # read-only commands, fixed inside the worker
    plan = db.one("SELECT * FROM plans WHERE id=?", (job["plan_id"],))
    if not plan or not plan.get("approval_hash") or not plan.get("approved_by"):
        return deny("plan is not approved")
    if plan["status"] not in ("APPROVED", "DEPLOYING", "DEPLOYED", "DEPLOY_FAILED"):
        return deny(f"plan status is {plan['status']}")
    configs = plan.get("configs") or {}
    if approval_hash(plan["id"], configs) != plan["approval_hash"]:
        return deny("configuration was modified after approval")
    cfg = configs.get(job["device_id"])
    if not cfg or cfg["sha256"] != job["config_sha256"]:
        return deny("job does not reference the approved configuration for this device")
    if config_digest(cfg["lines"]) != cfg["sha256"]:
        return deny("configuration content does not match its approved hash (tampered)")
    dep = db.one("SELECT status FROM deployments WHERE id=?", (job["deployment_id"],))
    if not dep or dep["status"] in ("SUCCESS", "FAILED"):
        return deny("deployment is not active")
    base.update(approved_by=plan["approved_by"], approved_at=plan["approved_at"],
                config_sha256=cfg["sha256"], expected=cfg["expected"])
    if job["job_type"] == "deploy":
        base["config_lines"] = cfg["lines"]
    return base


class Event(BaseModel):
    worker_id: str
    message: str
    level: str = "info"


@app.post("/api/worker/jobs/{job_id}/events", dependencies=[Depends(require_worker)])
def job_event(job_id: str, body: Event):
    job = _job(job_id)
    if job["worker_id"] != body.worker_id:
        raise HTTPException(409, "not the job owner")
    db.add_event(job_id, body.message, body.level)
    db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + settings.job_lease_seconds, job_id))
    return {"ok": True}


class Result(BaseModel):
    worker_id: str
    status: str
    output: str = ""
    result: dict | None = None
    error: str | None = None


@app.post("/api/worker/jobs/{job_id}/result", dependencies=[Depends(require_worker)])
def job_result(job_id: str, body: Result):
    if body.status not in ("SUCCESS", "FAILED"):
        raise HTTPException(400, "status must be SUCCESS or FAILED")
    with db.transaction() as conn:
        row = conn.execute("SELECT status, worker_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] != "RUNNING" or row["worker_id"] != body.worker_id:
            raise HTTPException(409, "job is not running under this worker")
        conn.execute("UPDATE jobs SET status=?, finished_at=?, output=?, result=?, error=? WHERE id=?",
                     (body.status, time.time(), body.output[-200000:],
                      json.dumps(body.result) if body.result is not None else None, body.error, job_id))
    db.add_event(job_id, f"finished: {body.status}" + (f" - {body.error}" if body.error else ""),
                 "info" if body.status == "SUCCESS" else "error")
    _after_result(_job(job_id))
    return {"ok": True}


def _after_result(job: dict) -> None:
    dep_id = job.get("deployment_id")
    if not dep_id:
        return
    if job["job_type"] == "deploy":
        deps = db.query("SELECT status FROM jobs WHERE deployment_id=? AND job_type='deploy'", (dep_id,))
        if any(j["status"] == "FAILED" for j in deps):
            _finish_deployment(dep_id, "FAILED")
        elif all(j["status"] == "SUCCESS" for j in deps):
            # exactly-once transition RUNNING -> VERIFYING, then queue verification jobs
            if db.execute("UPDATE deployments SET status='VERIFYING' WHERE id=? AND status='RUNNING'",
                          (dep_id,)) == 1:
                plan = db.one("SELECT * FROM plans WHERE id=?", (job["plan_id"],))
                for dev, cfg in sorted(plan["configs"].items()):
                    v = dispatch("verify", dev, plan["id"], dep_id, cfg["sha256"], plan["approval_hash"])
                    if v["status"] == "FAILED":
                        _finish_deployment(dep_id, "FAILED")
    elif job["job_type"] == "verify":
        ver = db.query("SELECT status FROM jobs WHERE deployment_id=? AND job_type='verify'", (dep_id,))
        if any(j["status"] in ACTIVE for j in ver):
            return
        _finish_deployment(dep_id, "SUCCESS" if all(j["status"] == "SUCCESS" for j in ver) else "FAILED")
