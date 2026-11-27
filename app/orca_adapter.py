"""Allowlisted Orca CLI adapter. Fixed argv arrays only; no shell interpolation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.config import ADAPTER_TIMEOUT, ORCA_EXE, OUTPUT_CAP, WORKSPACE
from app.security import redact


class OrcaError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


Runner = Callable[..., subprocess.CompletedProcess]


class OrcaAdapter:
    """Constructs only contract-allowlisted Orca CLI operations."""

    MUTATING_OPS = frozenset(
        {
            "terminal_create",
            "terminal_close",
            "run_create",
            "run_use",
            "task_create",
            "task_update",
            "worker_start",
            "worker_stop",
            "worker_release",
            "send",
        }
    )

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        orca_exe: Path | None = None,
        runner: Runner | None = None,
        timeout: float = ADAPTER_TIMEOUT,
    ) -> None:
        self.workspace = (workspace or WORKSPACE).resolve()
        self.orca_exe = Path(orca_exe or ORCA_EXE)
        self._runner = runner
        self.timeout = timeout
        self.mutation_calls: list[str] = []
        self.all_calls: list[str] = []

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("ORCA_AGENT_SESSION_ID", None)
        env.pop("ORCA_AGENT_SESSION_SPAWN_TOKEN", None)
        return env

    def _cap_output(self, stdout: bytes | str, stderr: bytes | str) -> tuple[str, str, bool]:
        def to_bytes(v: bytes | str) -> bytes:
            if isinstance(v, bytes):
                return v
            return v.encode("utf-8", errors="replace")

        out_b = to_bytes(stdout or b"")
        err_b = to_bytes(stderr or b"")
        total = len(out_b) + len(err_b)
        truncated = total > OUTPUT_CAP
        if truncated:
            # Prefer keeping stdout; trim stderr then stdout
            remain = OUTPUT_CAP
            out_keep = out_b[: min(len(out_b), remain)]
            remain -= len(out_keep)
            err_keep = err_b[:remain]
        else:
            out_keep, err_keep = out_b, err_b
        out_t = out_keep.decode("utf-8", errors="replace")
        err_t = err_keep.decode("utf-8", errors="replace")
        return redact(out_t), redact(err_t), truncated

    def _run(self, op_name: str, args: list[str]) -> dict[str, Any]:
        argv = [str(self.orca_exe), *args]
        self.all_calls.append(op_name)
        if op_name in self.MUTATING_OPS:
            self.mutation_calls.append(op_name)

        if self._runner is not None:
            proc = self._runner(
                argv,
                shell=False,
                cwd=str(self.workspace),
                timeout=self.timeout,
                capture_output=True,
                env=self._child_env(),
            )
        else:
            try:
                proc = subprocess.run(
                    argv,
                    shell=False,
                    cwd=str(self.workspace),
                    timeout=self.timeout,
                    capture_output=True,
                    env=self._child_env(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise OrcaError("timeout", f"Orca CLI timed out for {op_name}", {"op": op_name}) from exc
            except FileNotFoundError as exc:
                raise OrcaError("not_found", "Orca executable not found", {"exe": str(self.orca_exe)}) from exc
            except OSError as exc:
                raise OrcaError("os_error", str(exc), {"op": op_name}) from exc

        stdout, stderr, truncated = self._cap_output(proc.stdout or b"", proc.stderr or b"")
        if proc.returncode != 0:
            raise OrcaError(
                "cli_error",
                f"Orca CLI {op_name} exited {proc.returncode}",
                {
                    "op": op_name,
                    "returncode": proc.returncode,
                    "stderr": stderr[:4000],
                    "truncated": truncated,
                },
            )
        text = stdout.strip()
        if not text:
            raise OrcaError("invalid_json", f"Empty JSON from {op_name}", {"op": op_name})
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrcaError(
                "invalid_json",
                f"Non-JSON output from {op_name}",
                {"op": op_name, "stdout_preview": text[:500]},
            ) from exc
        if not isinstance(data, dict):
            raise OrcaError("invalid_json", f"JSON root must be object for {op_name}", {"op": op_name})
        return data

    def _require_from(self, from_handle: str | None) -> str:
        if not from_handle:
            raise OrcaError("missing_from", "--from handle is required for this mutation")
        return from_handle

    def _under_runs(self, abs_path: str | Path) -> str:
        path = Path(abs_path).resolve()
        runs = (self.workspace / ".orca-runs").resolve()
        try:
            path.relative_to(runs)
        except ValueError as exc:
            raise OrcaError("bad_path", "Path must be under .orca-runs", {"path": str(path)}) from exc
        return str(path)

    # --- allowlisted operations ---

    def status(self) -> dict[str, Any]:
        return self._run("status", ["status", "--json"])

    def account_list(self) -> dict[str, Any]:
        return self._run("account_list", ["account", "list", "--json"])

    def agent_installed(self) -> dict[str, Any]:
        return self._run("agent_installed", ["agent", "installed", "--json"])

    def agent_list(self) -> dict[str, Any]:
        return self._run("agent_list", ["agent", "list", "--json"])

    def terminal_create(self, *, title: str) -> dict[str, Any]:
        return self._run(
            "terminal_create",
            [
                "terminal",
                "create",
                "--worktree",
                f"path:{self.workspace}",
                "--title",
                title,
                "--keep-open",
                "--json",
            ],
        )

    def terminal_show(self, *, terminal: str) -> dict[str, Any]:
        return self._run("terminal_show", ["terminal", "show", "--terminal", terminal, "--json"])

    def terminal_close(self, *, terminal: str) -> dict[str, Any]:
        return self._run("terminal_close", ["terminal", "close", "--terminal", terminal, "--json"])

    def run_create(self, *, objective: str, from_handle: str) -> dict[str, Any]:
        handle = self._require_from(from_handle)
        return self._run(
            "run_create",
            [
                "orchestration",
                "run-create",
                "--environment",
                "local",
                "--objective",
                objective,
                "--from",
                handle,
                "--json",
            ],
        )

    def run_use(self, *, run_id: str, from_handle: str) -> dict[str, Any]:
        handle = self._require_from(from_handle)
        return self._run(
            "run_use",
            [
                "orchestration",
                "run-use",
                "--id",
                run_id,
                "--from",
                handle,
                "--json",
            ],
        )

    def task_create(
        self,
        *,
        run_id: str,
        task_title: str,
        spec_file: str | Path,
        from_handle: str,
        deps: list[str] | None = None,
    ) -> dict[str, Any]:
        handle = self._require_from(from_handle)
        spec = self._under_runs(spec_file)
        args = [
            "orchestration",
            "task-create",
            "--environment",
            "local",
            "--run",
            run_id,
            "--task-title",
            task_title,
            "--spec-file",
            spec,
        ]
        if deps is not None:
            args.extend(["--deps", json.dumps(deps)])
        args.extend(["--from", handle, "--json"])
        return self._run("task_create", args)

    def task_list(self, *, run_id: str, from_handle: str | None = None) -> dict[str, Any]:
        args = [
            "orchestration",
            "task-list",
            "--environment",
            "local",
            "--run",
            run_id,
        ]
        if from_handle:
            args.extend(["--from", from_handle])
        args.append("--json")
        return self._run("task_list", args)

    def task_update(
        self,
        *,
        task_id: str,
        from_handle: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        handle = self._require_from(from_handle)
        args = [
            "orchestration",
            "task-update",
            "--environment",
            "local",
            "--task",
            task_id,
        ]
        if status is not None:
            args.extend(["--status", status])
        args.extend(["--from", handle, "--json"])
        return self._run("task_update", args)

    def worker_start(
        self,
        *,
        run_id: str,
        task_id: str,
        agent: str,
        from_handle: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        handle = self._require_from(from_handle)
        args = [
            "orchestration",
            "worker-start",
            "--environment",
            "local",
            "--run",
            run_id,
            "--task",
            task_id,
            "--worktree",
            f"path:{self.workspace}",
            "--agent",
            agent,
        ]
        if model and model != "unknown":
            args.extend(["--model", model])
        args.extend(["--from", handle, "--json"])
        return self._run("worker_start", args)

    def worker_show(self, *, dispatch_id: str) -> dict[str, Any]:
        return self._run(
            "worker_show",
            ["orchestration", "worker-show", "--dispatch", dispatch_id, "--json"],
        )

    def worker_read(self, *, dispatch_id: str, limit: int) -> dict[str, Any]:
        return self._run(
            "worker_read",
            [
                "orchestration",
                "worker-read",
                "--dispatch",
                dispatch_id,
                "--limit",
                str(limit),
                "--json",
            ],
        )

    def worker_list(self, *, run_id: str) -> dict[str, Any]:
        return self._run(
            "worker_list",
            [
                "orchestration",
                "worker-list",
                "--environment",
                "local",
                "--run",
                run_id,
                "--json",
            ],
        )

    def worker_stop(self, *, dispatch_id: str) -> dict[str, Any]:
        return self._run(
            "worker_stop",
            ["orchestration", "worker-stop", "--dispatch", dispatch_id, "--json"],
        )

    def worker_release(self, *, dispatch_id: str) -> dict[str, Any]:
        return self._run(
            "worker_release",
            ["orchestration", "worker-release", "--dispatch", dispatch_id, "--json"],
        )

    def send(self, *, dispatch_id: str, subject: str, body_file: str | Path) -> dict[str, Any]:
        body = self._under_runs(body_file)
        return self._run(
            "send",
            [
                "orchestration",
                "send",
                "--to",
                f"dispatch:{dispatch_id}",
                "--subject",
                subject,
                "--body-file",
                body,
                "--json",
            ],
        )
