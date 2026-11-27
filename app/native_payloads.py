"""Read native Orca status envelopes without publishing raw CLI metadata."""
from __future__ import annotations

import re
from typing import Any


def project_status(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    runtime = result.get("runtime", payload.get("runtime"))
    native = runtime if isinstance(runtime, dict) else {}
    version = native.get("appVersion") or result.get("version") or payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"v?\d{1,5}\.\d{1,5}\.\d{1,5}", version):
        version = None
    state = native.get("state")
    connection = native.get("connectionState")
    available = payload.get("ok") is True
    if isinstance(runtime, dict):
        available = available and state == "ready" and connection == "connected"
    return {
        "available": available,
        "version": version,
        "runtime": {
            "state": state if state in ("ready", "starting", "stopped", "error") else "unknown",
            "connectionState": connection if connection in ("connected", "connecting", "disconnected", "error") else "unknown",
        },
    }
