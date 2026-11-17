"""Image-capable AI analysis of a topology diagram.

The AI only *reads the picture* and returns structured JSON. It never sees router
credentials and has no way to reach the routers - all device access happens in the
worker, after human approval, via Kafka.
"""
import base64
import ipaddress
import json
import re
import time

import httpx

from .config import settings

TOPOLOGY_SCHEMA_HINT = {
    "devices": [{"id": "R1", "label": "router1", "type": "router | host | switch", "notes": None}],
    "networks": [{"cidr": "10.0.0.0/24", "description": "R1 LAN (PC1)"}],
    "interfaces": [{"device": "R1", "network": "10.0.0.0/24", "ip": "10.0.0.1",
                    "prefix_length": 24, "peer": "PC1", "source": "written | inferred"}],
    "hosts": [{"id": "PC1", "ip": "10.0.0.10", "prefix_length": 24, "gateway": "10.0.0.1",
               "ip_source": "written | inferred", "gateway_source": "written | inferred"}],
    "links": [{"endpoints": ["PC1", "R1"], "network": "10.0.0.0/24"}],
    "routing": {"protocol": "OSPFv2", "process_id": 1, "area": 0,
                "router_ids": {"R1": "1.1.1.1"},
                "passive_lan_interfaces": True, "static_routes_allowed": False},
    "corrections": [{"item": "R2 LAN address", "old": ".1", "new": "10.0.0.254"}],
    "ambiguities": [{"item": "R2 router-id", "issue": "not shown in the diagram",
                     "severity": "blocking | warning"}],
}

SYSTEM_PROMPT = f"""You are a senior network engineer extracting a topology and IP addressing plan
from a network diagram image (and optional written requirements) for an automation system.

Return ONLY one JSON object (no markdown, no prose) with exactly these top-level keys:
devices, networks, interfaces, hosts, links, routing, corrections, ambiguities.
Example shape (values are illustrative, NOT the answer):
{json.dumps(TOPOLOGY_SCHEMA_HINT, indent=1)}

Rules:
- "interfaces" lists ROUTER interfaces only (one entry per router per connected network).
  Do NOT invent physical interface names (e.g. GigabitEthernet0/0); those are discovered later.
  Missing interface names are therefore expected: never list them as an ambiguity.
- "hosts" lists end hosts (PCs/servers) with their IP, prefix length and default gateway.
- Diagrams often show only the host part (".1", ".10") next to a device and the network
  ("10.0.0.0/24") on the link. Combine them into full addresses and use the network's prefix.
- Use the device names written in the diagram. If a router shows both an icon label (R1) and a
  caption (router1), use the short one as "id" and the other as "label".
- Every link between two devices must reference the network written on that link.
- "source" / "ip_source" / "gateway_source": "written" when that device's value (or its host
  part, e.g. ".1" next to the device, combined with the network on the link) is written in the
  picture or the requirements; "inferred" when it is not written for that device and you
  derived it (by elimination, from a rule like "gateways = router LAN IP", or by convention).
  Be honest: an inferred value is never "written". Never invent an address for a value that is
  missing, unreadable or scribbled over: set it to null and add a "blocking" ambiguity.
- Hand corrections: a value with a line or an X drawn THROUGH it is the old, cancelled value.
  A value written next to, above, below or at the end of an arrow from it, that is NOT struck
  through (often in another colour), is its replacement - use the replacement. The X belongs to
  the value it covers, not to the value written beside it. In a chain (a struck, b struck, c)
  the last un-struck value wins. List every correction in "corrections" (item, old, new);
  do not repeat it in "ambiguities". If every candidate is struck through, the value is
  missing: null + "blocking".
- A correction only replaces the value it is drawn on. If another written value still uses
  the old one (e.g. a PC gateway written as the router's old address), the picture
  contradicts itself: do NOT pick one side - add a "blocking" ambiguity naming both values.
- Management networks (labelled mgmt/management, dashed cables, "not part of OSPF") are not
  part of the plan: leave them out of networks, interfaces and links, and do not write an
  ambiguity about management access - it is handled outside the plan.
- routing: fill protocol/process_id/area/router_ids ONLY from what the image or the written
  requirements state. If a value is not stated, set it to null and add an ambiguity.
- NEVER guess. Anything unreadable, missing, contradictory or inferred goes into "ambiguities"
  with severity "blocking" (cannot safely configure without it) or "warning" (inferred, likely
  correct, needs human confirmation). A gateway inferred from the router address on the same
  LAN is a "warning", not blocking.
- Never output credentials, CLI commands or anything other than the JSON object.
"""


