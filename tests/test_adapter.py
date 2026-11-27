"""Adapter argv allowlist and shell-free execution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import WORKSPACE
from app.orca_adapter import OrcaAdapter, OrcaError


def test_shell_false_and_env_scrub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "sess")
    monkeypatch.setenv("ORCA_AGENT_SESSION_SPAWN_TOKEN", "tok")
    seen: dict = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = b'{"ok":true,"version":"1"}'
        proc.stderr = b""
        return proc

    adapter = OrcaAdapter(runner=runner, workspace=WORKSPACE)
    out = adapter.status()
    assert out["ok"] is True
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(WORKSPACE.resolve())
    env = seen["kwargs"]["env"]
    assert "ORCA_AGENT_SESSION_ID" not in env
    assert "ORCA_AGENT_SESSION_SPAWN_TOKEN" not in env
    assert seen["argv"][1:] == ["status", "--json"]


def test_allowlisted_shapes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv[1:])
        proc = MagicMock()
        proc.returncode = 0
        # Return plausible payloads
        op = argv[1]
        if op == "terminal" and argv[2] == "create":
            body = {"handle": "term_1"}
        elif op == "orchestration" and argv[2] == "run-create":
            body = {"run_id": "run_1"}
        elif op == "orchestration" and argv[2] == "task-create":
            body = {"task_id": "task_1"}
        elif op == "orchestration" and argv[2] == "worker-start":
            body = {"dispatch_id": "disp_1"}
        else:
            body = {"ok": True}
        proc.stdout = json.dumps(body).encode()
        proc.stderr = b""
        return proc

    runs = WORKSPACE / ".orca-runs" / "mis_test"
    runs.mkdir(parents=True, exist_ok=True)
    spec = runs / "spec.md"
    spec.write_text("hi", encoding="utf-8")

    a = OrcaAdapter(runner=runner, workspace=WORKSPACE)
    a.terminal_create(title="t")
    a.run_create(objective="o", from_handle="term_1")
    a.task_create(run_id="run_1", task_title="T", spec_file=spec, from_handle="term_1")
    a.worker_start(run_id="run_1", task_id="task_1", agent="cursor", from_handle="term_1")
    a.worker_show(dispatch_id="disp_1")
    a.worker_list(run_id="run_1")

    # Ensure --from present on mutations
    assert any("--from" in c and "run-create" in c for c in calls)
    assert any("--from" in c and "worker-start" in c for c in calls)
    # Read-only worker-show has no --from
    show = next(c for c in calls if "worker-show" in c)
    assert "--from" not in show
    # worktree path form
    start = next(c for c in calls if "worker-start" in c)
    assert f"path:{WORKSPACE.resolve()}" in " ".join(start) or any(
        x.startswith("path:") for x in start
    )


def test_non_json_is_error() -> None:
    def runner(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = b"not-json"
        proc.stderr = b""
        return proc

    a = OrcaAdapter(runner=runner, workspace=WORKSPACE)
    with pytest.raises(OrcaError) as ei:
        a.status()
    assert ei.value.code == "invalid_json"


def test_nonzero_exit() -> None:
    def runner(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 2
        proc.stdout = b""
        proc.stderr = b"fail"
        return proc

    a = OrcaAdapter(runner=runner, workspace=WORKSPACE)
    with pytest.raises(OrcaError) as ei:
        a.status()
    assert ei.value.code == "cli_error"


def test_spec_must_be_under_runs() -> None:
    def runner(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = b'{"task_id":"t"}'
        proc.stderr = b""
        return proc

    a = OrcaAdapter(runner=runner, workspace=WORKSPACE)
    with pytest.raises(OrcaError):
        a.task_create(
            run_id="r",
            task_title="t",
            spec_file=WORKSPACE / "CONTRACT.md",
            from_handle="term",
        )
