"""Regression: `with open_session(...)` must open exactly ONE SSH session.

open_session() returns an already-connected IOSSession. IOSSession.__enter__ used to call
connect() again, which leaked the first session (until the router ran out of VTY lines and
refused SSH) and bypassed open_session()'s retry loop. Found in the live GNS3 evidence run.
"""
import worker
from ssh_client import IOSSession, UnreachableError


def _patch(monkeypatch, fail_first=0):
    calls = {"connect": 0, "close": 0}

    def fake_connect(self):
        calls["connect"] += 1
        if calls["connect"] <= fail_first:
            raise UnreachableError("cannot reach (simulated)")
        self.client = object()
        return self

    def fake_close(self):
        calls["close"] += 1
        self.client = None

    monkeypatch.setattr(IOSSession, "connect", fake_connect)
    monkeypatch.setattr(IOSSession, "close", fake_close)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    monkeypatch.setattr(worker, "credentials", lambda dev: {"username": "u", "password": "p"})
    return calls


INV = {"R1": {"host": "192.0.2.1", "port": 22}}


def test_one_ssh_session_per_with_block(monkeypatch):
    calls = _patch(monkeypatch)
    with worker.open_session("R1", INV) as s:
        assert s.client is not None
    assert calls == {"connect": 1, "close": 1}


def test_retry_still_applies_and_no_extra_connect(monkeypatch):
    calls = _patch(monkeypatch, fail_first=2)
    with worker.open_session("R1", INV):
        pass
    assert calls == {"connect": 3, "close": 1}  # 2 failed attempts + 1 success, nothing extra


def test_plain_with_still_connects(monkeypatch):
    calls = _patch(monkeypatch)
    with IOSSession("192.0.2.1", 22, "u", "p"):
        pass
    assert calls == {"connect": 1, "close": 1}