class AIError(Exception):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AIError("AI response did not contain a JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIError(f"AI returned invalid JSON: {exc}") from exc


def _user_text(requirements: str | None, ocr_text: str | None = None) -> str:
    t = "Extract the topology, addressing plan and routing requirements from this diagram."
    if ocr_text and ocr_text.strip():
        t += ("\n\nText read from the picture by an OCR tool. Use it to check every number you "
              "read (OCR is usually exact on digits but may lose layout, so use the picture for "
              "which label belongs to which device):\n" + ocr_text.strip()[:4000])
    if requirements and requirements.strip():
        t += ("\n\nWritten requirements supplied by the administrator (treat as authoritative "
              "where the image is silent):\n" + requirements.strip()[:4000])
    return t


def _anthropic(image: bytes, media_type: str, requirements: str | None,
               ocr_text: str | None = None) -> tuple[str, str]:
    if not settings.anthropic_api_key:
        raise AIError("ANTHROPIC_API_KEY is not set")
    model = settings.ai_model or "claude-sonnet-5-5"
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(image).decode()}},
                {"type": "text", "text": _user_text(requirements, ocr_text)},
            ],
        }],
    }
    r = httpx.post("https://api.anthropic.com/v1/messages", json=body, timeout=settings.ai_timeout,
                   headers={"x-api-key": settings.anthropic_api_key,
                            "anthropic-version": "2023-06-01"})
    if r.status_code >= 400:
        raise AIError(f"Anthropic API error {r.status_code}: {r.text[:500]}")
    data = r.json()
    text = "".join(block.get("text", "") for block in data.get("content", [])
                   if block.get("type") == "text")
    return text, model


