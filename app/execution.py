"""Mission planning, execution, evidence, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import (
    COORDINATOR_TERMINAL_TITLE,
    LOCK_FILE,
    MAX_PLANNER_REPAIRS,
    MAX_TASK_RETRIES,
    RUNS_DIR,
    TERMINAL_MISSION_STATUSES,
    VALIDATION_TIMEOUT,
    WORKER_CAP_DEFAULT,
    WORKER_COMPLETION_CONTRACT,
    WORKER_READ_LIMIT,
    WORKSPACE,
)
from app.db import (
    add_audit,
    add_event,
    dumps,
    get_setting,
    loads,
    new_id,
    set_setting,
    utc_now,
    write_checkpoint,
)
from app.orca_adapter import OrcaAdapter, OrcaError
from app.native_payloads import project_status
from app.plan import (
    PlanValidationError,
    load_and_validate_plan_file,
    planner_spec_text,
    topological_order,
    validate_plan,
)
from app.security import SecurityError, cap_bytes, redact, resolve_under_workspace
from app.team_workflow import compile_team_workflow, team_planner_spec
from app.free_models import probe_opencode, selected_route


class AppError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class MissionStartLock:
    """Cross-process lock file for mission start. External Orca clients can bypass the app worker cap."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or LOCK_FILE
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "a+b")
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                self._fh.write(b"0")
                self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self) -> "MissionStartLock":
        if not self.acquire():
            raise AppError("locked", "Another mission start is in progress")
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def parse_validation_command(cmd: str) -> list[str]:
    """Allow only `python -m pytest <rel> -q` or `python <rel>`."""
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError as exc:
        raise AppError("unsupported_validation", f"Cannot parse validation: {cmd}") from exc
    if not parts:
        raise AppError("unsupported_validation", "Empty validation command")
    exe = parts[0].lower()
    if exe not in ("python", "python3", "py"):
        raise AppError("unsupported_validation", f"Unsupported validation: {cmd}")
    if len(parts) == 5 and parts[1] == "-m" and parts[2] == "pytest" and parts[4] == "-q":
        rel = parts[3].replace("\\", "/")
        resolve_under_workspace(rel)
        return [sys.executable, "-m", "pytest", rel, "-q"]
    if len(parts) == 2:
        rel = parts[1].replace("\\", "/")
        resolve_under_workspace(rel)
        return [sys.executable, rel]
    raise AppError("unsupported_validation", f"Unsupported validation: {cmd}")


