import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "worker"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

_tmp = tempfile.mkdtemp(prefix="netauto-test-")
os.environ.update({
    "DATA_DIR": _tmp,
    "DB_PATH": os.path.join(_tmp, "test.db"),
    "UPLOAD_DIR": os.path.join(_tmp, "uploads"),
    "INVENTORY_PATH": os.path.join(_tmp, "devices.yaml"),
    "AI_PROVIDER": "test",
    "ADMIN_TOKEN": "test-admin-token",
    "WORKER_TOKEN": "test-worker-token",
    "KAFKA_ENABLED": "false",
    "VERIFY_TIMEOUT": "30",
    "VERIFY_INTERVAL": "1",
    "SSH_TIMEOUT": "10",
    "ROUTER_SSH_USERNAME": "netadmin",
    "ROUTER_SSH_PASSWORD": "lab-pass",
})

# The app only ships real AI providers. Tests register their own provider that answers with a
# known topology, so they run offline and give the same result every time.
from app import ai  # noqa: E402

TOPOLOGY_FILE = os.path.join(ROOT, "tests", "fixtures", "topology.json")


def _test_provider(image: bytes, media_type: str, requirements: str | None) -> tuple[str, str]:
    with open(TOPOLOGY_FILE, encoding="utf-8") as fh:
        return fh.read(), "test-topology"


ai.PROVIDERS["test"] = _test_provider
