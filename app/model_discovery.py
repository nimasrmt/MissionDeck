"""Provider presets and explicit, bounded GET /models discovery. No inference."""
from __future__ import annotations

import json
import httpx

from app.api_models import APIConnections, APIModelError, MODEL, check_public_host, normalize_url
from app.security import register_secret

PROVIDERS = {
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "protocol": "openai", "public_catalog": True},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "protocol": "openai"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "protocol": "openai"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "protocol": "openai"},
    "anthropic": {"name": "Anthropic / Claude", "base_url": "https://api.anthropic.com/v1", "protocol": "anthropic"},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai/v1", "protocol": "openai"},
}


def provider_presets():
    return {"providers": [{"id": key, **row} for key, row in PROVIDERS.items()], "inference": False}


def normalize_provider(body):
    """The explicitly selected provider determines the destination, never a key guess."""
    data = dict(body)
    pid = data.get("provider", "custom")
    if pid != "custom":
        if pid not in PROVIDERS: raise APIModelError("api_provider", "Choose the service that issued your key, or use free models without a key")
        preset = PROVIDERS[pid]
        data.update(base_url=preset["base_url"], protocol=preset["protocol"])
    data["provider"] = pid
    return data


def project_models(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise APIModelError("api_catalog", "The service did not return a supported model list")
    found = {}
    for item in payload["data"]:
        if not isinstance(item, dict): continue
        mid = item.get("id")
        if not isinstance(mid, str) or not MODEL.fullmatch(mid) or ".." in mid.split("/"): continue
        name = item.get("display_name") or item.get("name") or mid
        if not isinstance(name, str): name = mid
        name = " ".join(name.split())[:160]
        parameters = item.get("supported_parameters")
        caps = item.get("capabilities")
        tools = True if isinstance(parameters, list) and "tools" in parameters else None
        if isinstance(caps, dict) and type(caps.get("function_calling")) is bool: tools = caps["function_calling"]
        if item.get("active") is False or item.get("status") in ("retired", "disabled"): continue
        found[mid] = {"id": mid, "name": name, "tool_support": tools, "quota": None, "price": None}
        if len(found) >= 2000: break
    return {"models": sorted(found.values(), key=lambda row: (row["name"].casefold(), row["id"])),
            "truncated": bool(payload.get("has_more")) or len(payload["data"]) > 2000}


def fetch_models(base_url, protocol, key):
    check_public_host(base_url)
    headers = {}
    if protocol == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif key:
        headers = {"Authorization": "Bearer " + key}
    try:
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            params = {"limit": 1000} if protocol == "anthropic" else None
            with client.stream("GET", base_url + "/models", headers=headers, params=params) as response:
                if response.status_code != 200:
                    raise APIModelError("api_catalog", f"Model list returned HTTP {response.status_code}. Check the selected service and its API key")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024: raise APIModelError("api_catalog", "The model catalog exceeded the supported size")
        return project_models(json.loads(raw))
    except APIModelError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise APIModelError("api_catalog", "Could not read this service's model list. No generation request was sent") from None


def discover(store: APIConnections, body):
    data = normalize_provider(body)
    base_url = normalize_url(data.get("base_url", ""))
    protocol = data.get("protocol")
    if protocol not in ("openai", "anthropic"): raise APIModelError("api_provider", "Unsupported API format")
    key = data.get("api_key", "")
    if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise APIModelError("api_key", "Enter a single-line API key without spaces")
    if not key and data.get("connection_id"):
        old = next((r for r in store._rows() if r["id"] == data["connection_id"]), None)
        if not old: raise APIModelError("not_found", "API connection not found")
        if old["base_url"] != base_url or old["protocol"] != protocol:
            raise APIModelError("api_key", "Enter the key again when changing the service or endpoint")
        key = store._key(old)
    public = PROVIDERS.get(data["provider"], {}).get("public_catalog", False)
    if not key and not public: raise APIModelError("api_key", "Paste the key issued by the selected service, or choose free models without a key")
    if key: register_secret(key)
    result = fetch_models(base_url, protocol, key)
    return {**result, "provider": data["provider"], "base_url": base_url, "protocol": protocol,
            "inference": False, "availability": "provider_catalog_only", "note": "Model names reported by the service. Account allowance and tool use require a separate explicit test"}
