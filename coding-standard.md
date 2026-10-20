# Developer Coding Standard

Updated 2026-09-22 for the hosted agent OS scope and open-source-first review.

## Authority and Scope

Current user instructions take precedence. For repository documents, use `design.md` for architectural scope, `docs/kernel-contract.md` for operation semantics, `mvp.md` for the first delivery boundary, and `roadmap.md` for sequencing. This file governs implementation practice within that scope. Historical enterprise workflow documents and legacy code do not override it.

New kernel behavior should name the agent/client, primitive, authority, durable state transition, host effect, failure/recovery semantics, and observable acceptance check. Do not require a customer, team workflow, CRM record, or dashboard to justify a kernel primitive.

## Open Source First

Prefer established open standards and maintained open-source implementations across all components. Before choosing custom protocols, authentication, authorization, storage, scheduling, or execution machinery, record candidate solutions, requirements, evidence of gaps, and the smallest necessary extension. Familiarity or a desire for control is not a sufficient rejection reason. Assess security, license, maintenance, interoperability, deployment cost, and failure semantics. Follow [the decision policy](docs/standards-first.md); existing bespoke proposals must pass the same review before implementation.

Use the [decision-record template](docs/decision-record-template.md) and [review register](docs/standards-review-2026-09-22.md) before implementing a mechanism. Milestone completion requires a selected reuse approach or a documented bounded exception plus acceptance evidence. A candidate list alone does not establish suitability.

## Stack and Layout

Retain Python 3.12+, Pydantic v2, pytest, ruff, mypy, uv, and root Make targets. Keep FastAPI as a transport boundary and React/TypeScript as an optional supervisor client. Use SQLite for the initial local persistent metadata/journal store and a private artifact directory. Dependency and host versions must be pinned and tested during implementation.

Layout:

- `apps/api/src/cardea/kernel`: task lifecycle, capabilities, dispatch, events, objects, budgets, and persistence interfaces.
- `apps/api/src/cardea/runtime`: deterministic and model-backed agent adapters and trusted model broker.
- `apps/api/src/cardea/hosts`: Debian adapter and host conformance interfaces.
- `apps/api/src/cardea/service`: authenticated daemon, policy, budgets and runtime dispatch.
- `apps/api/tests`: portable contract tests, recovery integration tests, and clearly marked Debian host tests.
- `packages`: actual shared/generated contracts; do not publish imagined endpoints as implemented APIs.

Do not import business-specific evaluators into the kernel. The pre-rescope escalation demo was removed on 2026-10-10; build any such feature deliberately as an application, never as an alternate authority path.

## Kernel Boundaries

- Keep policy, task transitions, budget admission, and retry classification deterministic and typed.
- Inject storage, clock, model, and host effects; pure lifecycle tests must not require network access.
- Route every effect through an authenticated capability check and operation broker, including reads and process creation.
- Runtime adapters and UIs cannot access kernel storage directly or choose their own authoritative actor IDs.
- Host-specific enforcement belongs in host adapters. Fail closed when an execution profile cannot be enforced.
- Keep privileged host setup narrowly scoped; agent workers never inherit administrative privileges.
- Treat external content and model output as data. They cannot alter grants or bypass operation validation.

## Contracts and State

Use explicit models for agent identity, capability, task, operation, receipt, object version, event, checkpoint, and budget. Operation schema versions and compatibility behavior must be explicit before external clients depend on them.

Bind requests to the authenticated principal and task. Use stable operation IDs and canonical argument digests; same ID with changed arguments is a conflict. Return machine-readable error codes, retry classification, and receipt IDs. Paginate object/event listings and bound request/response sizes.

Persist state and related journal events transactionally. Persist artifacts with a recoverable publication protocol; a database transaction alone does not atomically commit a separate file or external effect. Use conditional writes for shared state and durable task claims/leases where concurrent dispatch is possible.

Checkpoints contain explicit state and references. Do not claim arbitrary process-memory restoration, deterministic model replay, or exactly-once external execution. Represent uncertain effects and reconcile them before retrying.

## Authority, Isolation, and Data

Use authenticated identities and scoped grants with expiry/revocation. Task permissions must never exceed the accountable supervisor’s current permissions and delegation authority. Intersect explicit grants, that live ceiling, ancestor authority, and platform policy; reject overbroad grants and recheck before every authorized operation. Persist supervisor/provenance bindings, propagate revocation/disablement to descendants, and prevent privileged broker credentials or human approval from bypassing the ceiling. See the supervisor-authority section of [the kernel contract](docs/kernel-contract.md). Delegation may attenuate permissions and share a parent ceiling; it must not mint independent unlimited budgets. Human approval grants specific authority tied to the requested operation or scope. Operations already authorized by policy do not need a human click.

Workers receive only granted inputs, a private working area, and declared outputs. Keep service storage and credentials outside worker reach. Filesystem path normalization is not a sandbox: enforce and test host boundaries, symlink handling, network policy, process descendants, and resource limits.

Separate model/provider credentials from workers. Select context under read and disclosure permissions. Persist necessary provenance and usage, not credentials or hidden reasoning. Use fictional test data. Define retention and deletion before real sensitive data is introduced. Audit is application-append-only, not advertised as proof against host administrators.

## Execution and Budgets

Declare side effects, timeouts, output bounds, idempotency/reconciliation rules, cancellation behavior, and verification evidence for every registered operation. Installed operation implementations are trusted code; accepting arbitrary plugins is a separate scope decision.

Reserve resources before dispatch and settle recorded usage afterward. Refuse work with insufficient remaining budget. Document bounded overshoot for in-flight calls and unknown usage after provider failures; a price estimate is not a guaranteed provider invoice ceiling.

Kill/reconcile descendant processes on timeout or cancellation. Successful exit is execution evidence, not proof that a task objective is satisfied. Completion checks must inspect resulting state or artifacts.

## Code and UI Practice

Use type hints at public boundaries, strict external input validation, domain-specific errors, timezone-aware timestamps, and stable identifiers. Keep route handlers thin and avoid global mutable task state. Keep business-specific code outside the kernel.

The supervisor UI renders authoritative backend state and submits operations through the same enforcement path as agents. It may inspect, cancel, or request/grant authority according to its identity. No browser logic decides permission, invents verification, or drives mandatory task steps.

## Verification

Run checks appropriate to changed behavior. New runtime primitives require contract and targeted integration tests. Authority and host changes require denial-path tests; persistence changes require restart/fault-injection tests; operation changes require duplicate-request and uncertain-outcome cases.

Run `make test`, `make lint`, and `make typecheck` for implementation changes. Run frontend build/visual checks when applicable. Keep Debian enforcement tests separately identifiable and report when they were not run; portable tests cannot establish host isolation. Documentation-only changes need link, consistency, and diff checks rather than new behavioral tests.

Report implemented behavior, actual tests, environment, and unresolved guarantees. Do not check generated API changes in unless they match implemented endpoints.

## Documentation and Delivery

Update contracts and status when primitives or guarantees change. Clearly distinguish proposed, implemented, and verified-on-Debian behavior. PR descriptions should explain the concrete behavior, validation, and relevant compatibility/security limitations. Avoid unrelated refactors and speculative scaffolding.
