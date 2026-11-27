"""Plan schema validation for MissionDeck."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from app.config import MAX_PLAN_TASKS, PLAN_SIZE_LIMIT, WORKSPACE
from app.security import SecurityError, resolve_under_workspace

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
REQUIRED_TOP = (
    "schema_version",
    "mission_summary",
    "architecture",
    "selection_rationale",
    "departments",
    "tasks",
    "quality_gates",
    "risks",
)
REQUIRED_TASK = (
    "id",
    "title",
    "department",
    "role",
    "agent",
    "model",
    "deps",
    "write_scope",
    "inputs",
    "outputs",
    "acceptance",
    "validation",
    "review_required",
)


class PlanValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _require_str(obj: dict, key: str, errors: list[str], *, prefix: str = "") -> None:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        errors.append(f"{prefix}{key} must be a non-empty string")


def _topo_has_cycle(tasks: dict[str, list[str]]) -> bool:
    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for dep in tasks.get(node, []):
            if dep in tasks and dfs(dep):
                return True
        stack.remove(node)
        return False

    return any(dfs(n) for n in tasks)


def topological_order(tasks: list[dict[str, Any]]) -> list[str]:
    ids = [t["id"] for t in tasks]
    deps_map = {t["id"]: list(t.get("deps") or []) for t in tasks}
    remaining = set(ids)
    ordered: list[str] = []
    while remaining:
        ready = [tid for tid in remaining if all(d not in remaining for d in deps_map.get(tid, []))]
        if not ready:
            raise PlanValidationError(["dependency cycle detected during topological sort"])
        ready.sort()
        for tid in ready:
            ordered.append(tid)
            remaining.remove(tid)
    return ordered


def validate_rel_path(path: str, errors: list[str], *, field: str) -> None:
    try:
        resolve_under_workspace(path, workspace=WORKSPACE)
    except SecurityError as exc:
        errors.append(f"{field}: {exc.message} ({path})")


def validate_plan(
    plan: dict[str, Any] | Any,
    *,
    eligible_agents: Iterable[str] | None = None,
    require_independent_review: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        raise PlanValidationError(["plan must be a JSON object"])

    for key in REQUIRED_TOP:
        if key not in plan:
            errors.append(f"missing required field: {key}")

    if plan.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    for key in ("mission_summary", "architecture", "selection_rationale"):
        if key in plan:
            _require_str(plan, key, errors)

    departments = plan.get("departments")
    if not isinstance(departments, list) or not departments:
        errors.append("departments must be a non-empty list")
    else:
        dept_ids: set[str] = set()
        for i, d in enumerate(departments):
            if not isinstance(d, dict):
                errors.append(f"departments[{i}] must be an object")
                continue
            _require_str(d, "id", errors, prefix=f"departments[{i}].")
            _require_str(d, "name", errors, prefix=f"departments[{i}].")
            if isinstance(d.get("id"), str):
                dept_ids.add(d["id"])

    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        errors.append("tasks must be a list")
        tasks = []
    if len(tasks) == 0:
        errors.append("plan must contain at least one task")
    if len(tasks) > MAX_PLAN_TASKS:
        errors.append(f"plan may not contain more than {MAX_PLAN_TASKS} tasks")

    eligible = set(eligible_agents) if eligible_agents is not None else None
    task_ids: list[str] = []
    seen: set[str] = set()
    write_owners: dict[str, str] = {}
    review_tasks: list[dict[str, Any]] = []
    impl_agents: set[str] = set()
    deps_map: dict[str, list[str]] = {}

    for i, task in enumerate(tasks):
        prefix = f"tasks[{i}]."
        if not isinstance(task, dict):
            errors.append(f"tasks[{i}] must be an object")
            continue
        for key in REQUIRED_TASK:
            if key not in task:
                errors.append(f"{prefix}missing {key}")
        tid = task.get("id")
        if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
            errors.append(f"{prefix}id must match ^[A-Za-z0-9_-]{{1,32}}$")
        else:
            if tid in seen:
                errors.append(f"duplicate task id: {tid}")
            seen.add(tid)
            task_ids.append(tid)

        for key in ("title", "department", "role", "agent", "model"):
            if key in task:
                _require_str(task, key, errors, prefix=prefix)

        if isinstance(task.get("department"), str) and departments and isinstance(departments, list):
            if task["department"] not in dept_ids:
                errors.append(f"{prefix}department unknown: {task['department']}")

        agent = task.get("agent")
        if eligible is not None and isinstance(agent, str) and agent not in eligible:
            errors.append(f"{prefix}agent '{agent}' is not currently eligible")

        deps = task.get("deps")
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            errors.append(f"{prefix}deps must be a list of strings")
            deps = []
        if isinstance(tid, str):
            deps_map[tid] = deps

        for field_name in ("write_scope", "inputs", "outputs", "acceptance", "validation"):
            val = task.get(field_name)
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                errors.append(f"{prefix}{field_name} must be a list of strings")

        for field_name in ("write_scope", "inputs", "outputs"):
            for p in task.get(field_name) or []:
                if isinstance(p, str):
                    validate_rel_path(p, errors, field=f"{prefix}{field_name}")

        for p in task.get("write_scope") or []:
            if isinstance(p, str) and isinstance(tid, str):
                norm = p.replace("\\", "/")
                if norm in write_owners and write_owners[norm] != tid:
                    # Conflict only if neither depends on the other (checked after ids known)
                    write_owners.setdefault(norm, tid)
                    write_owners[f"__conflict__{norm}"] = f"{write_owners[norm]}|{tid}"
                else:
                    write_owners[norm] = tid

        if task.get("role") == "review":
            review_tasks.append(task)
        if task.get("role") == "implementation" and isinstance(agent, str):
            impl_agents.add(agent)

        if "review_required" in task and not isinstance(task.get("review_required"), bool):
            errors.append(f"{prefix}review_required must be a boolean")

    # Unknown deps and cycles
    for tid, deps in deps_map.items():
        for d in deps:
            if d not in seen:
                errors.append(f"task {tid} depends on unknown id {d}")
    if deps_map and _topo_has_cycle(deps_map):
        errors.append("dependency cycle detected")

    # Concurrent write conflicts: shared write_scope without ordering
    path_to_tasks: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        tid = task.get("id")
        if not isinstance(tid, str):
            continue
        for p in task.get("write_scope") or []:
            if isinstance(p, str):
                path_to_tasks.setdefault(p.replace("\\", "/"), []).append(tid)

    def depends_on(a: str, b: str) -> bool:
        """True if a depends (transitively) on b."""
        stack = list(deps_map.get(a, []))
        seen_d: set[str] = set()
        while stack:
            n = stack.pop()
            if n == b:
                return True
            if n in seen_d:
                continue
            seen_d.add(n)
            stack.extend(deps_map.get(n, []))
        return False

    for path, owners in path_to_tasks.items():
        if len(owners) < 2:
            continue
        for i, a in enumerate(owners):
            for b in owners[i + 1 :]:
                if not depends_on(a, b) and not depends_on(b, a):
                    errors.append(
                        f"concurrent write conflict on '{path}' between {a} and {b}"
                    )

    # Review independence
    for rt in review_tasks:
        r_agent = rt.get("agent")
        r_deps = set(rt.get("deps") or [])
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if task.get("role") != "implementation":
                continue
            if task.get("id") in r_deps and task.get("agent") == r_agent:
                errors.append(
                    f"review task {rt.get('id')} agent must differ from implementation task {task.get('id')}"
                )

    has_review = any(
        isinstance(t, dict) and (t.get("role") == "review" or t.get("review_required") is True)
        for t in tasks
    )
    # review_required false when plan has a review task
    if any(isinstance(t, dict) and t.get("role") == "review" for t in tasks):
        for task in tasks:
            if isinstance(task, dict) and task.get("role") == "implementation":
                # If a review task deps on this impl, review_required should be true
                for rt in review_tasks:
                    if task.get("id") in (rt.get("deps") or []):
                        if task.get("review_required") is False:
                            errors.append(
                                f"task {task.get('id')} must set review_required true when a review task exists"
                            )

    if require_independent_review and not any(
        isinstance(t, dict) and t.get("role") == "review" for t in tasks
    ):
        errors.append("mission requires independent review but plan has no review task")

    for key in ("quality_gates", "risks"):
        if key in plan and not isinstance(plan.get(key), list):
            errors.append(f"{key} must be a list")

    if errors:
        raise PlanValidationError(errors)
    return plan


def load_and_validate_plan_file(
    path: Path,
    *,
    eligible_agents: Iterable[str] | None = None,
    require_independent_review: bool = False,
) -> dict[str, Any]:
    if not path.is_file():
        raise PlanValidationError([f"plan file missing: {path}"])
    size = path.stat().st_size
    if size > PLAN_SIZE_LIMIT:
        raise PlanValidationError([f"plan file exceeds {PLAN_SIZE_LIMIT} bytes"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanValidationError([f"plan JSON invalid: {exc}"]) from exc
    return validate_plan(
        data,
        eligible_agents=eligible_agents,
        require_independent_review=require_independent_review,
    )


def planner_spec_text(mission_id: str, mission: dict[str, Any], *, repair: bool = False) -> str:
    plan_rel = f".orca-runs/{mission_id}/plan.json"
    repair_note = (
        "This is a repair attempt. Fix the previous plan errors and rewrite the file.\n"
        if repair
        else ""
    )
    return f"""# Planner task

Write exactly one file: `{plan_rel}`

{repair_note}Mission title: {mission.get('title')}
Objective: {mission.get('objective')}
Quality requirements: {mission.get('quality_requirements')}
Scope: {mission.get('scope')}
Constraints: {mission.get('constraints')}
Coordinator agent: {mission.get('coordinator')}

The file must be JSON matching schema_version 1 with departments, tasks (1..8),
quality_gates, and risks. Use only relative workspace paths. Do not invent
completion of other work. Do not start other workers.

## Worker completion contract

You may and must use native Orca communication tools for completion, required heartbeat, and blocking questions, even in a read-only/no-tools task. No nested workers; no unrelated tools. Follow injected native preamble and use your own active task ID and dispatch ID. Execute `worker_done` through your agent's shell/terminal tool (or the native Orca completion tool exposed in your preamble) as an actual command with outcome `succeeded` or `failed`, a concise summary, evidence and modified paths (if any). Printing, quoting, or describing the command in your reply is not a report and does not settle the task. Send it exactly once successfully; a failed transmission is not a receipt: inspect and retry only if unsettled. Do not end merely with conversational text. If blocked, ask through native Orca or report failed with partial results. Never file completion on another worker's behalf. Do not disclose secrets.
"""
