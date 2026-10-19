# Kernel State and Verification Role

Follow root `AGENTS.md` and the [open-source-first review](../../docs/standards-review-2026-09-22.md). R03/R04/R05/R10: reuse schema, persistence, workflow, and validation tools; justify only the remaining domain semantics. Include selected reuse, bounded exceptions, and unresolved decisions in the handoff.

Read root `coding-standard.md`, `design.md`, and `docs/kernel-contract.md` before implementation.

## Ownership

Own new kernel state models, pure lifecycle transitions, checkpoints, object/version semantics, and completion-verifier interfaces, plus their tests. Preserve the legacy domain/evaluator code unless a scoped migration explicitly owns it. Coordinate shared schemas; do not revert other contributors' changes.

## Responsibilities

- Model agents, capabilities, tasks, operations, receipts, events, budgets, and object references independently of business workflows.
- Make task transitions and recovery decisions deterministic with injected clocks and dependencies.
- Separate execution success, uncertain effects, and verified task completion.
- Define trusted application verifiers that inspect real results under bounded authority.
- Keep customer escalation evaluators in applications, not the kernel.

Done means typed models, transition/invalid-transition tests, version-conflict tests, and false-success rejection. Kernel schema work is not complete because legacy evaluator tests pass.
