"""Read-only discovery of zero-price, tool-capable OpenCode routes. No inference."""
from __future__ import annotations
import json
import math
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

MODEL_ID = re.compile(r"^opencode/[A-Za-z0-9._-]{1,100}$")
TTL = 3600


def project_models(text: str) -> list[dict]:
    """CLI verbose output has a model label followed by one JSON object per model."""
    decoder = json.JSONDecoder(); pos = 0; found = {}
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0: break
        try: obj, length = decoder.raw_decode(text[start:])
        except ValueError: pos = start + 1; continue
        pos = start + length
        if not isinstance(obj, dict): continue
        provider = obj.get("providerID") or obj.get("provider")
        mid = obj.get("id")
        if provider != "opencode" or not isinstance(mid, str): continue
        mid = mid if mid.startswith("opencode/") else "opencode/" + mid
        cost = obj.get("cost"); caps = obj.get("capabilities") or {}
        api = obj.get("api") or {}
        if not isinstance(caps, dict) or not isinstance(api, dict) or not isinstance(api.get("url"), str): continue
        try:
            endpoint = urlparse(api["url"])
            endpoint.hostname
        except ValueError:
            continue
        if not MODEL_ID.fullmatch(mid) or not isinstance(cost, dict): continue
        if not endpoint or endpoint.scheme != "https" or endpoint.hostname != "opencode.ai" or endpoint.username or endpoint.password: continue
        # The catalog must explicitly verify BOTH directions and tool execution.
        if any(type(cost.get(k)) not in (int, float) or not math.isfinite(cost[k]) or cost[k] != 0 for k in ("input", "output")): continue
        if caps.get("toolcall") is not True and obj.get("tool_call") is not True: continue
        # Price-bearing cache variants must also explicitly be zero.
        cache = cost.get("cache") or {}
        if not isinstance(cache, dict) or any(type(v) not in (int, float) or not math.isfinite(v) or v != 0 for v in cache.values()): continue
        variants = cost.get("context_over_200k")
        if variants is not None and (not isinstance(variants, dict) or any(type(v) not in (int, float) or not math.isfinite(v) or v != 0 for v in variants.values())): continue
        found[mid] = {"id": mid, "input_price": 0, "output_price": 0, "tool_call": True,
                      "quota": None, "source": "opencode models opencode --verbose"}
    return [found[k] for k in sorted(found)]


def probe_opencode() -> dict:
    exe = os.environ.get("MISSIONDECK_OPENCODE_EXE", "").strip() or shutil.which("opencode")
    base = {"checked_at_epoch": time.time(), "models": [], "inference": False, "quota": None}
    if not exe: return {**base, "status": "unavailable", "reason": "OpenCode executable was not found on PATH"}
    try:
        # Fixed argv; raw output and config/account identities are never returned or stored.
        result = subprocess.run([exe, "models", "opencode", "--verbose"], shell=False,
                                capture_output=True, timeout=30)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            return {**base, "status": "unavailable", "reason": "OpenCode model catalog could not be verified"}
        models = project_models(result.stdout.decode("utf-8", errors="replace"))
        return {**base, "status": "ok" if models else "unverified", "models": models,
                "reason": "Zero input/output price and tool use verified in CLI catalog" if models else "No supported zero-price route was verified; check OpenCode version and catalog"}
    except (OSError, subprocess.TimeoutExpired):
        return {**base, "status": "unavailable", "reason": "OpenCode model catalog command failed or timed out"}


def selected_route(catalog: dict, model: str | None) -> dict | None:
    if not isinstance(catalog, dict) or catalog.get("status") != "ok": return None
    timestamp = catalog.get("checked_at_epoch")
    if type(timestamp) not in (int, float) or not 0 <= time.time() - timestamp <= TTL: return None
    return next((m for m in catalog.get("models", []) if m.get("id") == model and MODEL_ID.fullmatch(model or "")), None)
