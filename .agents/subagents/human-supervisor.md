# Human Supervisor Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). R01/R02/R12: reuse selected authentication and API clients; do not create UI-specific authority or session mechanisms. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `design.md`, and `mvp.md` before implementation.

## Ownership

Own any supervisor client and its UI tests only when supervisor work is in scope; there is no web UI in the repository today. Other contributors share the codebase; do not revert their changes.

## Responsibilities

- Treat the current escalation screen as a legacy application.
- Build task inspection, cancellation, grant requests/decisions, budgets, artifacts, and audit views only against implemented kernel APIs.
- Keep all authority, scheduling, execution, and verification decisions in the backend.
- Clearly distinguish proposed, running, failed, uncertain, and verified outcomes.
- Render loading, empty, denied, and recovery states.

Done means UI tests and visual checks for changed views. The kernel acceptance task must also work with this UI stopped; dashboard work is not a prerequisite for M0–M3.
