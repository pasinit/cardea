# Authority, Operations, and Journal Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). R03/R04/R06/R09/R11: evaluate policy engines and existing transactional infrastructure before custom grants, accounting, or queues. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `design.md`, and `docs/kernel-contract.md` before implementation.

## Ownership

Own kernel grant enforcement, operation admission/dispatch semantics, resource accounting, and journal/receipt contracts with targeted tests. Coordinate persistence and host boundaries. You share the codebase; do not overwrite other owners' work.

## Responsibilities

- Enforce scoped authenticated authority at admission and dispatch, including reads and execution. Task permissions must remain within the live accountable-supervisor and ancestor ceilings; reject overbroad grants and propagate revocation/disablement.
- Implement expiry/revocation and later attenuated delegation without budget amplification.
- Persist intent before effects and bind deduplication to principal, task, operation ID, and argument digest.
- Commit state/journal changes consistently; retain uncertain outcomes for reconciliation.
- Separate human authority escalation from ordinary granted operations.
- Record evidence and usage without credentials or unnecessary sensitive content.

Done means denial/revocation tests, changed-argument duplicate rejection, crash-window tests, and auditable receipts. Do not promise exactly-once external effects or tamper resistance against the host administrator.
