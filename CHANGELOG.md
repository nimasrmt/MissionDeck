# Changelog

## Unreleased - manual team wiring and user APIs

- Make version-3 canvas links fully explicit, including input, final delivery and inspection. Add drag previews, connection controls, removal, undo and disconnected-path validation; never auto-connect a newly added block.
- Add write-only local API model settings for OpenAI-compatible and Anthropic Messages endpoints, explicit tool-call tests, native OpenCode 1/2 provider configuration and exact model selection for existing native Orca jobs. Preserve unknown billing/quota, two-session capacity and no fallback.
- Keep the six video references as a documented design direction; add restrained department colors, clearer ports and truthful flow highlights with reduced-motion support.
- Protect private API paths, remove rejected key inputs from validation responses and preserve unrelated native configuration.

## Unreleased - mission UI restoration

- Parse native account usage from `result.rateLimits`; recognize reported Cursor free/ok evidence while retaining unknown quota blocks for Codex and error blocks for Grok.
- Read the native runtime version and readiness, project status metadata, reject `ok:false` CLI envelopes and extract business identifiers from `result` instead of the request correlation ID.

- Add a read-only, allowlisted CLI diagnostic for investigating real account/usage payload shapes without exporting credentials or account identities.

- Discover an existing Orca CLI in its standard per-user Windows install when it is absent from PATH; explicit configuration and PATH retain priority.

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
