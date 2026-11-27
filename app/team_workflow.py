"""Compile a specialist team canvas into bounded native Orca task dispatches."""
from __future__ import annotations

import copy
import re
from typing import Any

from app.plan import PlanValidationError, validate_plan
from app.config import WORKER_COMPLETION_CONTRACT

MAX_BLOCKS = 8
MAX_EMPLOYEES = 8
MAX_TEAM_TASKS = MAX_BLOCKS * (MAX_EMPLOYEES + 2)
ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
KINDS = {"management", "specialist", "inspection"}


def _compile_v1(workflow: Any, mission_id: str, objective: str,
                          eligible_agents: set[str] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(workflow, dict) or workflow.get("version") != 1:
        raise PlanValidationError(["team workflow version must be 1"])
    blocks = workflow.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
        raise PlanValidationError([f"Choose 1..{MAX_BLOCKS} team blocks"])
    blocks = copy.deepcopy(blocks)
    ids = set()
    for b in blocks:
        if not isinstance(b, dict):
            errors.append("Every block must be an object"); continue
        bid = b.get("id")
        if not isinstance(bid, str) or not ID.fullmatch(bid) or bid in ids:
            errors.append("Block IDs must be unique short identifiers beginning with a letter")
        else: ids.add(bid)
        for key in ("name", "specialty", "goal"):
            if not isinstance(b.get(key), str) or not b[key].strip(): errors.append(f"{bid}: {key} is required")
        if b.get("kind") not in KINDS: errors.append(f"{bid}: unknown block type")
        for key in ("supervisor",):
            actor = b.get(key)
            if not isinstance(actor, dict) or not isinstance(actor.get("agent"), str) or not actor["agent"]:
                errors.append(f"{bid}: supervisor agent is required")
            elif eligible_agents is not None and actor["agent"] not in eligible_agents:
                errors.append(f"{bid}: supervisor is not currently eligible")
        staff = b.get("employees")
        if not isinstance(staff, list) or not 1 <= len(staff) <= MAX_EMPLOYEES:
            errors.append(f"{bid}: choose 1..{MAX_EMPLOYEES} employees"); continue
        for i, actor in enumerate(staff):
            if not isinstance(actor, dict): errors.append(f"{bid}: employee {i+1} must be an object"); continue
            for key in ("agent", "instruction"):
                if not isinstance(actor.get(key), str) or not actor[key].strip(): errors.append(f"{bid}: employee {i+1} {key} is required")
            if eligible_agents is not None and actor.get("agent") not in eligible_agents:
                errors.append(f"{bid}: employee {i+1} is not currently eligible")
            for key in ("inputs", "write_scope", "validation"):
                if key in actor and (not isinstance(actor[key], list) or not all(isinstance(x, str) and x.strip() for x in actor[key])):
                    errors.append(f"{bid}: employee {i+1} {key} must be a list of nonempty strings")
            for key in ("outputs", "acceptance"):
                if not isinstance(actor.get(key), list) or not actor[key] or not all(isinstance(x, str) and x.strip() for x in actor[key]):
                    errors.append(f"{bid}: employee {i+1} needs {key}")
        deps = b.get("deps")
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps): errors.append(f"{bid}: dependencies must be a list")
        elif len(set(deps)) != len(deps) or bid in deps: errors.append(f"{bid}: duplicate or self connection")
        pos = b.get("position")
        if pos is not None and (not isinstance(pos, dict) or any(type(pos.get(k)) not in (int, float) or not -5000 <= pos[k] <= 10000 for k in ("x", "y"))):
            errors.append(f"{bid}: invalid canvas position")
    if errors: raise PlanValidationError(errors)
    for b in blocks:
        if any(dep not in ids for dep in b["deps"]): errors.append(f"{b['id']}: connection points to an unknown block")
    if errors: raise PlanValidationError(errors)
    tasks = []
    all_outputs = {b["id"]: [p for a in b["employees"] for p in a["outputs"]] for b in blocks}
    for b in blocks:
        bid = b["id"]; root = f".orca-runs/{mission_id}/teams/{bid}"
        brief = f"{root}/brief.md"; verdict = f"{root}/review.json"
        upstream = [p for dep in b["deps"] for p in [*all_outputs[dep], f".orca-runs/{mission_id}/teams/{dep}/review.json"]]
        def task(tid, title, actor, role, deps, inputs, outputs, instruction, acceptance, validation=None):
            return {"id": tid, "title": title, "department": bid, "role": role,
                    "agent": actor["agent"], "model": actor.get("model") or "unknown", "deps": deps,
                    "inputs": inputs, "write_scope": (actor.get("write_scope") or outputs) if role == "team_employee" else outputs,
                    "outputs": outputs, "acceptance": acceptance, "validation": validation or [],
                    "review_required": False, "instruction": instruction, "team_block": bid,
                    "specialty": b["specialty"]}
        tasks.append(task(f"{bid}_brief", f"{b['name']} · Supervisor brief", b["supervisor"], "team_supervisor",
                          [f"{d}_check" for d in b["deps"]], upstream, [brief],
                          f"Specialty: {b['specialty']}. Block goal: {b['goal']}. Prepare a precise assignment for each employee. "
                          f"Employee contracts: {b['employees']}. Read upstream outputs. Write only the brief, not employee deliverables. "
                          "Do not start any workers: MissionDeck dispatches the declared employees.", ["The brief assigns every employee a distinct bounded job and its declared outputs."]))
        workers = []
        for i, a in enumerate(b["employees"], 1):
            tid = f"{bid}_w{i}"; workers.append(tid)
            tasks.append(task(tid, f"{b['name']} · Employee {i}", a, "team_employee", [f"{bid}_brief"],
                              [brief, *upstream, *(a.get("inputs") or [])], a["outputs"],
                              f"Specialty: {b['specialty']}. Block goal: {b['goal']}. You are employee {i} of {len(b['employees'])}. "
                              f"Read the supervisor brief and do your assigned part. {a['instruction']}", a["acceptance"], a.get("validation")))
        tasks.append(task(f"{bid}_check", f"{b['name']} · Supervisor check", b["supervisor"],
                          "team_inspection" if b["kind"] == "inspection" else "team_check", workers,
                          [brief, *upstream, *all_outputs[bid]], [verdict],
                          f"Specialty: {b['specialty']}. Inspect every employee deliverable against the goal: {b['goal']}. "
                          "Do not edit employee outputs. Write a JSON object with verdict PASS or FAIL, findings (array), "
                          "dispatch_id (your own active native dispatch ID from the injected preamble), and checks "
                          "(nonempty array of objects with criterion: string, evidence: string and passed: boolean). "
                          "PASS requires every declared acceptance criterion, all checks passed:true, and findings:[] . "
                          "A missing, unverifiable or incorrect deliverable requires FAIL. This is a separate supervisor session, "
                          "not a claim of independent model/provider review.", ["verdict is PASS with concrete checks and findings recorded"]))
    plan = {"schema_version": 1, "mission_summary": objective,
            "architecture": "Each team: supervisor brief → selected employees → supervisor check. Downstream teams wait for the check.",
            "selection_rationale": "User-selected specialist teams; session capacity is separate from employee count.",
            "departments": [{"id": b["id"], "name": b["name"]} for b in blocks], "tasks": tasks,
            "quality_gates": [{"id": f"{b['id']}_gate", "description": f"{b['name']} supervisor PASS", "evidence": f".orca-runs/{mission_id}/teams/{b['id']}/review.json"} for b in blocks],
            "risks": ["Separate inspection sessions may use the same provider/model; this is not provider-independent review."],
            "team_workflow": {"version": 1, "blocks": blocks}}
    return validate_plan(plan, eligible_agents=eligible_agents, max_tasks=MAX_TEAM_TASKS)


