"""Project native Orca session metadata without returning prompts or transcripts."""

from __future__ import annotations

import re
from typing import Any

from app.orca_adapter import OrcaError

MAX_SESSIONS = 300
_LABEL = re.compile(r"[A-Za-z0-9_. ~-]{1,100}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def _label(value: Any) -> str:
    # Native metadata only; do not return arbitrary paths or free text.
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "unknown"


def _short_id(row: dict[str, Any]) -> str | None:
    value = row.get("orcaSessionId")
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        pane = row.get("paneKey")
        value = pane.rsplit(":", 1)[-1] if isinstance(pane, str) else None
    return value[-8:] if isinstance(value, str) and _UUID.fullmatch(value) else None


def project_sessions(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("ok") is not True or not isinstance(data.get("result"), dict):
        raise OrcaError("invalid_session_list", "Invalid Orca session response")
    result = data["result"]
    rows = result.get("agents")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise OrcaError("invalid_session_list", "Invalid Orca session rows")

    sessions = []
    for row in rows[:MAX_SESSIONS]:
        last = row.get("lastAssistantMessage")
        updated = row.get("updatedAt")
        sessions.append({
            "session_suffix": _short_id(row),
            "agent": _label(row.get("agent")),
            "kind": _label(row.get("kind")),
            "state": _label(row.get("state")),
            "workspace": _label(row.get("workspace")),
            "repo": _label(row.get("repo")),
            "assistant_message_present": (
                None if "lastAssistantMessage" not in row else
                bool(last.strip()) if isinstance(last, str) else
                False if last is None else None
            ),
            "updated_at_ms": updated if type(updated) is int and 0 <= updated <= 8_640_000_000_000_000 else None,
        })
    scope = result.get("hostScope")
    omitted = scope.get("omittedHostIds") if isinstance(scope, dict) else None
    return {
        "sessions": sessions,
        "truncated": result.get("truncated") is True or len(rows) > MAX_SESSIONS,
        "hosts_omitted": bool(omitted) if isinstance(omitted, list) else None,
    }
