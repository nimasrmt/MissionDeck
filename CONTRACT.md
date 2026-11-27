# MissionDeck — Implementation Contract v1

This file is binding for Wave A workers. The coordinator owns this file. Workers must not edit it.

> Historical note: this contract was authored under the working name "Orca Command
> Center". The product is now MissionDeck; internal module and API names are retained
> for continuity with existing local mission data. Machine-specific paths have been
> replaced with configurable equivalents.

## 1. Product goal

A loopback-only local control surface over the installed Orca runtime, located via
`MISSIONDECK_ORCA_EXE` or resolved from `PATH`. No install path is hardcoded.

The user creates a mission, chooses an eligible coordinator, triggers a real planning worker, reviews the returned plan, approves it, and starts real native Orca workers. The app observes native dispatches, validates real artifacts, and records evidence. Orca remains authoritative for runs, tasks, workers, and completion.

Hard rules:

- No simulated runs, invented metrics, fake completion, or fixture data in production.
- No shell interpolation. Fixed argument arrays only.
- No paid inference route. Only agents marked eligible by the capability matrix.
- No writes outside the registered workspace.
- No edits to Orca application files or the Orca MCP bridge.
- No package installation. Use the already-installed Python 3.12, FastAPI 0.158.1, uvicorn 0.54.0, pydantic 2.14.1, pytest, httpx.
- Maximum two app-originated workers in flight.
- Browser navigation/refresh must never dispatch inference.

## 2. Workspace and file ownership

Workspace root: the repository root, derived from `app/config.py` (`WORKSPACE`).

```
CONTRACT.md                  coordinator only
README.md                    coordinator only
requirements.txt             backend worker
run.ps1, stop.ps1            backend worker
app/__init__.py              backend worker
app/config.py                backend worker
app/security.py              backend worker
app/db.py                    backend worker
app/orca_adapter.py          backend worker
app/plan.py                  backend worker
app/execution.py             backend worker
app/main.py                  backend worker
app/static/index.html        frontend worker
app/static/styles.css        frontend worker
app/static/app.js            frontend worker
app/static/graph.js          frontend worker
tests/**                     backend worker (coordinator acceptance tests are read-only)
.orca-runs/**                runtime only
```

Frontend worker must not create Python files, edit tests, edit CONTRACT.md, or run installs.
Backend worker must not edit `app/static/**`.

## 3. Runtime

- Bind `127.0.0.1:8765` only. No `0.0.0.0`. No CORS middleware.
- `run.ps1` starts uvicorn in the foreground from the workspace using the system Python.
- `stop.ps1` stops only the process recorded in `.orca-runs/app.pid` (verify command line contains `app.main:app` before stopping).
- SQLite database at `.orca-runs/command-center.db`.
- App version string `1.0.0`.
- The app may be launched from any shell. It must not depend on `ORCA_AGENT_SESSION_ID`.

## 4. Orca adapter

`app/orca_adapter.py` exposes a class with an allowlist of operations. Every CLI call:

- uses `subprocess.run([ORCA_EXE, *args], shell=False, cwd=workspace, timeout=..., capture_output=True)`
- removes `ORCA_AGENT_SESSION_ID` and `ORCA_AGENT_SESSION_SPAWN_TOKEN` from the child environment so the app's own coordinator terminal handle is the caller
- caps stdout+stderr at 1 MiB and redacts before storing
- parses JSON from stdout; non-JSON output is an error
- raises a typed `OrcaError(code, message, details)` on non-zero exit or invalid JSON
- never accepts a raw command string from an HTTP request

Allowed operations (exact argv shapes; only these may be constructed):