def _chat_completions(label: str, base_url: str, api_key: str | None, model: str, image: bytes,
                      media_type: str, requirements: str | None, timeout: float,
                      ocr_text: str | None = None) -> tuple[str, str]:
    """POST {base_url}/chat/completions in the OpenAI format.

    Used for OpenAI itself, every OpenAI-compatible cloud API (Gemini, Groq, OpenRouter,
    GitHub Models, ...) and local servers (Ollama, LM Studio, vLLM, llama.cpp)."""
    data_url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": _user_text(requirements, ocr_text)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }
    if settings.ai_json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        r = httpx.post(url, json=body, timeout=timeout, headers=headers)
    except httpx.HTTPError as exc:
        raise AIError(f"{label} API at {base_url} unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise AIError(f"{label} API error {r.status_code}: {r.text[:500]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError(f"{label} API returned an unexpected response: {r.text[:500]}") from exc
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or "", model


def _openai(image: bytes, media_type: str, requirements: str | None,
            ocr_text: str | None = None) -> tuple[str, str]:
    """Google Gemini by default, or any other OpenAI-style cloud API, chosen by OPENAI_BASE_URL."""
    if not settings.openai_api_key:
        raise AIError("OPENAI_API_KEY is not set")
    model = settings.ai_model or "gemini-4-hydrogen"
    return _chat_completions("OpenAI-compatible", settings.openai_base_url, settings.openai_api_key,
                             model, image, media_type, requirements, settings.ai_timeout, ocr_text)


def _local(image: bytes, media_type: str, requirements: str | None,
           ocr_text: str | None = None) -> tuple[str, str]:
    """A model on your own machine (Ollama by default). Needs a vision-capable model."""
    model = settings.ai_model or "qwen2.5vl:7b"
    return _chat_completions("Local model", settings.local_ai_base_url, settings.local_ai_api_key,
                             model, image, media_type, requirements, settings.local_ai_timeout, ocr_text)


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "local": _local}

# Physical interface names never come from the diagram (they are discovered over SSH and the
# admin confirms the mapping), so an AI "ambiguity" about them can never be acted on. Smaller
# models report one anyway, which would block approval - drop them.
_IFNAME_AMBIGUITY = re.compile(r"interface[\s_-]*names?", re.I)
_MGMT_AMBIGUITY = re.compile(r"\bmanagement\b|\bmgmt\b", re.I)


def _drop_interface_name_ambiguities(ambiguities: list) -> list:
    """Also drops warnings about management access: management is never part of the plan
    (it is reached separately and checked by discovery), so such a note asks nothing of the
    reviewer. A blocking item that mentions management is kept."""
    kept = []
    for a in ambiguities:
        text = f"{a.get('item', '')} {a.get('issue', '')}" if isinstance(a, dict) else ""
        if isinstance(a, dict) and _IFNAME_AMBIGUITY.search(text):
            continue
        if (isinstance(a, dict) and _MGMT_AMBIGUITY.search(text)
                and str(a.get("severity", "")).lower() != "blocking"):
            continue
        kept.append(a)
    return kept


def read_text(image: bytes, media_type: str) -> str | None:
    """Read the text in the picture with an OCR service (Typhoon OCR by default).

    Off when OCR_API_KEY is empty. Raises AIError if the service fails."""
    if not settings.ocr_api_key:
        return None
    data_url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
    body = {"model": settings.ocr_model, "max_tokens": 4000, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Read all text in this image exactly as written."},
                {"type": "image_url", "image_url": {"url": data_url}}]}]}
    url = settings.ocr_base_url.rstrip("/") + "/chat/completions"
    for attempt in (1, 2):  # one retry: a short network blip should not skip the OCR check
        try:
            r = httpx.post(url, json=body, timeout=settings.ocr_timeout,
                           headers={"Authorization": f"Bearer {settings.ocr_api_key}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            if attempt == 2:
                raise AIError(f"OCR failed: {exc}") from exc
            time.sleep(2)


_CIDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b")


def _network(cidr: str):
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def _ocr_mismatches(topology: dict, ocr_text: str) -> list:
    """Networks the AI reports that the OCR never saw, when the picture writes networks as
    a.b.c.d/nn. Catches a misread digit that the other checks can't see, because a misread
    network still agrees with itself."""
    seen = set(_CIDR.findall(ocr_text))
    # 10.0.0.1/24 written next to a router also counts as network 10.0.0.0/24
    seen_nets = {_network(c) for c in seen} - {None}
    if not seen:  # networks not written in CIDR form (e.g. dotted masks) - nothing to compare
        return []
    issues = []
    for n in topology.get("networks") or []:
        cidr = str((n.get("cidr") or n.get("network")) if isinstance(n, dict) else n)
        if _CIDR.fullmatch(cidr) and _network(cidr) not in seen_nets:
            issues.append({"item": f"network {cidr}",
                           "issue": f"not found in the text read from the picture "
                                    f"({', '.join(sorted(seen))}). Check this network against the picture.",
                           "severity": "warning"})
    return issues


def _source_checks(topology: dict) -> list:
    """Turn what the AI says about where each value came from into ambiguities, so the rules
    don't depend on the AI also remembering to write an ambiguity:
    * a router or PC address that is not written in the picture (inferred) blocks the plan;
    * an inferred PC gateway is a warning (normal: "gateway = router LAN IP");
    * every hand correction becomes a warning that names the old and the new value."""
    out = []
    for i in topology.get("interfaces") or []:
        if isinstance(i, dict) and str(i.get("source", "")).lower() == "inferred" and i.get("ip"):
            out.append({"item": f"{i.get('device')} address on {i.get('network')}",
                        "issue": f"{i['ip']} is not written in the picture (the AI inferred it). "
                                 "Check it, then fix it with Edit JSON", "severity": "blocking"})
    for h in topology.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        name = h.get("id") or h.get("name")
        if str(h.get("ip_source", "")).lower() == "inferred" and h.get("ip"):
            out.append({"item": f"{name} address", "issue": f"{h['ip']} is not written in the picture "
                        "(the AI inferred it). Check it, then fix it with Edit JSON", "severity": "blocking"})
        already = any(name and name in f"{a.get('item')} {a.get('issue')}" and "gateway" in
                      f"{a.get('item')} {a.get('issue')}".lower()
                      for a in topology.get("ambiguities") or [] if isinstance(a, dict))
        if str(h.get("gateway_source", "")).lower() == "inferred" and h.get("gateway") and not already:
            out.append({"item": f"{name} gateway", "issue": f"{h['gateway']} inferred from the router "
                        "address on the same LAN", "severity": "warning"})
    for c in topology.get("corrections") or []:
        if isinstance(c, dict):
            out.append({"item": f"hand correction: {c.get('item')}",
                        "issue": f"{c.get('old')} is crossed out, replaced by {c.get('new')}. "
                                 "Check the picture", "severity": "warning"})
    return out


def analyze_diagram(image: bytes, media_type: str, requirements: str | None = None) -> dict:
    provider = settings.ai_provider
    if provider not in PROVIDERS:
        raise AIError(f"Unknown AI_PROVIDER '{provider}'")
    ocr_note = None
    try:
        ocr_text = read_text(image, media_type)
    except AIError as exc:
        ocr_text, ocr_note = None, {"item": "OCR", "issue": f"{exc} - numbers not double-checked",
                                    "severity": "warning"}
    extra = {"ocr_text": ocr_text} if ocr_text else {}
    raw, model = PROVIDERS[provider](image, media_type, requirements, **extra)
    topology = _extract_json(raw)
    for key in ("devices", "networks", "interfaces", "hosts", "links", "corrections", "ambiguities"):
        topology.setdefault(key, [])
    topology.setdefault("routing", {})
    topology["ambiguities"] = _drop_interface_name_ambiguities(topology["ambiguities"] or [])
    topology["ambiguities"] += _source_checks(topology)
    if ocr_text:
        topology["ambiguities"] += _ocr_mismatches(topology, ocr_text)
    if ocr_note:
        topology["ambiguities"].append(ocr_note)
    return {"topology": topology, "raw": raw, "provider": provider, "model": model, "ocr_text": ocr_text}
