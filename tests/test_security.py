"""Host/origin/token, redaction, path scoping."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.security import (
    SecurityError,
    check_csrf,
    check_host,
    check_origin,
    redact,
    resolve_under_workspace,
)


def test_host_enforcement() -> None:
    check_host("127.0.0.1:8765")
    check_host("localhost:8765")
    with pytest.raises(SecurityError):
        check_host("evil.example:8765")
    with pytest.raises(SecurityError):
        check_host(None)


def test_origin_enforcement() -> None:
    check_origin(None)
    check_origin("http://127.0.0.1:8765")
    with pytest.raises(SecurityError):
        check_origin("http://evil.example")


def test_csrf() -> None:
    check_csrf("abc", "abc", mutating=True)
    with pytest.raises(SecurityError):
        check_csrf("abc", "xyz", mutating=True)
    with pytest.raises(SecurityError):
        check_csrf(None, "abc", mutating=True)
    check_csrf(None, None, mutating=False)


def test_redaction() -> None:
    text = (
        "key sk-abcdefghijklmnopqrstuvwxyz123456 and "
        "ghp_abcdefghijklmnopqrstuv and "
        "Bearer abcdefghijklmnopqr and "
        "AKIAAAAAAAAAAAAAAAAA and "
        "AIzaabcdefghijklmnopqrstuv"
    )
    out = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
    assert "ghp_abcdefghijklmnopqrstuv" not in out
    assert "Bearer abcdefghijklmnopqr" not in out
    assert "[REDACTED" in out


def test_path_dotdot() -> None:
    with pytest.raises(SecurityError):
        resolve_under_workspace("../outside.txt")


def test_path_absolute() -> None:
    with pytest.raises(SecurityError):
        resolve_under_workspace("C:/Windows/notepad.exe")
    with pytest.raises(SecurityError):
        resolve_under_workspace("/etc/passwd")


def test_path_git() -> None:
    with pytest.raises(SecurityError):
        resolve_under_workspace(".git/HEAD")


def test_path_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Use real workspace-relative path that exists conceptually
    p = resolve_under_workspace("CONTRACT.md")
    assert p.name == "CONTRACT.md"


def test_reparse_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a symlink/junction would escape workspace, reject."""
    if os.name != "nt":
        pytest.skip("Windows reparse coverage")
    # Create a symlink under workspace pointing outside — if creation fails, skip
    from app.config import WORKSPACE

    link = WORKSPACE / ".orca-runs" / "_test_escape_link"
    target = Path("C:/Windows")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink(missing_ok=True)
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Cannot create symlink (needs privilege)")
    try:
        with pytest.raises(SecurityError):
            resolve_under_workspace(".orca-runs/_test_escape_link/system32")
    finally:
        try:
            link.unlink(missing_ok=True)
        except OSError:
            pass


def test_http_host_origin_token(client) -> None:
    c, _runner, token = client
    # Bad host
    r = c.get("/api/health", headers={"Host": "evil:8765", "X-CC-Token": token})
    assert r.status_code == 403
    # Bad origin on mutation
    r = c.post(
        "/api/missions",
        headers={
            "Host": "127.0.0.1:8765",
            "Origin": "http://evil",
            "X-CC-Token": token,
            "Content-Type": "application/json",
        },
        json={
            "title": "t",
            "objective": "o",
            "quality_requirements": "q",
            "scope": "s",
            "constraints": "c",
            "coordinator": "cursor",
        },
    )
    assert r.status_code == 403
    # Missing CSRF (override default client header)
    r = c.post(
        "/api/missions",
        headers={
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "X-CC-Token": "",
        },
        json={
            "title": "t",
            "objective": "o",
            "quality_requirements": "q",
            "scope": "s",
            "constraints": "c",
            "coordinator": "cursor",
        },
    )
    assert r.status_code == 403
