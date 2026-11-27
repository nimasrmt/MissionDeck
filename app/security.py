"""Security helpers: host/origin checks, CSRF, path scoping, redaction."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Iterable

from app.config import ALLOWED_HOSTS, ALLOWED_ORIGINS, WORKSPACE

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED_SK]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GHP]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE), "[REDACTED_BEARER]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[REDACTED_GOOGLE]"),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"), "[REDACTED_B64]"),
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])"), "[REDACTED_HEX]"),
]


class SecurityError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def check_host(host: str | None) -> None:
    if not host or host.lower() not in ALLOWED_HOSTS:
        raise SecurityError("bad_host", "Host header not allowed", {"host": host})


def check_origin(origin: str | None) -> None:
    if origin is None or origin == "":
        return
    if origin not in ALLOWED_ORIGINS:
        raise SecurityError("bad_origin", "Origin not allowed", {"origin": origin})


def check_csrf(cookie_token: str | None, header_token: str | None, *, mutating: bool) -> None:
    if not mutating:
        return
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        raise SecurityError("csrf", "Missing or mismatched X-CC-Token")


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, replacement in REDACTION_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _is_reparse_point(path: Path) -> bool:
    try:
        st = path.lstat()
        # Windows reparse points / Unix symlink
        if hasattr(st, "st_file_attributes"):
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400
            if int(getattr(st, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        return path.is_symlink()
    except OSError:
        return False


def resolve_under_workspace(
    rel_or_path: str,
    *,
    workspace: Path | None = None,
    must_exist: bool = False,
    allow_missing: bool = True,
) -> Path:
    """Resolve a relative path strictly under workspace. Reject absolute, .., .git, reparse escapes."""
    root = (workspace or WORKSPACE).resolve()
    if not isinstance(rel_or_path, str) or not rel_or_path.strip():
        raise SecurityError("bad_path", "Empty path")
    raw = rel_or_path.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) or raw.startswith("//"):
        raise SecurityError("bad_path", "Absolute paths are not allowed", {"path": rel_or_path})
    parts = Path(raw).parts
    if ".." in parts:
        raise SecurityError("bad_path", "Path traversal is not allowed", {"path": rel_or_path})
    if any(p == ".git" or p.endswith(".git") for p in parts):
        raise SecurityError("bad_path", ".git paths are not allowed", {"path": rel_or_path})
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SecurityError("bad_path", "Path escapes workspace", {"path": rel_or_path}) from exc

    # Walk parents for reparse-point escapes
    cur = candidate
    while True:
        if _is_reparse_point(cur):
            # Resolve through and re-check containment
            try:
                real = cur.resolve()
                real.relative_to(root)
            except (OSError, ValueError) as exc:
                raise SecurityError(
                    "bad_path",
                    "Reparse-point escape rejected",
                    {"path": rel_or_path},
                ) from exc
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent

    if must_exist and not candidate.exists():
        raise SecurityError("bad_path", "Path does not exist", {"path": rel_or_path})
    if not allow_missing and not candidate.exists():
        raise SecurityError("bad_path", "Path does not exist", {"path": rel_or_path})
    return candidate


def is_under_workspace(path: Path, workspace: Path | None = None) -> bool:
    root = (workspace or WORKSPACE).resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def sanitize_markdown(text: str) -> str:
    """Allowlist-style sanitization: strip raw HTML/scripts/event handlers."""
    if not text:
        return ""
    out = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    out = re.sub(r"on\w+\s*=", "", out, flags=re.IGNORECASE)
    return out


def cap_bytes(data: bytes | str, limit: int) -> tuple[str, bool]:
    if isinstance(data, bytes):
        truncated = len(data) > limit
        chunk = data[:limit]
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            text = chunk.decode("latin-1", errors="replace")
    else:
        truncated = len(data.encode("utf-8", errors="replace")) > limit
        # Cap by characters approximating bytes
        encoded = data.encode("utf-8", errors="replace")
        text = encoded[:limit].decode("utf-8", errors="replace")
    return text, truncated
