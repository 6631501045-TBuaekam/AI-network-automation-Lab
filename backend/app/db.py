"""Tiny SQLite persistence layer (single backend instance owns the database)."""
import json
import os
import sqlite3
import threading
import time
import uuid

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS diagrams(
  id TEXT PRIMARY KEY, filename TEXT, path TEXT, sha256 TEXT, content_type TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, diagram_id TEXT, status TEXT, requirements TEXT,
  ai_provider TEXT, ai_model TEXT, ai_raw TEXT, topology TEXT, topology_source TEXT,
  interface_map TEXT, mapping_confirmed INTEGER DEFAULT 0,
  configs TEXT, validation TEXT,
  approval_hash TEXT, approved_by TEXT, approved_at REAL, approval_note TEXT,
  created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS deployments(
  id TEXT PRIMARY KEY, plan_id TEXT, status TEXT, created_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, job_type TEXT, plan_id TEXT, deployment_id TEXT, device_id TEXT,
  status TEXT, config_sha256 TEXT, kafka_meta TEXT, worker_id TEXT, attempts INTEGER DEFAULT 0,
  created_at REAL, started_at REAL, finished_at REAL, lease_until REAL,
  output TEXT, result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS job_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, ts REAL, level TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_jobs_plan ON jobs(plan_id);
CREATE INDEX IF NOT EXISTS ix_jobs_device ON jobs(device_id, job_type, status);
CREATE INDEX IF NOT EXISTS ix_events_job ON job_events(job_id);
"""

JSON_COLUMNS = {"topology", "interface_map", "configs", "validation", "kafka_meta", "result"}


def init_db() -> None:
    global _conn
    with _lock:
        os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)


def _c() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    return _conn  # type: ignore[return-value]


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_COLUMNS & d.keys():
        if d[k]:
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def _encode(params):
    return tuple(json.dumps(p) if isinstance(p, (dict, list)) else p for p in params)


def execute(sql: str, params=()) -> int:
    with _lock:
        cur = _c().execute(sql, _encode(params))
        return cur.rowcount


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [_decode(r) for r in _c().execute(sql, _encode(params)).fetchall()]


def one(sql: str, params=()) -> dict | None:
    with _lock:
        return _decode(_c().execute(sql, _encode(params)).fetchone())


def transaction():
    """Usage: with db.transaction() as conn: ... (BEGIN IMMEDIATE for atomic claims)."""
    class _Tx:
        def __enter__(self):
            _lock.acquire()
            _c().execute("BEGIN IMMEDIATE")
            return _c()

        def __exit__(self, exc_type, exc, tb):
            try:
                _c().execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                _lock.release()
            return False
    return _Tx()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def add_event(job_id: str, message: str, level: str = "info") -> None:
    execute("INSERT INTO job_events(job_id, ts, level, message) VALUES(?,?,?,?)",
            (job_id, now(), level, message[:4000]))
