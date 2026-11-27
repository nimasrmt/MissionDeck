# Changelog

## Unreleased - mission UI restoration

- Restore API-backed mission composer, plan graph and task inspector, execution and delivery views, agents, artifacts, and settings.
- Keep saved-state refresh read-only; label explicit reconciliation and continued dispatch separately.
- Link the default session monitor to mission control. Render planner/worker text as inert DOM text and support Persian content.
- Default new databases to auto-refresh off. Preserve existing settings.
- Recheck agent eligibility before native dispatch; reject unverified routes and model overrides, and correctly handle zero Codex quota.
- Include planners in the worker cap and serialize dispatch-capable transitions across processes.
- Add dispatch-preflight regression checks and isolated browser tests in CI. Live Windows/Orca acceptance remains pending.

## Unreleased - initial public development snapshot

- Existing Python backend, native Orca adapter, planning and local mission state.
- HTML/CSS shell; app.js and graph.js are missing.
- Offline tests included. Live end-to-end acceptance is pending.
- Portable configuration and public-source hygiene.