def team_planner_spec(mission_id: str, mission: dict, agents: list[dict]) -> str:
    import json
    allowed = [{"agent": a["id"], "model": a["model"]} for a in agents if a.get("eligible")]
    return f'''# Design a specialist team workflow
Write exactly `.orca-runs/{mission_id}/plan.json` as JSON with one key `team_workflow`.
Mission objective: {mission['objective']}
Quality: {mission['quality_requirements']}
Files: {mission['scope']}
Constraints: {mission['constraints']}
Available routes (use ONLY these exact agent/model pairs): {json.dumps(allowed)}

team_workflow has version 2 and blocks (1..8). Include management, the useful specialties,
and a final inspection block. Each block has:
- id: unique short letter-led ID
- name, specialty, goal: clear nonempty strings
- kind: management, specialist, or inspection
- deps: IDs of prerequisite blocks, no cycles; inspection waits for all implementation blocks
- For specialist blocks only: supervisor: {{"agent": "eligible agent ID", "model": "verified model ID or unknown"}}
- For management: advisors: 1..4 objects with agent and model. These are consulting peers,
  with no supervisor or employees. They produce advice, then a synthesis session publishes the manager brief.
- For inspection: inspector: {{"agent": "eligible agent ID", "model": "verified model ID or unknown"}}.
  This is a separate read-only inspection session, with no supervisor or employees.
- For specialist blocks only: employees: 1..8 objects with agent, model, instruction, outputs (nonempty list of relative
  files), acceptance (nonempty observable criteria), optionally inputs, write_scope, validation.
Use few employees for this test. Give each employee a distinct, small assignment and output
file. No shared writes between concurrent jobs. The independent inspector reads upstream
outputs and writes a scoped PASS/FAIL report; it must not rewrite producer deliverables.
At the top of team_workflow include input: {{"text": "mission input", "files": []}},
and delivery: {{"sources": ["final producer block IDs"], "inspector": "inspection block ID or null"}}.
The inspector must transitively depend on all delivery sources. Select final producer blocks, not the inspector, as sources.
Do not include task arrays or invent progress. MissionDeck compiles a supervisor brief, employee
jobs and a supervisor PASS/FAIL gate for each block. At most two native sessions run at once.
Do not launch any worker. No network, installs or spending for implementation unless the mission
explicitly permits it. Validation commands can only be python <relative_file> or
python -m pytest <relative_path> -q.

## Worker completion contract
{WORKER_COMPLETION_CONTRACT}
'''


def compile_team_workflow(workflow: Any, mission_id: str, objective: str,
                          eligible_agents: set[str] | None = None) -> dict[str, Any]:
    if isinstance(workflow, dict) and workflow.get("version") == 2:
        from app.team_workflow_v2 import compile_v2
        return compile_v2(workflow, mission_id, objective, eligible_agents)
    return _compile_v1(workflow, mission_id, objective, eligible_agents)
