"""Shared fixtures: fake Orca adapter, isolated DB, TestClient."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.db import init_db
from app.orca_adapter import OrcaAdapter, OrcaError


class _FakeCliError(Exception):
    pass


class FakeRunner:
    """Records argv and returns scripted JSON responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.mutation_ops: list[str] = []
        self._lock = threading.Lock()
        self.terminals: dict[str, dict] = {"term_coord": {"handle": "term_coord", "ok": True}}
        self.runs: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.dispatches: dict[str, dict] = {}
        self._seq = 0
        self.fail_ops: set[str] = set()
        self.worker_states: dict[str, str] = {}
        self.plan_on_done: Path | None = None

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def __call__(self, argv: list[str], **kwargs: Any) -> MagicMock:
        assert kwargs.get("shell") is False
        env = kwargs.get("env") or {}
        assert "ORCA_AGENT_SESSION_ID" not in env
        assert "ORCA_AGENT_SESSION_SPAWN_TOKEN" not in env
        with self._lock:
            self.calls.append(list(argv))
            args = argv[1:]  # drop exe
            proc = MagicMock()
            try:
                result = self._dispatch(args)
            except _FakeCliError as exc:
                proc.returncode = 1
                proc.stdout = b""
                proc.stderr = str(exc).encode("utf-8")
                return proc
            proc.returncode = 0
            proc.stdout = json.dumps(result).encode("utf-8")
            proc.stderr = b""
            return proc

    def _dispatch(self, args: list[str]) -> dict[str, Any]:
        if args[:2] == ["status", "--json"]:
            return {"ok": True, "version": "fake-orca", "result": {"app": {"running": True}}}
        if args[:3] == ["account", "list", "--json"]:
            return {
                "accounts": [
                    {"agent": "cursor", "planType": "free", "status": "ok"},
                    {"agent": "grok", "status": "ok", "oauth": True, "weeklyWindow": {"remaining": 1}},
                    {"agent": "codex", "quotaUsed": 100, "status": "ok"},
                ]
            }
        if args[:3] == ["agent", "installed", "--json"]:
            return {
                "agents": [
                    {"id": "cursor", "name": "Cursor", "installed": True},
                    {"id": "grok", "name": "Grok", "installed": True},
                    {"id": "opencode", "name": "OpenCode", "installed": True},
                    {"id": "codex", "name": "Codex", "installed": True},
                    {"id": "claude", "name": "Claude", "installed": True},
                ]
            }
        if args[:2] == ["terminal", "create"]:
            handle = self._next("term")
            self.terminals[handle] = {"handle": handle, "ok": True}
            return {"handle": handle}
        if args[:2] == ["terminal", "show"]:
            idx = args.index("--terminal")
            handle = args[idx + 1]
            if handle not in self.terminals:
                # Signal via exception consumed by __call__ into non-zero proc
                raise _FakeCliError("terminal gone")
            return {"handle": handle, "ok": True}
        if args[:2] == ["terminal", "close"]:
            idx = args.index("--terminal")
            handle = args[idx + 1]
            self.terminals.pop(handle, None)
            return {"ok": True}
        if args[:2] == ["orchestration", "run-create"]:
            rid = self._next("run")
            self.runs[rid] = {"id": rid}
            return {"run_id": rid}
        if args[:2] == ["orchestration", "run-use"]:
            idx = args.index("--id")
            return {"run_id": args[idx + 1], "ok": True}
        if args[:2] == ["orchestration", "task-create"]:
            tid = self._next("task")
            self.tasks[tid] = {"id": tid}
            return {"task_id": tid}
        if args[:2] == ["orchestration", "task-list"]:
            return {"tasks": list(self.tasks.values())}
        if args[:2] == ["orchestration", "task-update"]:
            return {"ok": True}
        if args[:2] == ["orchestration", "worker-start"]:
            did = self._next("disp")
            self.dispatches[did] = {"dispatch_id": did, "state": "running", "liveness": "alive"}
            self.worker_states[did] = "running"
            return {"dispatch_id": did}
        if args[:2] == ["orchestration", "worker-show"]:
            idx = args.index("--dispatch")
            did = args[idx + 1]
            state = self.worker_states.get(did, "running")
            return {"dispatch_id": did, "state": state, "liveness": "alive"}
        if args[:2] == ["orchestration", "worker-read"]:
            return {"messages": [{"text": "worker output ok"}]}
        if args[:2] == ["orchestration", "worker-list"]:
            return {
                "workers": [
                    {"dispatch_id": d, "state": s} for d, s in self.worker_states.items()
                ]
            }
        if args[:2] == ["orchestration", "worker-stop"]:
            idx = args.index("--dispatch")
            did = args[idx + 1]
            self.worker_states[did] = "stopped"
            return {"ok": True}
        if args[:2] == ["orchestration", "worker-release"]:
            return {"ok": True}
        if args[:2] == ["orchestration", "send"]:
            return {"ok": True}
        raise OrcaError("unknown", f"Unhandled fake args: {args}")


@pytest.fixture()
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture()
def client(tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    # Point runs dir artifacts under tmp via monkeypatch where needed
    main_mod.set_db_path(db_path)
    main_mod._capabilities_cache = {"at": 0.0, "data": None}
    main_mod._session_tokens = set()

    from app import config as config_mod

    def factory_adapter() -> OrcaAdapter:
        return OrcaAdapter(runner=fake_runner, workspace=config_mod.WORKSPACE)

    main_mod.set_adapter_factory(factory_adapter)
    init_db(db_path)

    with TestClient(main_mod.app, base_url="http://127.0.0.1:8765") as c:
        # Bootstrap session
        r = c.get("/api/session", headers={"Host": "127.0.0.1:8765"})
        token = r.json()["token"]
        c.headers.update({"Host": "127.0.0.1:8765", "X-CC-Token": token, "Origin": "http://127.0.0.1:8765"})
        yield c, fake_runner, token

    main_mod.set_adapter_factory(None)
    main_mod.set_db_path(None)


def valid_plan(**overrides: Any) -> dict[str, Any]:
    plan = {
        "schema_version": 1,
        "mission_summary": "Build a tiny module",
        "architecture": "Single file",
        "selection_rationale": "cursor for impl",
        "departments": [{"id": "eng", "name": "Engineering"}],
        "tasks": [
            {
                "id": "T1",
                "title": "Implement",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": [],
                "write_scope": ["src/example.py"],
                "inputs": [],
                "outputs": ["src/example.py"],
                "acceptance": ["file exists"],
                "validation": ["python -m pytest tests/test_example.py -q"],
                "review_required": False,
            }
        ],
        "quality_gates": [{"id": "QG1", "description": "tests pass", "evidence": "pytest"}],
        "risks": ["scope creep"],
    }
    plan.update(overrides)
    return plan
