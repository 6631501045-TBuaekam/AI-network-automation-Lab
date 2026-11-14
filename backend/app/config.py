"""Runtime settings, read from environment variables only (no secrets in code)."""
import os


def _env(name: str, default: str | None = None) -> str | None:
    """Read NAME, or the contents of the file named by NAME_FILE (Docker secrets)."""
    file_var = os.getenv(f"{name}_FILE")
    if file_var and os.path.exists(file_var):
        with open(file_var, encoding="utf-8") as fh:
            return fh.read().strip()
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.data_dir = _env("DATA_DIR", "/data")
        self.db_path = _env("DB_PATH", os.path.join(self.data_dir, "netauto.db"))
        self.upload_dir = _env("UPLOAD_DIR", os.path.join(self.data_dir, "uploads"))
        self.inventory_path = _env("INVENTORY_PATH", "/config/devices.yaml")

        # AI provider: anthropic | openai | local | mock (mock = offline fixture, development only)
        #   openai = OpenAI or any OpenAI-compatible cloud API (set OPENAI_BASE_URL)
        #   local  = a model on your own machine (Ollama, LM Studio, vLLM, llama.cpp)
        self.ai_provider = (_env("AI_PROVIDER", "anthropic") or "anthropic").lower()
        self.ai_model = _env("AI_MODEL")
        self.anthropic_api_key = _env("ANTHROPIC_API_KEY")
        self.openai_api_key = _env("OPENAI_API_KEY")
        self.openai_base_url = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.local_ai_base_url = _env("LOCAL_AI_BASE_URL", "http://host.docker.internal:11434/v1")
        self.local_ai_api_key = _env("LOCAL_AI_API_KEY")  # most local servers need none
        self.ai_json_mode = _bool("AI_JSON_MODE", True)    # send response_format=json_object
        self.ai_timeout = float(_env("AI_TIMEOUT", "120"))
        self.local_ai_timeout = float(_env("LOCAL_AI_TIMEOUT", "600"))  # CPU inference is slow
        self.mock_ai_fixture = _env("MOCK_AI_FIXTURE", "/app/fixtures/mock_ai_response.json")

        # GNS3 lab cross-check (read-only, optional). Suggests the interface mapping from the
        # real cabling; SSH discovery and the admin's confirmation still decide.
        self.gns3_enabled = _bool("GNS3_ENABLED", False)
        self.gns3_url = _env("GNS3_URL", "http://192.168.192.131")
        self.gns3_project = _env("GNS3_PROJECT", "ai-netauto-ospf")
        self.gns3_token = _env("GNS3_TOKEN")
        self.gns3_username = _env("GNS3_USERNAME")
        self.gns3_password = _env("GNS3_PASSWORD")
        self.gns3_timeout = float(_env("GNS3_TIMEOUT", "5"))
        # auto = detect (v3 for GNS3 3.x, v2 for GNS3 2.2); or force "v2" / "v3"
        self.gns3_api = (_env("GNS3_API", "auto") or "auto").lower()

        # Auth
        self.admin_token = _env("ADMIN_TOKEN")
        self.worker_token = _env("WORKER_TOKEN")

        # Kafka
        self.kafka_enabled = _bool("KAFKA_ENABLED", True)
        self.kafka_bootstrap = _env("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
        self.kafka_topic = _env("KAFKA_TOPIC", "network-config-jobs")

        # Behaviour
        self.discovery_max_age_min = int(_env("DISCOVERY_MAX_AGE_MIN", "120"))
        self.ospf_transit_p2p = _bool("OSPF_TRANSIT_P2P", True)
        self.job_lease_seconds = int(_env("JOB_LEASE_SECONDS", "600"))
        self.max_upload_mb = int(_env("MAX_UPLOAD_MB", "10"))
        self.public_base_url = _env("PUBLIC_BASE_URL", "http://backend:8000")


settings = Settings()


def reload_settings() -> Settings:
    """Used by tests after changing environment variables."""
    global settings
    settings.__init__()
    return settings