```
status --json
account list --json
agent installed --json
terminal create --worktree path:<workspace> --title <title> --keep-open --json
terminal show --terminal <handle> --json
terminal close --terminal <handle> --json
orchestration run-create --environment local --objective <text> [--from <handle>] --json
orchestration run-use --id <run_id> [--from <handle>] --json
orchestration task-create --environment local --run <run_id> --task-title <title> --spec-file <abs path under .orca-runs> [--deps <json array>] [--from <handle>] --json
orchestration task-list --environment local --run <run_id> [--from <handle>] --json
orchestration task-update --environment local --task <task_id> [--status <status>] [--from <handle>] --json
orchestration worker-start --environment local --run <run_id> --task <task_id> --worktree path:<workspace> --agent <agent> [--model <model>] [--from <handle>] --json
orchestration worker-show --dispatch <dispatch_id> --json
orchestration worker-read --dispatch <dispatch_id> --limit <n> --json
orchestration worker-list --environment local --run <run_id> --json
orchestration worker-stop --dispatch <dispatch_id> --json
orchestration worker-release --dispatch <dispatch_id> --json
orchestration send --to dispatch:<dispatch_id> --subject <text> --body-file <abs path under .orca-runs> --json
```

`--from` is mandatory for run/task/worker-start mutations and omitted for read-only worker commands. The handle comes from the app-owned terminal stored in settings, never from a request.

## 5. Coordinator ownership and recovery

On first start (or when the stored terminal is gone), the app:

1. creates a terminal titled `MissionDeck — Coordinator` in the workspace with `--keep-open`
2. stores the handle in settings
3. uses that handle for all `--from` arguments

On restart:

- verify the handle with `terminal show`; if valid, reuse it
- if invalid, create a new terminal and call `run-use --id <run_id> --from <new handle>` before any further mutation
- reconcile every non-terminal mission and dispatch against Orca before allowing new starts
- if reconciliation is uncertain, mark state `unknown` and refuse new dispatches for that mission

## 6. Capability matrix

`GET /api/capabilities` is derived from `agent installed --json` and `account list --json` at request time (cache at most 30 s). Eligibility is fixed policy:

| Agent | Eligible | Rule |
|---|---|---|
| cursor | yes | `planType == "free"` and status ok; included plan only |
| grok | yes | status ok, OAuth, weekly window present; included allowance only |
| opencode | yes | free models only; model shown as `unknown` unless verified |
| codex | only if quota < 100% | currently 100% used, so not eligible |
| claude | no | API-key billing is paid metered |
| copilot, kilo, freebuff, hermes, opencode2, others | no by default | unverified billing/route |

Every agent row must include: `id`, `name`, `installed`, `eligible`, `billing`, `quota`, `model` (`"unknown"` when opaque), `evidence`, `reason`. Never invent a number. If a field is unavailable, use `null` and say why.

## 7. Data model (SQLite)

Tables (minimum):

- `missions`: id, title, objective, quality_requirements, scope, constraints, coordinator, workspace, status, plan_json, plan_version, plan_status, plan_errors_json, run_id, planner_dispatch_id, repair_count, created_at, updated_at, approved_at, cancel_requested
- `tasks`: id, mission_id, plan_task_id, title, role, agent, model, deps_json, write_scope_json, outputs_json, acceptance_json, validation_json, review_required, status, native_task_id, dispatch_id, attempt, result_summary, created_at, updated_at, started_at, ended_at
- `dispatches`: id, mission_id, task_id, native_dispatch_id, native_task_id, agent, model, state, liveness, terminal_state, last_seen_at, next_action, raw_json
- `artifacts`: id, mission_id, task_id, rel_path, size, sha256, kind, verified, validation_output, created_at
- `events`: id, mission_id, ts, type, message, data_json
- `settings`: key, value
- `audit`: id, ts, action, detail_json

All timestamps ISO-8601 UTC with `Z`. IDs are opaque strings prefixed `mis_`, `tsk_`, `dsp_`, `art_`.

## 8. Mission lifecycle

```
draft -> planning -> awaiting_approval -> approved -> running -> review -> succeeded
                                  \-> blocked / failed / cancelled
```

Rules:

