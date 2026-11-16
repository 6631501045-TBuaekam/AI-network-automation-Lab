"""AI provider routing: OpenAI-compatible cloud APIs (via OPENAI_BASE_URL) and local models."""
import json
import os

import httpx
import pytest

from app import ai
from app.config import reload_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = open(os.path.join(ROOT, "tests", "fixtures", "topology.json"), encoding="utf-8").read()
AI_VARS = ("AI_PROVIDER", "AI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "LOCAL_AI_BASE_URL",
           "LOCAL_AI_API_KEY", "AI_JSON_MODE", "LOCAL_AI_TIMEOUT", "OCR_API_KEY", "OCR_BASE_URL",
           "OCR_MODEL")


@pytest.fixture
def capture(monkeypatch):
    """Replace httpx.post with a fake that records the request and answers like OpenAI."""
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append({"url": url, "body": json, "timeout": timeout, "headers": headers or {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": FIXTURE}}]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(ai.httpx, "post", fake_post)
    yield calls
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "test")
    reload_settings()


def _run(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reload_settings()
    return ai.analyze_diagram(b"\x89PNG fake", "image/png")


def test_openai_defaults_to_gemini(capture, monkeypatch):
    out = _run(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="k1")
    c = capture[0]
    assert c["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer k1"
    assert c["body"]["model"] == "gemini-4-hydrogen"
    assert c["body"]["response_format"] == {"type": "json_object"}
    assert out["provider"] == "openai" and out["topology"]["devices"]


def test_openai_compatible_base_url(capture, monkeypatch):
    _run(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="gem",
         OPENAI_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
         AI_MODEL="gemini-x")
    c = capture[0]
    assert c["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert c["body"]["model"] == "gemini-x"
    image = c["body"]["messages"][1]["content"][1]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_requires_key(capture, monkeypatch):
    with pytest.raises(ai.AIError, match="OPENAI_API_KEY"):
        _run(monkeypatch, AI_PROVIDER="openai")
    assert capture == []


def test_local_needs_no_key(capture, monkeypatch):
    out = _run(monkeypatch, AI_PROVIDER="local")
    c = capture[0]
    assert c["url"] == "http://host.docker.internal:11434/v1/chat/completions"
    assert "Authorization" not in c["headers"]
    assert c["body"]["model"] == "qwen2.5vl:7b"
    assert c["timeout"] == 600
    assert out["provider"] == "local"


def test_local_custom_server_key_and_json_mode_off(capture, monkeypatch):
    _run(monkeypatch, AI_PROVIDER="local", LOCAL_AI_BASE_URL="http://host.docker.internal:1234/v1",
         LOCAL_AI_API_KEY="lm", AI_MODEL="qwen-vl", AI_JSON_MODE="false", LOCAL_AI_TIMEOUT="90")
    c = capture[0]
    assert c["url"] == "http://host.docker.internal:1234/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer lm"
    assert "response_format" not in c["body"]
    assert c["timeout"] == 90


def test_unreachable_server_is_reported(monkeypatch):
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)

    def boom(url, **kw):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(ai.httpx, "post", boom)
    try:
        with pytest.raises(ai.AIError, match="unreachable"):
            _run(monkeypatch, AI_PROVIDER="local")
    finally:
        monkeypatch.setenv("AI_PROVIDER", "test")
        reload_settings()


def test_content_parts_are_joined(monkeypatch):
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    parts = [{"type": "text", "text": FIXTURE[:10]}, {"type": "text", "text": FIXTURE[10:]}]
    monkeypatch.setattr(ai.httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"choices": [{"message": {"content": parts}}]}, request=httpx.Request("POST", url)))
    try:
        out = _run(monkeypatch, AI_PROVIDER="local")
        assert out["topology"] == {**json.loads(FIXTURE), **out["topology"]}
    finally:
        monkeypatch.setenv("AI_PROVIDER", "test")
        reload_settings()


def test_interface_name_ambiguities_are_dropped(monkeypatch):
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    topo = json.loads(FIXTURE)
    topo["ambiguities"] = [
        {"item": "R1 interface names", "issue": "not shown in the diagram", "severity": "blocking"},
        {"item": "R1-R2", "issue": "interface name unknown", "severity": "blocking"},
        {"item": "R2 router-id", "issue": "not shown in the diagram", "severity": "blocking"},
        {"item": "PC gateways", "issue": "inferred", "severity": "warning"},
    ]
    monkeypatch.setattr(ai.httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(topo)}}]},
        request=httpx.Request("POST", url)))
    try:
        out = _run(monkeypatch, AI_PROVIDER="local")
        assert [a["item"] for a in out["topology"]["ambiguities"]] == ["R2 router-id", "PC gateways"]
    finally:
        monkeypatch.setenv("AI_PROVIDER", "test")
        reload_settings()


def test_ocr_text_is_given_to_the_ai(capture, monkeypatch):
    out = _run(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="k1", OCR_API_KEY="ocr-key")
    ocr, main = capture
    assert ocr["url"] == "https://api.opentyphoon.ai/v1/chat/completions"
    assert ocr["body"]["model"] == "typhoon-ocr-v2"
    assert ocr["headers"]["Authorization"] == "Bearer ocr-key"
    prompt = main["body"]["messages"][1]["content"][0]["text"]
    assert "Text read from the picture by an OCR tool" in prompt and FIXTURE.strip()[:40] in prompt
    assert out["ocr_text"]


def test_ocr_is_off_without_a_key(capture, monkeypatch):
    out = _run(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="k1")
    assert len(capture) == 1 and out["ocr_text"] is None
    assert "OCR" not in capture[0]["body"]["messages"][1]["content"][0]["text"]


def test_ocr_failure_only_warns(capture, monkeypatch):
    real_post = ai.httpx.post

    def post(url, **kw):
        if "opentyphoon" in url:
            raise httpx.ConnectError("down")
        return real_post(url, **kw)

    monkeypatch.setattr(ai.httpx, "post", post)
    out = _run(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="k1", OCR_API_KEY="ocr-key")
    note = out["topology"]["ambiguities"][-1]
    assert note["item"] == "OCR" and note["severity"] == "warning"


def test_network_missing_from_ocr_text_is_flagged():
    topo = {"networks": [{"cidr": "172.20.30.0/27"}, {"cidr": "10.255.0.0/30"}]}
    issues = ai._ocr_mismatches(topo, "PC1 172.20.5.0/27 .1 R1 10.255.0.0/30 .2 R2")
    assert [i["item"] for i in issues] == ["network 172.20.30.0/27"]
    assert issues[0]["severity"] == "warning"
    # pictures that write masks instead of /nn give nothing to compare against
    assert ai._ocr_mismatches(topo, "192.168.50.66 255.255.255.192") == []
