# Cardea Agent Instructions

Updated 2026-09-25. The current project is a hosted, agent-first operating layer. The pre-rescope customer-escalation demo is archived on the `archive/legacy-escalation-demo` branch.

## Mission

Build a userspace kernel above Debian/Linux that lets agents discover permitted operations, execute bounded tasks, persist progress, wait for events, recover after interruption, and return independently checked results. Humans supervise objectives and authority. The browser is not required for ordinary authorized execution.

## Read Before Implementation

1. `../design.md`: architecture and host boundary.
2. `../docs/kernel-contract.md`: proposed primitive semantics.
3. `../mvp.md`: initial delivery boundary and acceptance scenario.
4. `../roadmap.md`: dependency-ordered milestones.
5. `../coding-standard.md`: mandatory implementation practices.
6. `../validation-plan.md`: verification and adoption evidence.
7. `../docs/standards-first.md` and `../docs/standards-review-2026-09-22.md`: reuse policy and component/milestone decisions.
8. `../product.md` and `../docs/product/validation.md`: company application and cross-department product direction, evidence and adoption gates after M5.

Current user instructions take precedence. Architectural scope comes from design, operation semantics from the kernel contract, and sequencing from the roadmap. Historical enterprise research and legacy code do not override the new scope.

## Implementation Direction

- Retain Python 3.12+, FastAPI transport, Pydantic, pytest, ruff, mypy, uv, and root Make targets.
- Start with one Debian host, SQLite metadata/journal storage, a private artifact store, and an authenticated local client.
- Keep the kernel deterministic; model reasoning lives in runtime adapters with bounded authority.
- Implement a deterministic adapter for fault injection and one real model adapter for agent-choice evidence.
- Keep UI and agents on the same authorization/operation path.
- Other Unix hosts require adapters and conformance evidence; no silent weakening of execution profiles.

## First Build Target

M0 contracts and selection gates are complete (ADR-001). M1 authenticated service, live authority, DBOS execution and shared-store recovery are complete for the bounded headless profile (ADR-010). M2 is complete for its fixed Debian profile (ADR-012–015): two confined operations, exact-version input grants, transactional host budgets and recoverable staging cleanup. See the [M2 report](../experiments/m2/M2-REPORT.md) for observed acceptance and limits. M3 is complete for its bounded incident profile under ADR-016: the OpenRouter broker, native MCP, two-round task, accounting, deterministic Debian recovery/denial scenarios, live `z-ai/glm-4.7` restart/new-input acceptance and matched load comparison pass. See the M3 report. M4 is complete for bounded deterministic delegation under ADR-017: native async A2A handlers, shared child allocations, live authority, cancellation/recovery, independent verification and matched Debian load evidence pass. See the M4 report. M5 is complete for the supported Debian profile under ADR-018/019: installed upgrades, incompatible downgrade refusal, full-machine interrupted-effect/wait recovery, artifact-checked snapshot restore with sealed scopes and fresh authority/budgets, redacted diagnostics and explicit retention pass. See the M5 report and operations guide. Current-store restart and historical snapshot restore have deliberately different semantics. Arbitrary remote peers and nested/delegated model plans remain outside the delivered profiles. Existing demo tests do not establish kernel guarantees.

## Operating Rules

- For M6 onward, follow the product roadmap: prove one useful cross-team company process with integrated data and systems, then reuse for additional processes. Keep proposed demand, selected designs, implementation, technical verification and customer adoption separate. Product milestones require observed outcomes; synthetic demos do not close them. A new UI or kernel feature is not itself evidence of product value.
- Follow [standards-first decisions](../docs/standards-first.md): evaluate established open standards and maintained open-source implementations before custom mechanisms. Record concrete gaps and reuse everything that meets requirements; this applies across the architecture, including existing proposals.
- Preserve the existing demo until a deliberate port or removal is requested.
- Do not encode customer escalation, CRM, or team-specific evaluators as kernel concepts.
- Do not treat caller-supplied actor strings as authentication or model output as authority.
- A task and every descendant must remain within its accountable supervisor’s current permissions and delegation authority. Preserve this live ceiling through grants, approvals, retries, resume, and revocation; service privileges cannot enlarge task authority.
- Persist state and journal events consistently; reconcile uncertain effects before retrying.
- Ordinary in-scope operations execute under grants without mandatory human preview/approval clicks.
- Keep credentials and service storage outside worker reach.
- Require observed artifacts/state for verification, never an unconditional success flag.
- Test actual host enforcement on Debian; report unsupported profiles and unrun host tests explicitly.

## Readiness and Handoff

An implementation task names the primitive, client, authority, durable transition, host effect, recovery behavior, and acceptance check. Documentation and tooling work are exempt from requiring a runtime effect.

When work is explicitly split, the role files in `subagents/` define ownership. Their filenames are retained for continuity but their contents reflect the new architecture. Each handoff lists files changed, reused standards/projects and decision records, justified custom gaps, implemented behavior, contracts, tests/environment, and remaining guarantees. Completion requires the relevant reuse decision and acceptance evidence; open choices are reported explicitly. Contributors are not alone in the workspace and must preserve others' edits.