- `POST /plan` allowed only from `draft`, `failed`, or `blocked` with `plan_status != generating`.
- A plan is produced by a real native planner worker. The planner brief writes exactly `.orca-runs/<mission_id>/plan.json`; the app reads and validates it after the dispatch settles.
- One planner attempt plus at most one explicit repair.
- `POST /approve` requires `plan_status == valid`; increments `plan_version`.
- `POST /start` requires `approved`, a current approved plan, no other app mission holding the execution lock, and fewer than 2 active app dispatches. It creates one Run, one native task per plan task in topological order, then dispatches ready tasks up to the cap.
- `POST /refresh` reconciles. It may dispatch newly ready tasks only while status is `running`/`review` and the cap allows.
- `POST /cancel` requires `{"confirm": true}`; calls `worker-stop` on active dispatches, marks cancellation, never kills unrelated terminals.
- Retry: only failed/blocked tasks, maximum two retries, creates a new native task and dispatch, preserves prior evidence.

Mission success requires all tasks succeeded, all declared outputs present under the workspace, all validations recorded with exit code 0, and, when a review task exists, `review.json` verdict `PASS`. Otherwise the mission is `blocked` or `failed` — never `succeeded`.

## 9. Plan schema and validation

The planner writes JSON matching:

```json
{
  "schema_version": 1,
  "mission_summary": "one paragraph",
  "architecture": "short architecture decision",
  "selection_rationale": "why these roles/agents",
  "departments": [{"id": "eng", "name": "Engineering"}],
  "tasks": [
    {
      "id": "T1",
      "title": "short title",
      "department": "eng",
      "role": "implementation",
      "agent": "cursor",
      "model": "unknown",
      "deps": [],
      "write_scope": ["src/example.py"],
      "inputs": [],
      "outputs": ["src/example.py"],
      "acceptance": ["observable criterion"],
      "validation": ["python -m pytest tests/test_example.py -q"],
      "review_required": false
    }
  ],
  "quality_gates": [{"id": "QG1", "description": "gate", "evidence": "how it is proven"}],
  "risks": ["risk"]
}
```

Validation must reject with useful messages:

- wrong schema version; missing required strings
- duplicate or malformed task IDs (`^[A-Za-z0-9_-]{1,32}$`)
- unknown dependency IDs; dependency cycles
- more than 8 tasks; zero tasks
- task agent not currently eligible
- absolute paths, `..`, backslash traversal, `.git`, paths outside workspace
- two tasks that are not ordered by dependency sharing any write-scope path
- a `review` task whose agent equals the agent of any implementation task it reviews
- `review_required` false when the plan has a review task, or no review task when the mission requires independent review

Plan file size limit 256 KiB. Invalid plans are shown with errors and never approved.

## 10. Execution and evidence

- Generate a self-contained native task spec per plan task under `.orca-runs/<mission_id>/specs/<task>.md`. Include goal, inputs, write scope, forbidden actions, outputs, acceptance, validation, and the verbatim Orca worker completion contract.
- Use `--worktree path:<workspace>`; no new worktrees for v1.
- Record native task IDs, dispatch IDs, agent, model, state, timestamps.
- On dispatch success: read a bounded `worker-read` preview (max 20 messages, allowlisted text only), verify every declared output exists and is inside the workspace, hash it, then run declared validation commands through a strict parser.
- Validation allowlist: `python -m pytest <relative path> -q` and `python <relative path>`, arguments split with `shlex`, no shell, timeout 120 s, cwd workspace, relative paths only. Anything else is rejected and recorded as `unsupported_validation`.
- Artifact index includes only files under the workspace and under declared outputs or `.orca-runs/<mission_id>/`. Preview text is redacted and capped at 64 KiB.
- Evidence is stored per task: command, exit code, bounded output, timestamp, artifact hashes.
- Native `done`/session idle alone is never success.

## 11. HTTP API

