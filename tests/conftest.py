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
    "AI_PROVIDER": "mock",
    "MOCK_AI_FIXTURE": os.path.join(ROOT, "backend", "fixtures", "mock_ai_response.json"),
    "ADMIN_TOKEN": "test-admin-token",
    "WORKER_TOKEN": "test-worker-token",
    "KAFKA_ENABLED": "false",
    "VERIFY_TIMEOUT": "30",
    "VERIFY_INTERVAL": "1",
    "SSH_TIMEOUT": "10",
    "ROUTER_SSH_USERNAME": "netadmin",
    "ROUTER_SSH_PASSWORD": "lab-pass",
})