def run_validation(cmd: str, *, workspace: Path | None = None) -> dict[str, Any]:
    cwd = workspace or WORKSPACE
    try:
        argv = parse_validation_command(cmd)
    except AppError as exc:
        return {
            "command": cmd,
            "exit_code": None,
            "output": exc.message,
            "status": "unsupported_validation",
            "ts": utc_now(),
        }
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=str(cwd),
            timeout=VALIDATION_TIMEOUT,
            capture_output=True,
            check=False,
        )
        out = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
        text, _ = cap_bytes(out, 64 * 1024)
        return {
            "command": cmd,
            "exit_code": proc.returncode,
            "output": redact(text),
            "status": "ok" if proc.returncode == 0 else "failed",
            "ts": utc_now(),
        }
    except subprocess.TimeoutExpired:
        return {
            "command": cmd,
            "exit_code": None,
            "output": "validation timed out",
            "status": "timeout",
            "ts": utc_now(),
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def extract_handle(payload: dict[str, Any]) -> str | None:
    if payload.get("ok") is False:
        return None
    if "result" in payload:
        result = payload["result"]
        return extract_handle(result) if isinstance(result, dict) else None
    terminal = payload.get("terminal")
    if isinstance(terminal, dict):
        return extract_handle(terminal)
    for key in ("handle", "terminal", "id", "terminalHandle"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def extract_id(payload: dict[str, Any], *keys: str) -> str | None:
    if payload.get("ok") is False:
        return None
    if "result" in payload:
        result = payload["result"]
        return extract_id(result, *keys) if isinstance(result, dict) else None
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


class MissionService:
    def __init__(self, conn: sqlite3.Connection, adapter: OrcaAdapter) -> None:
        self.conn = conn
        self.adapter = adapter

    # --- coordinator terminal ---

    def ensure_coordinator_terminal(self) -> str:
        stored = get_setting(self.conn, "coordinator_terminal", "") or ""
        if stored:
            try:
                self.adapter.terminal_show(terminal=stored)
                return stored
            except OrcaError:
                pass
        data = self.adapter.terminal_create(title=COORDINATOR_TERMINAL_TITLE)
        handle = extract_handle(data)
        if not handle:
            raise AppError("terminal", "terminal create did not return a handle", {"data_keys": list(data)})
        set_setting(self.conn, "coordinator_terminal", handle)
        add_audit(self.conn, "coordinator_terminal", {"handle": handle})
        # Re-bind existing runs
        rows = self.conn.execute(
            "SELECT id, run_id FROM missions WHERE run_id IS NOT NULL AND status NOT IN ('succeeded','failed','cancelled')"
        ).fetchall()
        for row in rows:
            try:
                self.adapter.run_use(run_id=row["run_id"], from_handle=handle)
            except OrcaError:
                self._mark_unknown(row["id"], "run-use failed after terminal recovery")
        self.conn.commit()
        return handle

    def _mark_unknown(self, mission_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE missions SET status = 'unknown', updated_at = ? WHERE id = ?",
            (utc_now(), mission_id),
        )
        add_event(self.conn, mission_id, "unknown", reason)

    # --- capabilities ---

    def capabilities(self) -> dict[str, Any]:
        try:
            agents_raw = self.adapter.agent_installed()
        except OrcaError as exc:
            agents_raw = {"error": exc.message}
        try:
            accounts = self.adapter.account_list()
        except OrcaError as exc:
            accounts = {"error": exc.message}
        try:
            status = self.adapter.status()
        except OrcaError as exc:
            status = {"error": exc.message}

        agents_list = self._normalize_agents(agents_raw, accounts)
        worker_cap = int(get_setting(self.conn, "worker_cap", str(WORKER_CAP_DEFAULT)) or WORKER_CAP_DEFAULT)
        return {
            "workspace": str(WORKSPACE),
            "worker_cap": worker_cap,
            "checked_at": utc_now(),
            "orca": project_status(status),
            "agents": agents_list,
            "note": (
                "App-enforced worker_cap applies only to dispatches started by this app; "
                "external Orca clients can bypass the app cap."
            ),
        }

    def _normalize_agents(self, agents_raw: dict, accounts: dict) -> list[dict[str, Any]]:
        if agents_raw.get("ok") is False:
            agents_raw = {}
        if accounts.get("ok") is False:
            accounts = {}
        installed = agents_raw.get("agents") or agents_raw.get("result") or agents_raw.get("installed") or []
        if isinstance(installed, dict):
            installed = installed.get("agents") or list(installed.values())
        if not isinstance(installed, list):
            installed = []

        account_rows = accounts.get("accounts") or accounts.get("result") or []
        if isinstance(account_rows, dict):
            account_rows = account_rows.get("accounts") or []
        if not isinstance(account_rows, list):
            account_rows = []

        # Native 1.4.x account-list groups provider usage under result.rateLimits;
        # provider-specific .accounts lists may be empty despite valid usage data.
        account_data = accounts.get("result", accounts)
        rate_limits = account_data.get("rateLimits") if isinstance(account_data, dict) else None
        if isinstance(rate_limits, dict):
            native_rows = []
            for aid, row in rate_limits.items():
                if not isinstance(aid, str) or not isinstance(row, dict):
                    continue
                if row.get("provider") not in (None, aid):
                    native_rows.append({"agent": aid, "status": "unavailable", "_source": "result.rateLimits"})
                    continue
                native_rows.append({**row, "agent": aid, "_source": "result.rateLimits",
                                    **({"status": "error"} if row.get("error") is not None else {})})
            # Native status is authoritative; legacy rows must not override a
            # reported native error or unavailable status.
            account_rows = native_rows + account_rows

        by_id: dict[str, dict] = {}
        for item in installed:
            if isinstance(item, str):
                by_id[item] = {"id": item, "installed": True}
            elif isinstance(item, dict):
                aid = item.get("id") or item.get("agent") or item.get("name")
                if aid:
                    by_id[str(aid)] = {**item, "id": str(aid), "installed": True}

        known = ["cursor", "grok", "opencode", "codex", "claude", "copilot", "kilo", "freebuff", "hermes", "opencode2"]
        for kid in known:
            by_id.setdefault(kid, {"id": kid, "installed": False})

        out: list[dict[str, Any]] = []
        for aid, info in sorted(by_id.items(), key=lambda x: x[0]):
            out.append(self._eligibility(aid, info, account_rows))
        return out

    def _eligibility(self, agent_id: str, info: dict, accounts: list) -> dict[str, Any]:
        installed = bool(info.get("installed"))
        name = str(info.get("name") or agent_id)
        billing = None
        quota = None
        model = "unknown"
        evidence: list[str] = []
        reason = ""
        eligible = False

        acct = next((a for a in accounts if isinstance(a, dict) and (a.get("agent") == agent_id or a.get("id") == agent_id)), None)
        if acct is not None:
            if acct.get("_source") == "result.rateLimits":
                evidence.append("source=result.rateLimits")
            reported_status = acct.get("status")
            evidence.append(f"status={reported_status}" if reported_status in ("ok", "error", "unavailable") else "status=unknown")

        if agent_id == "cursor":
            plan = (acct or {}).get("planType") or info.get("planType")
            status_ok = (acct or info).get("status") == "ok"
            billing = "included_plan"
            if installed and plan == "free" and status_ok:
                eligible = True
                reason = "free plan included"
            else:
                reason = "requires planType==free and status ok"
            evidence.append(f"planType={plan}")
        elif agent_id == "grok":
            status_ok = (acct or info).get("status") == "ok"
            oauth = bool((acct or info).get("oauth") or (acct or info).get("auth") == "oauth")
            weekly = (acct or info).get("weeklyWindow") or (acct or info).get("weekly_window") or (acct or info).get("weekly")
            billing = "included_allowance"
            if installed and status_ok and oauth and isinstance(weekly, dict) and weekly:
                eligible = True
                reason = "status ok with OAuth weekly allowance"
            else:
                reason = "requires status ok, OAuth, weekly window"
            if weekly is None and acct is not None:
                quota = None
                evidence.append("weekly window missing")
            evidence.append("oauth check")
        elif agent_id == "opencode":
            billing = "free_models"
            # A client being installed is not evidence of its selected model's
            # price. Until a pinned free route is verified, do not dispatch it.
            catalog = loads(get_setting(self.conn, "free_model_catalog"), {})
            pinned = get_setting(self.conn, "free_model_pin")
            route = selected_route(catalog, pinned)
            eligible = bool(installed and route)
            reason = "pinned zero-price tool-capable route; quota unknown" if eligible else "free model route unverified; dispatch blocked"
            model = pinned if route else "unknown"
            if route: evidence.extend(["source=OpenCode model catalog", "input_price=0", "output_price=0", "tool_call=true"])
            from app.api_models import APIConnections
            custom = APIConnections(self.conn, WORKSPACE).routes()
            enabled = {r["id"] for r in custom if r["eligible"]}
            user_pin = get_setting(self.conn, "user_api_pin")
            if enabled:
                eligible = installed
                if not route:
                    reason = "Choose an enabled API model; its cost and quota are unknown"
                    billing = "user_supplied_api"
            if user_pin:
                model = user_pin if user_pin in enabled else "unknown"
                eligible = installed and user_pin in enabled
                billing = "user_supplied_api"
                reason = "Your explicitly selected API; cost and quota unknown" if eligible else "Selected API needs a new test; no fallback"
                evidence = ["source=user API settings", "price=unknown", "quota=unknown"]
        elif agent_id == "codex":
            billing = "quota"
            source = acct or info
            used = next((source[k] for k in ("quotaUsed", "quota_used", "quota") if source.get(k) is not None), None)
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                quota = used
                eligible = installed and 0 <= used < 100 and source.get("status") == "ok"
                reason = "eligible only if quota < 100%"
            else:
                quota = None
                eligible = False
                reason = "quota unavailable; treating as ineligible"
            evidence.append(f"quota={used}")
        elif agent_id == "claude":
            billing = "api_key_metered"
            eligible = False
            reason = "API-key billing is paid metered"
        else:
            billing = "unverified"
            eligible = False
            reason = "unverified billing/route"

        return {
            "id": agent_id,
            "name": name,
            "installed": installed,
            "eligible": eligible and installed,
            "billing": billing,
            "quota": quota,
            "model": model,
            "evidence": evidence,
            "reason": reason,
        }

    def eligible_agent_ids(self) -> set[str]:
        caps = self.capabilities()
        return {a["id"] for a in caps["agents"] if a.get("eligible")}

    def refresh_free_models(self) -> dict[str, Any]:
        catalog = probe_opencode()
        set_setting(self.conn, "free_model_catalog", dumps(catalog))
        self.conn.commit()
        return {**catalog, "selected": get_setting(self.conn, "free_model_pin")}

    def select_free_model(self, model: str) -> dict[str, Any]:
        catalog = loads(get_setting(self.conn, "free_model_catalog"), {})
        if not selected_route(catalog, model):
            raise AppError("model_unverified", "Refresh the free catalog and choose a verified route")
        set_setting(self.conn, "free_model_pin", model)
        set_setting(self.conn, "user_api_pin", "")
        add_audit(self.conn, "select_free_model", {"model": model})
        self.conn.commit()
        return {**catalog, "selected": model}

    def require_eligible(self, agent: str, model: str | None = None) -> str | None:
        """Check current evidence before any worker dispatch, never a UI cache."""
        requested = model if model not in (None, "", "unknown") else get_setting(self.conn, "user_api_pin")
        custom = agent == "opencode" and isinstance(requested, str) and requested.startswith("mdapi_")
        if agent == "opencode" and not custom:
            self.refresh_free_models()
        capabilities = self.capabilities()
        if not capabilities["orca"]["available"]:
            raise AppError("orca_unavailable", "Orca did not report a ready, connected runtime")
        row = next((a for a in capabilities["agents"] if a["id"] == agent), None)
        if not row or (not row["eligible"] and not (agent == "opencode" and row.get("installed"))):
            raise AppError("agent_ineligible", f"Agent {agent} is not currently eligible",
                           {"reason": row["reason"] if row else "agent not reported"})
        if agent == "opencode":
            if custom:
                from app.api_models import APIConnections, APIModelError
                try:
                    return APIConnections(self.conn, WORKSPACE).require(requested)
                except APIModelError as exc:
                    raise AppError(exc.code, exc.message) from exc
            selected = model if model not in (None, "", "unknown") else row["model"]
            catalog = loads(get_setting(self.conn, "free_model_catalog"), {})
            if not selected_route(catalog, selected):
                raise AppError("model_unverified", "This explicit OpenCode model has no current zero-price evidence")
            return selected
        if model not in (None, "", "unknown", row.get("model")):
            raise AppError("model_unverified", "Explicit model route has not been verified")
        return model

    # --- missions CRUD ---

    def create_mission(self, body: dict[str, Any]) -> dict[str, Any]:
        required = ("title", "objective", "quality_requirements", "scope", "constraints", "coordinator")
        for key in required:
            if not isinstance(body.get(key), str) or not body[key].strip():
                raise AppError("validation", f"{key} is required")
        mid = new_id("mis")
        now = utc_now()
        self.conn.execute(
            """INSERT INTO missions(
                id, title, objective, quality_requirements, scope, constraints, coordinator,
                workspace, status, plan_json, plan_version, plan_status, plan_errors_json,
                run_id, planner_dispatch_id, repair_count, created_at, updated_at, approved_at, cancel_requested
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mid,
                body["title"].strip(),
                body["objective"].strip(),
                body["quality_requirements"].strip(),
                body["scope"].strip(),
                body["constraints"].strip(),
                body["coordinator"].strip(),
                str(WORKSPACE),
                "draft",
                None,
                0,
                "none",
                dumps([]),
                None,
                None,
                0,
                now,
                now,
                None,
                0,
            ),
        )
        (RUNS_DIR / mid).mkdir(parents=True, exist_ok=True)
        add_event(self.conn, mid, "created", "Mission created")
        add_audit(self.conn, "mission_create", {"mission_id": mid})
        self.conn.commit()
        return self.get_mission(mid)

    def list_missions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, title, status, plan_status, created_at, updated_at, coordinator FROM missions WHERE id NOT IN (SELECT child_mission_id FROM manager_turns) ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_mission_row(self, mission_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "Mission not found", {"id": mission_id})
        return row

    def get_mission(self, mission_id: str) -> dict[str, Any]:
        m = dict(self.get_mission_row(mission_id))
        tasks = [
            self._task_dict(dict(r))
            for r in self.conn.execute(
                "SELECT * FROM tasks WHERE mission_id = ? ORDER BY created_at", (mission_id,)
            ).fetchall()
        ]
        artifacts = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM artifacts WHERE mission_id = ? ORDER BY created_at", (mission_id,)
            ).fetchall()
        ]
        events = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "type": r["type"],
                "message": r["message"],
                "data": loads(r["data_json"], {}),
            }
            for r in self.conn.execute(
                "SELECT * FROM events WHERE mission_id = ? ORDER BY ts DESC LIMIT 100",
                (mission_id,),
            ).fetchall()
        ]
        plan = loads(m.get("plan_json"), None)
        plan_errors = loads(m.get("plan_errors_json"), [])
        active = self.count_active_dispatches(mission_id=mission_id)
        status = m["status"]
        can = {
            "plan": status in ("draft", "failed", "blocked") and m["plan_status"] != "generating",
            "approve": m["plan_status"] == "valid" and status == "awaiting_approval",
            "start": status == "approved" and m["plan_status"] == "valid",
            "cancel": status not in TERMINAL_MISSION_STATUSES and status != "draft",
            "refresh": True,
            "save_plan": status in ("draft", "awaiting_approval", "approved", "blocked", "failed")
                         and not tasks and active == 0 and m["plan_status"] != "generating",
        }
        if status == "unknown":
            can["start"] = False
            can["plan"] = False
        return {
            "id": m["id"],
            "title": m["title"],
            "objective": m["objective"],
            "quality_requirements": m["quality_requirements"],
            "scope": m["scope"],
            "constraints": m["constraints"],
            "coordinator": m["coordinator"],
            "workspace": m["workspace"],
            "status": status,
            "created_at": m["created_at"],
            "updated_at": m["updated_at"],
            "run_id": m["run_id"],
            "plan": plan,
            "plan_version": m["plan_version"],
            "plan_status": m["plan_status"],
            "plan_errors": plan_errors,
            "planner": {
                "dispatch_id": m["planner_dispatch_id"],
                "state": None,
                "agent": m["coordinator"],
            },
            "tasks": tasks,
            "artifacts": [
                {
                    "id": a["id"],
                    "rel_path": a["rel_path"],
                    "size": a["size"],
                    "sha256": a["sha256"],
                    "kind": a["kind"],
                    "verified": bool(a["verified"]),
                    "created_at": a["created_at"],
                }
                for a in artifacts
            ],
            "events": events,
            "quality": self._quality_summary(m, tasks, artifacts),
            "freshness": utc_now(),
            "active_dispatches": active,
            "global_active_dispatches": self.count_active_dispatches(),
            "native_checked_at": self.conn.execute("SELECT MAX(last_seen_at) FROM dispatches WHERE mission_id = ?", (mission_id,)).fetchone()[0],
            "can": can,
            "repair_count": m["repair_count"],
            "approved_at": m["approved_at"],
            "cancel_requested": bool(m["cancel_requested"]),
        }

    def _task_dict(self, t: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": t["id"],
            "plan_task_id": t["plan_task_id"],
            "title": t["title"],
            "role": t["role"],
            "agent": t["agent"],
            "model": t["model"],
            "deps": loads(t["deps_json"], []),
            "write_scope": loads(t["write_scope_json"], []),
            "outputs": loads(t["outputs_json"], []),
            "acceptance": loads(t["acceptance_json"], []),
            "validation": loads(t["validation_json"], []),
            "review_required": bool(t["review_required"]),
            "status": t["status"],
            "native_task_id": t["native_task_id"],
            "dispatch_id": t["dispatch_id"],
            "attempt": t["attempt"],
            "result_summary": t["result_summary"],
            "started_at": t["started_at"],
            "ended_at": t["ended_at"],
            "evidence": loads(t.get("evidence_json"), []),
        }

    def _quality_summary(self, m: dict, tasks: list[dict], artifacts: list) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        if m["status"] == "succeeded":
            status = "pass"
        elif m["status"] in ("failed", "blocked"):
            status = "fail"
        else:
            status = "pending"
        checks.append({"id": "outputs", "status": status, "detail": f"{len(artifacts)} artifacts"})
        return {"status": status, "checks": checks}

    def count_active_dispatches(self, mission_id: str | None = None) -> int:
        sql = """
            SELECT COUNT(*) AS c FROM dispatches
            WHERE terminal_state IS NULL AND state NOT IN ('done','failed','stopped','cancelled','released')
        """
        args: tuple = ()
        if mission_id:
            sql += " AND mission_id = ?"
            args = (mission_id,)
        return int(self.conn.execute(sql, args).fetchone()["c"])

    # --- planning ---

    def start_plan(self, mission_id: str, *, repair: bool = False) -> dict[str, Any]:
        with MissionStartLock():
            return self._start_plan_locked(mission_id, repair=repair)

    def _start_plan_locked(self, mission_id: str, *, repair: bool = False, team: bool = False) -> dict[str, Any]:
        m = self.get_mission_row(mission_id)
        if m["status"] == "unknown":
            raise AppError("unknown_state", "Mission is in unknown state; cannot plan")
        if repair:
            if m["repair_count"] >= MAX_PLANNER_REPAIRS:
                raise AppError("repair_limit", "Only one plan repair is allowed")
            if m["plan_status"] not in ("invalid", "failed"):
                raise AppError("invalid_state", "Repair allowed only for invalid/failed plans")
        else:
            if m["status"] not in ("draft", "failed", "blocked") or m["plan_status"] == "generating":
                raise AppError("invalid_state", "Plan not allowed in current state")

        planner_model = self.require_eligible(m["coordinator"])
        cap = int(get_setting(self.conn, "worker_cap", str(WORKER_CAP_DEFAULT)) or WORKER_CAP_DEFAULT)
        if self.count_active_dispatches() >= cap:
            raise AppError("worker_cap", f"Active app dispatches at cap ({cap})")
        handle = self.ensure_coordinator_terminal()
        # Create run if needed
        run_id = m["run_id"]
        if not run_id:
            created = self.adapter.run_create(objective=m["objective"], from_handle=handle)
            run_id = extract_id(created, "run_id", "id")
            if not run_id:
                raise AppError("orca", "run-create did not return run id")
            self.conn.execute(
                "UPDATE missions SET run_id = ?, updated_at = ? WHERE id = ?",
                (run_id, utc_now(), mission_id),
            )

        mission_dir = RUNS_DIR / mission_id
        mission_dir.mkdir(parents=True, exist_ok=True)
        specs_dir = mission_dir / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        spec_path = specs_dir / ("plan-repair.md" if repair else "plan.md")
        spec_path.write_text(
            (team_planner_spec(mission_id, dict(m), self.capabilities()["agents"]) if team
             else planner_spec_text(mission_id, dict(m), repair=repair)),
            encoding="utf-8",
        )

        task_payload = self.adapter.task_create(
            run_id=run_id,
            task_title=f"Plan {mission_id}",
            spec_file=spec_path,
            from_handle=handle,
        )
        native_task_id = extract_id(task_payload, "task_id", "id")
        if not native_task_id:
            raise AppError("orca", "task-create did not return task id")

        start = self.adapter.worker_start(
            run_id=run_id,
            task_id=native_task_id,
            agent=m["coordinator"],
            from_handle=handle,
            **({"model": planner_model} if m["coordinator"] == "opencode" else {}),
        )
        dispatch_id = extract_id(start, "dispatch_id", "dispatch", "id")
        if not dispatch_id:
            raise AppError("orca", "worker-start did not return dispatch id")

        dsp = new_id("dsp")
        self.conn.execute(
            """INSERT INTO dispatches(
                id, mission_id, task_id, native_dispatch_id, native_task_id, agent, model,
                state, liveness, terminal_state, last_seen_at, next_action, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                dsp,
                mission_id,
                None,
                dispatch_id,
                native_task_id,
                m["coordinator"],
                planner_model,
                "running",
                "unknown",
                None,
                utc_now(),
                "wait_planner",
                dumps({"result": "redacted"}),
            ),
        )
        repair_count = m["repair_count"] + (1 if repair else 0)
        self.conn.execute(
            """UPDATE missions SET status = 'planning', plan_status = 'generating',
               planner_dispatch_id = ?, repair_count = ?, updated_at = ? WHERE id = ?""",
            (dispatch_id, repair_count, utc_now(), mission_id),
        )
        add_event(self.conn, mission_id, "plan_started", "Planner worker dispatched", {"dispatch_id": dispatch_id})
        add_audit(self.conn, "plan_start", {"mission_id": mission_id, "dispatch_id": dispatch_id, "repair": repair})
        self.conn.commit()
        return self.get_mission(mission_id)

    def _ingest_plan(self, mission_id: str) -> None:
        plan_path = RUNS_DIR / mission_id / "plan.json"
        try:
            eligible = self.eligible_agent_ids()
            plan = load_and_validate_plan_file(plan_path, eligible_agents=eligible,
                                               mission_id=mission_id, objective=self.get_mission_row(mission_id)["objective"])
            self.conn.execute(
                """UPDATE missions SET plan_json = ?, plan_status = 'valid', plan_errors_json = ?,
                   status = 'awaiting_approval', updated_at = ? WHERE id = ?""",
                (dumps(plan), dumps([]), utc_now(), mission_id),
            )
            add_event(self.conn, mission_id, "plan_valid", "Plan validated")
        except PlanValidationError as exc:
            self.conn.execute(
                """UPDATE missions SET plan_status = 'invalid', plan_errors_json = ?,
                   status = 'blocked', updated_at = ? WHERE id = ?""",
                (dumps(exc.errors), utc_now(), mission_id),
            )
            add_event(self.conn, mission_id, "plan_invalid", "Plan validation failed", {"errors": exc.errors})

    # --- approve / start / refresh / cancel / retry ---

    def save_plan(self, mission_id: str, plan: dict[str, Any], plan_version: int) -> dict[str, Any]:
        """Save a user-authored plan, with no native mutations or inference."""
        with MissionStartLock():
            mission = self.get_mission(mission_id)
            if not mission["can"]["save_plan"]:
                raise AppError("invalid_state", "Plans can only be edited before execution with no active workers")
            if plan_version != mission["plan_version"]:
                raise AppError("version_conflict", "The saved plan changed. Reload it before applying edits")
            try:
                if "team_workflow" in plan:
                    from copy import deepcopy
                    plan = deepcopy(plan)
                    pin = get_setting(self.conn, "user_api_pin") or get_setting(self.conn, "free_model_pin")
                    source = plan["team_workflow"]
                    source_blocks = source.get("blocks", []) if isinstance(source, dict) else []
                    for block in source_blocks if isinstance(source_blocks, list) else []:
                        if not isinstance(block, dict): continue
                        staff = block.get("employees")
                        if not isinstance(staff, list): staff = []
                        advisors = block.get("advisors")
                        if not isinstance(advisors, list): advisors = []
                        for actor in [block.get("supervisor"), block.get("inspector"), *staff, *advisors]:
                            if isinstance(actor, dict) and actor.get("agent") == "opencode" and actor.get("model") in (None, "", "unknown"):
                                actor["model"] = pin or "unknown"
                    plan = compile_team_workflow(plan["team_workflow"], mission_id, mission["objective"], self.eligible_agent_ids())
                else:
                    validate_plan(plan, eligible_agents=self.eligible_agent_ids())
            except PlanValidationError as exc:
                raise AppError("invalid_plan", "The workflow has validation errors", {"errors": exc.errors}) from exc
            for task in plan["tasks"]:
                if not task.get("outputs") or not task.get("acceptance"):
                    raise AppError("invalid_plan", "Each step needs an output path and acceptance criteria")
                for command in task.get("validation", []):
                    try:
                        parse_validation_command(command)
                    except SecurityError as exc:
                        raise AppError(exc.code, exc.message, exc.details) from exc
            now = utc_now()
            changed = self.conn.execute(
                """UPDATE missions SET plan_json = ?, plan_version = plan_version + 1,
                   plan_status = 'valid', plan_errors_json = '[]', status = 'awaiting_approval',
                   approved_at = NULL, updated_at = ? WHERE id = ? AND plan_version = ?""",
                (dumps(plan), now, mission_id, plan_version),
            )
            if changed.rowcount != 1:
                self.conn.rollback()
                raise AppError("version_conflict", "The saved plan changed")
            add_event(self.conn, mission_id, "plan_edited", "Workflow saved; approval required",
                      {"plan_version": plan_version + 1})
            add_audit(self.conn, "plan_edit", {"mission_id": mission_id, "plan_version": plan_version + 1})
            self.conn.commit()
            return self.get_mission(mission_id)

    def approve(self, mission_id: str, plan_version: int) -> dict[str, Any]:
        with MissionStartLock():
            return self._approve_locked(mission_id, plan_version)

    def _approve_locked(self, mission_id: str, plan_version: int) -> dict[str, Any]:
        m = self.get_mission_row(mission_id)
        if m["plan_status"] != "valid":
            raise AppError("invalid_state", "Plan is not valid")
        if m["status"] != "awaiting_approval":
            raise AppError("invalid_state", "Mission not awaiting approval")
        if plan_version != m["plan_version"]:
            raise AppError("version_conflict", "The plan changed. Review the current version before approval")
        new_version = m["plan_version"] + 1
        now = utc_now()
        changed = self.conn.execute(
            """UPDATE missions SET status = 'approved', plan_version = ?, approved_at = ?, updated_at = ?
               WHERE id = ? AND status = 'awaiting_approval' AND plan_status = 'valid' AND plan_version = ?""",
            (new_version, now, now, mission_id, plan_version),
        )
        if changed.rowcount != 1:
            raise AppError("conflict", "Approve race lost")
        write_checkpoint(mission_id, "approved", {"plan_version": new_version})
        add_event(self.conn, mission_id, "approved", f"Plan version {new_version} approved")
        add_audit(self.conn, "approve", {"mission_id": mission_id, "plan_version": new_version})
        self.conn.commit()
        return self.get_mission(mission_id)

    def start(self, mission_id: str) -> dict[str, Any]:
        with MissionStartLock():
            return self._start_locked(mission_id)

    def _start_locked(self, mission_id: str) -> dict[str, Any]:
        m = self.get_mission_row(mission_id)
        if m["status"] == "unknown":
            raise AppError("unknown_state", "Mission is unknown; cannot start")
        if m["status"] != "approved" or m["plan_status"] != "valid":
            raise AppError("invalid_state", "Mission must be approved with valid plan")

        checked_routes: set[tuple[str, str | None]] = set()
        for task in loads(m["plan_json"], {}).get("tasks", []):
            route = (task["agent"], task.get("model"))
            if route not in checked_routes:
                self.require_eligible(*route)
                checked_routes.add(route)

        # Guarded DB transition to prevent double-dispatch
        now = utc_now()
        self.conn.execute(
            "UPDATE missions SET status = 'running', updated_at = ? WHERE id = ? AND status = 'approved'",
            (now, mission_id),
        )
        if self.conn.total_changes == 0:
            raise AppError("conflict", "Start rejected; mission already transitioning")

        cap = int(get_setting(self.conn, "worker_cap", str(WORKER_CAP_DEFAULT)) or WORKER_CAP_DEFAULT)
        active = self.count_active_dispatches()
        if active >= cap:
            # roll back status
            self.conn.execute(
                "UPDATE missions SET status = 'approved', updated_at = ? WHERE id = ?",
                (utc_now(), mission_id),
            )
            self.conn.commit()
            raise AppError("worker_cap", f"Active app dispatches at cap ({cap})")

        handle = self.ensure_coordinator_terminal()
        plan = loads(m["plan_json"], {})
        run_id = m["run_id"]
        if not run_id:
            created = self.adapter.run_create(objective=m["objective"], from_handle=handle)
            run_id = extract_id(created, "run_id", "id")
            self.conn.execute(
                "UPDATE missions SET run_id = ? WHERE id = ?",
                (run_id, mission_id),
            )
        else:
            try:
                self.adapter.run_use(run_id=run_id, from_handle=handle)
            except OrcaError as exc:
                self._mark_unknown(mission_id, f"run-use failed: {exc.message}")
                self.conn.commit()
                raise AppError("unknown_state", "Could not rebind run; mission marked unknown")

        # Materialize app tasks from plan
        existing = self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE mission_id = ?", (mission_id,)
        ).fetchone()["c"]
        if existing == 0:
            for pt in plan.get("tasks") or []:
                tid = new_id("tsk")
                self.conn.execute(
                    """INSERT INTO tasks(
                        id, mission_id, plan_task_id, title, role, agent, model, deps_json,
                        write_scope_json, outputs_json, acceptance_json, validation_json,
                        review_required, status, native_task_id, dispatch_id, attempt,
                        result_summary, evidence_json, created_at, updated_at, started_at, ended_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tid,
                        mission_id,
                        pt["id"],
                        pt["title"],
                        pt["role"],
                        pt["agent"],
                        pt.get("model") or "unknown",
                        dumps(pt.get("deps") or []),
                        dumps(pt.get("write_scope") or []),
                        dumps(pt.get("outputs") or []),
                        dumps(pt.get("acceptance") or []),
                        dumps(pt.get("validation") or []),
                        1 if pt.get("review_required") else 0,
                        "pending",
                        None,
                        None,
                        1,
                        None,
                        dumps([]),
                        now,
                        now,
                        None,
                        None,
                    ),
                )

        # Create native tasks in topological order
        order = topological_order(plan.get("tasks") or [])
        plan_by_id = {t["id"]: t for t in plan.get("tasks") or []}
        native_by_plan: dict[str, str] = {}
        specs_dir = RUNS_DIR / mission_id / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)

        for plan_tid in order:
            pt = plan_by_id[plan_tid]
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE mission_id = ? AND plan_task_id = ?",
                (mission_id, plan_tid),
            ).fetchone()
            if row and row["native_task_id"]:
                native_by_plan[plan_tid] = row["native_task_id"]
                continue
            spec_path = specs_dir / f"{plan_tid}.md"
            spec_path.write_text(self._task_spec(dict(m), pt), encoding="utf-8")
            dep_natives = [native_by_plan[d] for d in (pt.get("deps") or []) if d in native_by_plan]
            created = self.adapter.task_create(
                run_id=run_id,
                task_title=pt["title"],
                spec_file=spec_path,
                from_handle=handle,
                deps=dep_natives or None,
            )
            native_id = extract_id(created, "task_id", "id")
            if not native_id:
                raise AppError("orca", "task-create missing id")
            native_by_plan[plan_tid] = native_id
            self.conn.execute(
                "UPDATE tasks SET native_task_id = ?, updated_at = ? WHERE id = ?",
                (native_id, utc_now(), row["id"]),
            )

        add_event(self.conn, mission_id, "started", "Execution started")
        add_audit(self.conn, "start", {"mission_id": mission_id, "run_id": run_id})
        self.conn.commit()
        self._dispatch_ready(mission_id)
        return self.get_mission(mission_id)

    def _task_spec(self, mission: dict, pt: dict) -> str:
        return f"""# Task {pt['id']}: {pt['title']}

## Goal
{pt['title']}

Mission objective: {mission.get('objective')}

## Role
{pt.get('role')}

## Assignment
{pt.get('instruction') or pt['title']}

Specialty: {pt.get('specialty') or pt.get('department')}

## Inputs
{json.dumps(pt.get('inputs') or [], indent=2)}

## Write scope
You may write only these paths:
{json.dumps(pt.get('write_scope') or [], indent=2)}

## Forbidden actions
- Do not edit files outside write scope
- Do not start nested workers
- Do not invent metrics or fake completion
- Do not install packages
- Do not change the selected model, provider, account or billing route; no paid fallback

## Outputs
{json.dumps(pt.get('outputs') or [], indent=2)}

## Acceptance
{json.dumps(pt.get('acceptance') or [], indent=2)}

## Validation
{json.dumps(pt.get('validation') or [], indent=2)}

## Worker completion contract
{WORKER_COMPLETION_CONTRACT}
"""

    def _dispatch_ready(self, mission_id: str) -> None:
        m = self.get_mission_row(mission_id)
        if m["status"] not in ("running", "review"):
            return
        if m["cancel_requested"]:
            return
        cap = int(get_setting(self.conn, "worker_cap", str(WORKER_CAP_DEFAULT)) or WORKER_CAP_DEFAULT)
        handle = self.ensure_coordinator_terminal()
        run_id = m["run_id"]
        tasks = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM tasks WHERE mission_id = ?", (mission_id,)
            ).fetchall()
        ]
        # Chat councils and execution runs share a terminal; reclaim this Run before mutation.
        if run_id:
            self.adapter.run_use(run_id=run_id, from_handle=handle)
        status_by_plan = {t["plan_task_id"]: t["status"] for t in tasks}
        for t in tasks:
            if t["status"] != "pending" or t["dispatch_id"]:
                continue
            deps = loads(t["deps_json"], [])
            if not all(status_by_plan.get(d) == "succeeded" for d in deps):
                continue
            if self.count_active_dispatches() >= cap:
                break
            dispatch_model = self.require_eligible(t["agent"], t["model"])
            start = self.adapter.worker_start(
                run_id=run_id,
                task_id=t["native_task_id"],
                agent=t["agent"],
                from_handle=handle,
                model=dispatch_model,
            )
            dispatch_id = extract_id(start, "dispatch_id", "dispatch", "id")
            if not dispatch_id:
                continue
            if dispatch_model != t["model"]:
                self.conn.execute("UPDATE tasks SET model = ? WHERE id = ?", (dispatch_model, t["id"]))
                t["model"] = dispatch_model
            dsp = new_id("dsp")
            self.conn.execute(
                """INSERT INTO dispatches(
                    id, mission_id, task_id, native_dispatch_id, native_task_id, agent, model,
                    state, liveness, terminal_state, last_seen_at, next_action, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dsp,
                    mission_id,
                    t["id"],
                    dispatch_id,
                    t["native_task_id"],
                    t["agent"],
                    t["model"],
                    "running",
                    "unknown",
                    None,
                    utc_now(),
                    "wait",
                    dumps({"redacted": True}),
                ),
            )
            self.conn.execute(
                "UPDATE tasks SET status = 'running', dispatch_id = ?, started_at = ?, updated_at = ? WHERE id = ?",
                (dispatch_id, utc_now(), utc_now(), t["id"]),
            )
            add_event(
                self.conn,
                mission_id,
                "task_dispatched",
                f"Dispatched {t['plan_task_id']}",
                {"dispatch_id": dispatch_id},
            )
        self.conn.commit()

    def refresh(self, mission_id: str) -> dict[str, Any]:
        with MissionStartLock():
            return self._refresh_locked(mission_id)

    def _refresh_locked(self, mission_id: str) -> dict[str, Any]:
        m = self.get_mission_row(mission_id)
        if m["status"] == "cancelled":
            for row in self.conn.execute("SELECT native_dispatch_id, task_id FROM dispatches WHERE mission_id = ? AND terminal_state IS NULL", (mission_id,)).fetchall():
                self._reconcile_dispatch(mission_id, row["native_dispatch_id"], planner=False, task_id=row["task_id"])
            # Observation only: cancellation must never dispatch another worker.
            return self.get_mission(mission_id)
        # Reconcile planner
        if m["status"] == "planning" and m["planner_dispatch_id"]:
            self._reconcile_dispatch(mission_id, m["planner_dispatch_id"], planner=True)
        if m["status"] in ("running", "review"):
            rows = self.conn.execute(
                "SELECT * FROM dispatches WHERE mission_id = ? AND terminal_state IS NULL",
                (mission_id,),
            ).fetchall()
            for row in rows:
                self._reconcile_dispatch(mission_id, row["native_dispatch_id"], planner=False, task_id=row["task_id"])
            self._dispatch_ready(mission_id)
            self._evaluate_mission_completion(mission_id)
        return self.get_mission(mission_id)

    def _reconcile_dispatch(
        self,
        mission_id: str,
        dispatch_id: str,
        *,
        planner: bool,
        task_id: str | None = None,
    ) -> None:
        try:
            shown = self.adapter.worker_show(dispatch_id=dispatch_id)
        except OrcaError:
            self._mark_unknown(mission_id, f"worker-show failed for {dispatch_id}")
            self.conn.commit()
            return

        state = (
            shown.get("state")
            or (shown.get("result") or {}).get("state")
            or shown.get("status")
            or "unknown"
        )
        liveness = shown.get("liveness") or (shown.get("result") or {}).get("liveness")
        terminal = None
        if state in ("done", "failed", "stopped", "cancelled", "released"):
            terminal = state
        self.conn.execute(
            """UPDATE dispatches SET state = ?, liveness = ?, terminal_state = ?, last_seen_at = ?
               WHERE native_dispatch_id = ?""",
            (str(state), str(liveness) if liveness else None, terminal, utc_now(), dispatch_id),
        )

        if terminal is None:
            self.conn.commit()
            return

        if planner:
            if state == "done":
                self._ingest_plan(mission_id)
            else:
                self.conn.execute(
                    """UPDATE missions SET status = 'failed', plan_status = 'failed', updated_at = ?
                       WHERE id = ?""",
                    (utc_now(), mission_id),
                )
                add_event(self.conn, mission_id, "plan_failed", f"Planner ended with {state}")
            self.conn.commit()
            return

        if task_id:
            self._settle_task(mission_id, task_id, dispatch_id, state)
        self.conn.commit()

    def _settle_task(self, mission_id: str, task_id: str, dispatch_id: str, state: str) -> None:
        t = dict(
            self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
        evidence: list[dict[str, Any]] = []
        try:
            preview = self.adapter.worker_read(dispatch_id=dispatch_id, limit=WORKER_READ_LIMIT)
            messages = preview.get("messages") or (preview.get("result") or {}).get("messages") or []
            text_bits = []
            if isinstance(messages, list):
                for msg in messages[:WORKER_READ_LIMIT]:
                    if isinstance(msg, str):
                        text_bits.append(msg)
                    elif isinstance(msg, dict):
                        for k in ("text", "body", "content", "message"):
                            if isinstance(msg.get(k), str):
                                text_bits.append(msg[k])
                                break
            joined, _ = cap_bytes("\n".join(text_bits), 64 * 1024)
            evidence.append({"type": "worker_preview", "text": redact(joined), "ts": utc_now()})
        except OrcaError as exc:
            evidence.append({"type": "worker_preview_error", "text": exc.message, "ts": utc_now()})

        outputs = loads(t["outputs_json"], [])
        all_ok = state == "done"
        for rel in outputs:
            try:
                path = resolve_under_workspace(rel, must_exist=True, workspace=WORKSPACE)
            except Exception as exc:
                all_ok = False
                evidence.append({"type": "missing_output", "path": rel, "error": str(exc), "ts": utc_now()})
                continue
            digest = sha256_file(path)
            art_id = new_id("art")
            self.conn.execute(
                """INSERT INTO artifacts(id, mission_id, task_id, rel_path, size, sha256, kind, verified, validation_output, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    art_id,
                    mission_id,
                    task_id,
                    rel.replace("\\", "/"),
                    path.stat().st_size,
                    digest,
                    path.suffix.lstrip(".") or "file",
                    1,
                    None,
                    utc_now(),
                ),
            )
            evidence.append({"type": "artifact", "path": rel, "sha256": digest, "ts": utc_now()})

        for cmd in loads(t["validation_json"], []):
            result = run_validation(cmd, workspace=WORKSPACE)
            evidence.append({"type": "validation", **result})
            if result.get("status") != "ok":
                all_ok = False

        # Independent inspection cannot accept producer files that changed after their recorded handoff.
        if t["role"] == "team_inspection":
            plan = loads(self.get_mission_row(mission_id)["plan_json"], {})
            contract = next((p for p in plan.get("tasks", []) if p["id"] == t["plan_task_id"]), {})
            for rel in contract.get("inputs", []):
                recorded = self.conn.execute("SELECT sha256 FROM artifacts WHERE mission_id = ? AND rel_path = ? AND verified = 1 ORDER BY rowid DESC LIMIT 1", (mission_id, rel)).fetchone()
                if not recorded:
                    continue
                try:
                    unchanged = sha256_file(resolve_under_workspace(rel, workspace=WORKSPACE, must_exist=True)) == recorded["sha256"]
                except (SecurityError, OSError):
                    unchanged = False
                if not unchanged:
                    all_ok = False
                    evidence.append({"type": "inspection_input_changed", "path": rel, "ts": utc_now()})

        # Team gates require a real scoped verdict; native done alone never opens the next block.
        if t["role"] in ("team_check", "team_inspection"):
            try:
                if len(outputs) != 1: raise ValueError("One declared gate file is required")
                target = resolve_under_workspace(outputs[0], must_exist=True, workspace=WORKSPACE)
                if target.stat().st_size > 256 * 1024: raise ValueError("Gate file exceeds size limit")
                gate = json.loads(target.read_text(encoding="utf-8"))
                passed = (isinstance(gate, dict) and gate.get("verdict") == "PASS"
                          and gate.get("dispatch_id") == dispatch_id
                          and isinstance(gate.get("checks"), list) and bool(gate["checks"])
                          and all(isinstance(c, dict) and c.get("passed") is True
                                  and isinstance(c.get("criterion"), str) and c["criterion"].strip()
                                  and isinstance(c.get("evidence"), str) and c["evidence"].strip() for c in gate["checks"])
                          and isinstance(gate.get("findings"), list) and not gate["findings"])
                evidence.append({"type": "team_gate", "verdict": "PASS" if passed else "FAIL", "ts": utc_now()})
                if not passed: all_ok = False
            except Exception:
                all_ok = False
                evidence.append({"type": "team_gate", "verdict": "INVALID", "ts": utc_now()})

        # Review verdict
        if t["role"] == "review":
            review_path = WORKSPACE / "review.json"
            # Prefer mission-scoped review.json if present
            scoped = RUNS_DIR / mission_id / "review.json"
            target = scoped if scoped.is_file() else (WORKSPACE / "review.json" if review_path.is_file() else None)
            # Also accept declared outputs named review.json
            for rel in outputs:
                if rel.replace("\\", "/").endswith("review.json"):
                    try:
                        target = resolve_under_workspace(rel, must_exist=True, workspace=WORKSPACE)
                    except Exception:
                        target = None
            if target and target.is_file():
                try:
                    review = json.loads(target.read_text(encoding="utf-8"))
                    verdict = str(review.get("verdict") or review.get("status") or "").upper()
                    evidence.append({"type": "review", "verdict": verdict, "ts": utc_now()})
                    if verdict != "PASS":
                        all_ok = False
                except Exception as exc:
                    all_ok = False
                    evidence.append({"type": "review_error", "error": str(exc), "ts": utc_now()})
            else:
                all_ok = False
                evidence.append({"type": "review_missing", "ts": utc_now()})

        if state != "done":
            all_ok = False

        new_status = "succeeded" if all_ok else "failed"
        self.conn.execute(
            """UPDATE tasks SET status = ?, result_summary = ?, evidence_json = ?, ended_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                new_status,
                "ok" if all_ok else f"settled with state={state}",
                dumps(evidence),
                utc_now(),
                utc_now(),
                task_id,
            ),
        )
        write_checkpoint(mission_id, f"task-{t['plan_task_id']}", {"status": new_status})
        add_event(self.conn, mission_id, "task_settled", f"{t['plan_task_id']} -> {new_status}")

    def _evaluate_mission_completion(self, mission_id: str) -> None:
        m = self.get_mission_row(mission_id)
        if m["status"] not in ("running", "review"):
            return
        tasks = [
            dict(r)
            for r in self.conn.execute("SELECT * FROM tasks WHERE mission_id = ?", (mission_id,)).fetchall()
        ]
        if not tasks:
            return
        if any(t["status"] in ("pending", "running") for t in tasks):
            if any(t["role"] == "review" and t["status"] in ("pending", "running") for t in tasks):
                if m["status"] != "review" and all(
                    t["status"] == "succeeded" for t in tasks if t["role"] != "review"
                ):
                    self.conn.execute(
                        "UPDATE missions SET status = 'review', updated_at = ? WHERE id = ?",
                        (utc_now(), mission_id),
                    )
            return
        if all(t["status"] == "succeeded" for t in tasks):
            # Final gate: review.json PASS if review task exists
            has_review = any(t["role"] == "review" for t in tasks)
            ok = True
            if has_review:
                ok = any(
                    any(
                        e.get("type") == "review" and e.get("verdict") == "PASS"
                        for e in loads(t.get("evidence_json"), [])
                    )
                    for t in tasks
                    if t["role"] == "review"
                )
            self.conn.execute(
                "UPDATE missions SET status = ?, updated_at = ? WHERE id = ?",
                ("succeeded" if ok else "blocked", utc_now(), mission_id),
            )
            add_event(
                self.conn,
                mission_id,
                "mission_complete",
                "succeeded" if ok else "blocked: review/evidence gate",
            )
        elif any(t["status"] == "failed" for t in tasks):
            self.conn.execute(
                "UPDATE missions SET status = 'failed', updated_at = ? WHERE id = ?",
                (utc_now(), mission_id),
            )
            add_event(self.conn, mission_id, "mission_failed", "One or more tasks failed")
        self.conn.commit()

    def cancel(self, mission_id: str, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise AppError("validation", "confirm must be true")
        m = self.get_mission_row(mission_id)
        if m["status"] in TERMINAL_MISSION_STATUSES:
            raise AppError("invalid_state", "Mission already terminal")
        # A mission stop includes only its own explicitly requested council consultations.
        for child in self.conn.execute("SELECT id, child_mission_id FROM manager_turns WHERE parent_mission_id = ?", (mission_id,)).fetchall():
            state = self.get_mission(child["child_mission_id"])
            if state["can"]["cancel"]:
                self.cancel(child["child_mission_id"], True)
            self.conn.execute("UPDATE manager_turns SET status = CASE WHEN status = 'ready' THEN status ELSE 'failed' END, error = CASE WHEN status = 'ready' THEN error ELSE 'Stopped with parent mission' END, updated_at = ? WHERE id = ?", (utc_now(), child["id"]))
        write_checkpoint(mission_id, "cancel", {"status": m["status"]})
        self.conn.execute(
            "UPDATE missions SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (utc_now(), mission_id),
        )
        rows = self.conn.execute(
            "SELECT native_dispatch_id, task_id FROM dispatches WHERE mission_id = ? AND terminal_state IS NULL",
            (mission_id,),
        ).fetchall()
        for row in rows:
            try:
                self.adapter.worker_stop(dispatch_id=row["native_dispatch_id"])
            except OrcaError:
                pass
            # A stop request does not free capacity until native state confirms settlement.
            self._reconcile_dispatch(mission_id, row["native_dispatch_id"], planner=False, task_id=row["task_id"])
        self.conn.execute(
            "UPDATE missions SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (utc_now(), mission_id),
        )
        add_event(self.conn, mission_id, "cancelled", "Mission cancelled")
        add_audit(self.conn, "cancel", {"mission_id": mission_id})
        self.conn.commit()
        return self.get_mission(mission_id)

    def retry_task(self, mission_id: str, task_id: str) -> dict[str, Any]:
        with MissionStartLock():
            return self._retry_task_locked(mission_id, task_id)

    def _retry_task_locked(self, mission_id: str, task_id: str) -> dict[str, Any]:
        m = self.get_mission_row(mission_id)
        if m["status"] in ("unknown", "cancelled", "succeeded") or m["cancel_requested"]:
            raise AppError("invalid_state", "Cannot retry in unknown or closed state")
        t = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND mission_id = ?", (task_id, mission_id)
        ).fetchone()
        if t is None:
            raise AppError("not_found", "Task not found")
        if t["status"] not in ("failed", "blocked"):
            raise AppError("invalid_state", "Only failed/blocked tasks can be retried")
        if t["attempt"] >= MAX_TASK_RETRIES:
            raise AppError("retry_limit", "Maximum two retries reached")
        self.require_eligible(t["agent"], t["model"])
        write_checkpoint(mission_id, f"retry-{t['plan_task_id']}", {"attempt": t["attempt"]})
        handle = self.ensure_coordinator_terminal()
        plan = loads(m["plan_json"], {})
        pt = next(p for p in plan["tasks"] if p["id"] == t["plan_task_id"])
        specs_dir = RUNS_DIR / mission_id / "specs"
        spec_path = specs_dir / f"{t['plan_task_id']}-r{t['attempt'] + 1}.md"
        spec_path.write_text(self._task_spec(dict(m), pt), encoding="utf-8")
        created = self.adapter.task_create(
            run_id=m["run_id"],
            task_title=f"{pt['title']} (retry)",
            spec_file=spec_path,
            from_handle=handle,
        )
        native_id = extract_id(created, "task_id", "id")
        self.conn.execute(
            """UPDATE tasks SET status = 'pending', native_task_id = ?, dispatch_id = NULL,
               attempt = attempt + 1, updated_at = ?, ended_at = NULL WHERE id = ?""",
            (native_id, utc_now(), task_id),
        )
        if m["status"] in ("failed", "blocked"):
            self.conn.execute(
                "UPDATE missions SET status = 'running', updated_at = ? WHERE id = ?",
                (utc_now(), mission_id),
            )
        add_event(self.conn, mission_id, "retry", f"Retry {t['plan_task_id']}")
        add_audit(self.conn, "retry", {"mission_id": mission_id, "task_id": task_id})
        self.conn.commit()
        self._dispatch_ready(mission_id)
        return self.get_mission(mission_id)

    def reconcile_all(self) -> None:
        rows = self.conn.execute(
            "SELECT id FROM missions WHERE status NOT IN ('succeeded','failed','cancelled','blocked','draft')"
        ).fetchall()
        for row in rows:
            try:
                self.refresh(row["id"])
            except Exception:
                self._mark_unknown(row["id"], "reconcile failed")
                self.conn.commit()

    def escalation_markdown(self, mission_id: str) -> str:
        m = self.get_mission(mission_id)
        lines = [
            f"# Escalation — {m['title']}",
            "",
            f"- Mission: `{m['id']}`",
            f"- Status: **{m['status']}**",
            f"- Plan status: {m['plan_status']}",
            f"- Run: `{m['run_id']}`",
            "",
            "## Objective",
            m["objective"],
            "",
            "## Plan errors",
        ]
        for err in m.get("plan_errors") or []:
            lines.append(f"- {err}")
        lines.append("")
        lines.append("## Tasks")
        for t in m.get("tasks") or []:
            lines.append(
                f"- {t['plan_task_id']} `{t['status']}` dispatch={t.get('dispatch_id')} native={t.get('native_task_id')}"
            )
        lines.append("")
        lines.append("## Recent events")
        for e in (m.get("events") or [])[:20]:
            lines.append(f"- {e['ts']} {e['type']}: {e['message']}")
        return "\n".join(lines) + "\n"