All responses JSON except file downloads and escalation Markdown. Errors: `{"error":{"code":"...","message":"...","details":{}}}`.

```
GET  /api/session                  -> {token}  (also sets HttpOnly SameSite=Strict cookie cc_session)
GET  /api/health                   -> {ok, app_version, orca:{available,version,runtime}, workspace, checked_at}
GET  /api/capabilities             -> {workspace, worker_cap, checked_at, orca, agents:[...]}
GET  /api/missions                 -> {missions:[summary...]}
POST /api/missions                 -> mission            body: {title, objective, quality_requirements, scope, constraints, coordinator}
GET  /api/missions/{id}            -> mission
POST /api/missions/{id}/plan       -> mission            body: {}   explicit inference
POST /api/missions/{id}/plan/repair-> mission            body: {}   max once
POST /api/missions/{id}/approve    -> mission            body: {plan_version}
POST /api/missions/{id}/start      -> mission            body: {}   explicit dispatch
POST /api/missions/{id}/refresh    -> mission            body: {}
POST /api/missions/{id}/cancel     -> mission            body: {confirm:true}
POST /api/missions/{id}/tasks/{task_id}/retry -> mission  body: {}
GET  /api/missions/{id}/artifacts  -> {artifacts:[...]}
GET  /api/missions/{id}/artifacts/{artifact_id}         -> file (inline text/image, attachment otherwise)
GET  /api/missions/{id}/artifacts/{artifact_id}/preview -> {text, truncated, redacted}
GET  /api/missions/{id}/escalation -> text/markdown attachment
GET  /api/settings                 -> {worker_cap, auto_refresh_seconds, redaction_enabled}
PUT  /api/settings                 -> settings           body: {worker_cap<=2, auto_refresh_seconds 0..60, redaction_enabled}
GET  /api/audit?limit=100          -> {events:[...]}
```

Mission object shape:

```json
{
  "id": "mis_...",
  "title": "...",
  "objective": "...",
  "quality_requirements": "...",
  "scope": "...",
  "constraints": "...",
  "coordinator": "cursor",
  "workspace": "C:/...",
  "status": "draft",
  "created_at": "...",
  "updated_at": "...",
  "run_id": null,
  "plan": null,
  "plan_version": 0,
  "plan_status": "none",
  "plan_errors": [],
  "planner": {"dispatch_id": null, "state": null, "agent": null},
  "tasks": [],
  "artifacts": [],
  "events": [],
  "quality": {"status": "pending", "checks": []},
  "freshness": "...",
  "active_dispatches": 0,
  "can": {"plan": true, "approve": false, "start": false, "cancel": false, "refresh": true}
}
```

Task object:

```json
{
  "id": "tsk_...",
  "plan_task_id": "T1",
  "title": "...",
  "role": "implementation",
  "agent": "cursor",
  "model": "unknown",
  "deps": [],
  "write_scope": ["src/example.py"],
  "outputs": ["src/example.py"],
  "acceptance": ["..."],
  "validation": ["python -m pytest tests/test_example.py -q"],
  "review_required": false,
  "status": "pending",
  "native_task_id": null,
  "dispatch_id": null,
  "attempt": 1,
  "result_summary": null,
  "started_at": null,
  "ended_at": null,
  "evidence": []
}
```

## 12. Security

- Loopback bind only.
- Reject requests whose `Host` is not `127.0.0.1:8765` or `localhost:8765`.
- If `Origin` is present, it must be `http://127.0.0.1:8765` or `http://localhost:8765`.
- Mutations require header `X-CC-Token` equal to the `cc_session` cookie. `GET /api/session` returns the token for same-origin JS.
- No `Access-Control-Allow-Origin` header.
- Request bodies limited to 64 KiB.
- Path resolution: `Path.resolve()`, reject if not under resolved workspace, reject `.git`, reject reparse-point escapes, reject absolute input and `..`.
- Redaction patterns: `sk-[A-Za-z0-9_-]{16,}`, `ghp_[A-Za-z0-9]{20,}`, `github_pat_[A-Za-z0-9_]{20,}`, `Bearer\s+[A-Za-z0-9._-]{16,}`, `AKIA[0-9A-Z]{16}`, long base64/hex blobs, `xox[baprs]-...`, `AIza[0-9A-Za-z_-]{20,}`.
- Untrusted worker output is rendered as text; Markdown is sanitized by allowlist (no raw HTML, no scripts, no event handlers).
- Never expose the full raw CLI JSON, worker reasoning, or environment.

