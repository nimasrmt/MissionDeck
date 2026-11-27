# MissionDeck development rules

Orca is the authoritative orchestration engine; MissionDeck is its local control and observation surface. Keep the application lightweight and truthful.

- Read CONTRACT.md as the initial interface specification; verify actual Orca CLI behavior rather than assuming it.
- Use separate branches/worktrees for independent changes, and reviewed pull requests to main.
- Keep a maximum of two active workers by default. No paid overages or new subscriptions, and no inference from dashboard refreshes. Unknown model or quota values must be shown as unknown.
- Use Claude Desktop only for high-value supervision, unresolved blockers, or independent reviews. The DeepSeek Harness may perform bounded, independently verifiable helper tasks only after its billing is authorized.
- Keep credentials, user paths, runtime databases, logs and private worker history out of Git. Never expose an arbitrary shell endpoint.
- Preserve real task evidence and persistent checkpoints. Do not claim live acceptance on mock evidence.
- Record actual validation results and blockers. Use EXPOSE, Red Team, Devil's Advocate and Autopsy proportionally at meaningful milestones.
