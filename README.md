# MissionDeck

A loopback-only local control surface for real Orca missions. The user writes a mission, chooses an eligible coordinator, triggers a real planning worker, reviews the returned plan, approves it, and starts real native Orca workers. Orca remains authoritative for runs, tasks, workers, and completion.

This is not a second orchestration engine and not a simulation. If Orca is unavailable, the UI says so. If a plan is invalid, it is blocked. If a worker has not produced a validated artifact, the mission is not successful.

MissionDeck was previously developed under the working name "Orca Command Center". The product identity is MissionDeck; the `app/` package and HTTP API keep their original internal names to avoid breaking existing local mission data.

## Project status

This repository is published as a **work in progress**, not a finished release.

| Area | State |
|---|---|
| Backend (`app/`) | Implemented: plan validation, execution, evidence, reconciliation, artifacts, audit |
| Offline tests (`tests/`) | Present. Run them yourself — see [Tests](#tests). CI configuration included; remote results pending |
| Frontend HTML/CSS | Present (`app/static/index.html`, `app/static/styles.css`) |
| Frontend JavaScript | **Missing** (`app/static/app.js`, `app/static/graph.js` are referenced by `index.html` but absent) |
| Live end-to-end mission | **Not verified**; prior orchestration tests are not evidence of a full MissionDeck mission |

Because the two frontend scripts are absent, **the browser UI is not currently operational**. The HTTP API has offline test coverage; live Orca integration is not yet accepted. The static shell cannot drive it. Do not treat the interface as working until `app.js` and `graph.js` are restored or rewritten.

## Requirements

- Windows (the launch scripts are PowerShell; the Python app itself is portable)
- Orca 1.4.x, developed against 1.4.278
- Python 3.12 with FastAPI 0.158.1, uvicorn 0.54.0, pydantic 2.14.1, pytest, httpx
- No Node, Docker, database server, or external service

## Configuration

MissionDeck has one optional environment variable:

| Variable | Purpose |
|---|---|
| `MISSIONDECK_ORCA_EXE` | Absolute path to the Orca CLI. Unset → resolve `orca` from `PATH`. |

MissionDeck does not require provider API keys in its own configuration; the local Orca runtime manages provider authentication. See [`.env.example`](.env.example); the app does not auto-load `.env` files.

## Start

```powershell
cd <path-to-this-repo>
# If Orca is not on PATH, set MISSIONDECK_ORCA_EXE to the actual CLI path.
.\run.ps1
```

Open `http://127.0.0.1:8765`.

Stop:

```powershell
.\stop.ps1
```

The app binds loopback only. It stores its own metadata in `.orca-runs\command-center.db` and its coordinator terminal handle in the same database. It never modifies Orca application files. Runtime state is ignored by Git and must never be committed.

## Workflow

1. **Create a mission.** Title, objective, quality requirements, scope, constraints, and an eligible coordinator.
2. **Plan mission.** Explicit action. A real native planner worker writes `.orca-runs\<mission>\plan.json`; the app validates it. Invalid plans are blocked with errors; one repair is allowed.
3. **Approve plan.** Only a valid plan can be approved. The approval records the plan version.
4. **Start execution.** Explicit action. The app creates one native Run, one native task per plan task in dependency order, and dispatches ready tasks with a maximum of two app-originated workers.
5. **Refresh.** Reconciles against Orca. It never dispatches from navigation or a plain page load. Auto-refresh is off by default.
6. **Delivery.** Artifacts, validation exit codes, review verdict, quality gate, risks, and an escalation export.

## Eligibility and cost policy

The capability matrix is derived from Orca's own `agent installed` and `account list` output at request time. It does not invent quota.

- Cursor: eligible only on the included free plan with status ok.
- Grok: eligible only with status ok and an included weekly allowance.
- OpenCode: eligible for free models only; the effective model is shown as `unknown` unless Orca reports it.
- Codex: eligible only below 100% of its current window.
- Claude: excluded because the available route is paid API-key billing.
- Copilot, Kilo, Freebuff, Hermes, OpenCode 2, and unverified providers: excluded by default.

The app never enables paid overages, buys credits, or uses an automatic paid fallback. Unknown cost is treated as blocked. Model identity is shown as `unknown` when the provider does not expose it; a client name alone is not treated as proof of model quality.

## Security model

- Loopback bind only (`127.0.0.1:8765`), Host/Origin checks, no permissive CORS.
- Mutations require a same-site session cookie plus a matching `X-CC-Token` header.
- The Orca adapter accepts only fixed, allowlisted argv arrays. No HTTP request supplies a command string. `shell=False`.
- The app owns one dedicated Orca coordinator terminal and uses its handle with `--from`. It never borrows another terminal handle. The child CLI environment removes `ORCA_AGENT_SESSION_ID` and `ORCA_AGENT_SESSION_SPAWN_TOKEN` so the owned handle is the caller.
- Paths resolve under the registered workspace; absolute paths, `..`, `.git`, and reparse-point escapes are rejected.
- Worker output is treated as untrusted text. Provider reasoning is not stored or rendered. Text previews are capped and redacted.
- Validation commands are restricted to `python -m pytest <relative path> -q` and `python <relative path>`, with no shell, a 120 s timeout, and workspace cwd.
- Success requires declared outputs, hashes, validation exit code 0, and a review PASS when a review task exists. Native `done` alone is not success.

## Known limitations

- **Development snapshot only. Do not enable live dispatch on a sensitive workspace.** Native identifier extraction, provider eligibility, and live worker recovery still require verification. Validation commands execute project Python code; only use trusted project workspaces.

- **The browser UI is incomplete**: `app/static/app.js` and `app/static/graph.js` are missing. The API and tests are unaffected.
- The two-worker cap is enforced for dispatches started by this app. Other Orca clients can start workers outside it.
- The app does not implement automatic quality-based failover or automatic quota-aware routing. A failed provider is a supervised decision, not an automatic paid fallback.
- Effective models for Cursor, Grok, and some OpenCode routes may be opaque. They are shown as `unknown`.
- The planner is a native worker. A weak planner can produce a weak plan; validation blocks structural errors, not poor judgment. Review independence is checked by agent, not by quota pool.
- One planner repair is allowed. Task retries are capped at two.
- The app does not modify Orca settings, install agents, or create new provider accounts.
- Windows-only launch scripts. The Python app is not yet exercised on other platforms.

## Tests

```powershell
python -m pytest -q
```

Offline tests use a fake adapter and require no inference and no network. They cover plan validation, path scoping, origin/CSRF checks, redaction, state mapping, worker cap, duplicate start rejection, restart reconciliation, and artifact scoping. Tests create scratch state under `.orca-runs/`, which is ignored by Git.

## Contributing

`main` is the canonical branch and should stay releasable.

1. Open or pick up an issue describing the change.
2. Branch from `main` using `type/short-description`, for example `fix/frontend-graph`.
3. Keep changes scoped; do not commit runtime state, `.env` files, databases, or logs.
4. Run `python -m pytest -q` before opening a pull request.
5. Open a pull request against `main` and describe what you verified, including any checks you could not run.

Security issues: report them privately rather than in a public issue.

## Rollback

1. `.\stop.ps1`
2. Back up `.orca-runs/` before any reset. A reset loses mission metadata; only remove local database files after confirming that their data is no longer needed.
3. Close the Orca terminal titled `MissionDeck — Coordinator` if it remains. Terminals created by earlier builds may be titled `Orca Command Center — Coordinator`.
4. The application does not change Orca configuration, so no configuration rollback is required.

## License

No license has been declared yet. This repository is publicly visible, but an open-source license remains pending confirmation of source ownership and third-party licensing. Default copyright applies in the meantime.