## 13. UI requirements

- Dark navy industrial command center, cool blue accents, restrained. No glow on every card.
- One navigation structure: left rail with Missions, Agents, Artifacts, Settings.
- Mission composer with objective, quality requirements, scope, constraints, coordinator select. Eligible agents only; disabled agents show the reason.
- Plan view: mission summary, architecture, rationale, department lanes, dependency DAG (inline SVG, accessible list fallback), task cards with deps, agent/model, write scope, acceptance.
- Execution view: real worker cards with native task/dispatch IDs, state, elapsed time, redacted output preview, freshness. Two-worker cap visible.
- Delivery view: artifact links, validation evidence, review verdict/findings, quality gate, unresolved risks, acceptance status, escalation download.
- Right detail panel for the selected task: inputs/outputs, evidence, errors, next action, retry/stop controls.
- Explicit `Plan mission`, `Approve plan`, `Start execution` actions. No inference on navigation or refresh.
- Honest empty, disconnected, stale, unknown, and error states.
- Keyboard navigation, visible focus, reduced-motion support.
- Usable at 1366x768; scales upward.
- Persian mission text must render; detect RTL for display.
- No external fonts, CDNs, trackers, or network requests. System font stack, Vazirmatn if locally available.
- No fake data, no placeholder success, no fabricated percentages.

## 14. Offline acceptance tests

`python -m pytest -q` must pass with no network and no inference. Coordinator-provided tests in `tests/` are read-only. Backend must add its own tests as needed.

Coverage required:

- plan validation: cycles, missing deps, duplicate IDs, traversal, concurrent write conflict, review independence
- path scoping: `..`, absolute, junction/reparse escape, `.git`
- origin/host/token enforcement
- redaction
- adapter argv allowlist and shell-free execution
- state mapping and unknown handling
- idempotent mission transitions and duplicate start rejection
- worker cap
- restart reconciliation with a fake adapter
- navigation GET requests cause zero adapter mutation calls
- artifact download scoping

## 15. Definition of done (Wave A)

Backend:

- `python -m pytest -q` passes
- `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765` starts
- `GET /api/health` returns real Orca status
- `GET /api/capabilities` returns the real matrix
- a mission can be created, planned (real worker), approved, started (real workers), refreshed, and cancelled
- no fixture/default fake mission in production
- README section for startup, limits, safety, rollback

Frontend:

- all views render from the API, no fixtures
- empty/disconnected/error states work
- DAG renders from plan data
- 1366x768 verified
- keyboard focus and reduced motion
- no external assets
- artifact links point to real endpoints

## 16. Worker completion contract (include verbatim in every dispatched brief)

You may and must use native Orca communication tools for completion, required heartbeat, and blocking questions, even in a read-only/no-tools task. No nested workers; no unrelated tools. Follow injected native preamble and use your own active task ID and dispatch ID. Execute `worker_done` through your agent's shell/terminal tool (or the native Orca completion tool exposed in your preamble) as an actual command with outcome `succeeded` or `failed`, a concise summary, evidence and modified paths (if any). Printing, quoting, or describing the command in your reply is not a report and does not settle the task. Send it exactly once successfully; a failed transmission is not a receipt: inspect and retry only if unsettled. Do not end merely with conversational text. If blocked, ask through native Orca or report failed with partial results. Never file completion on another worker's behalf. Do not disclose secrets.
