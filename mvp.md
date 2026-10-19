# Agent OS MVP

Status: infrastructure MVP complete for the M0–M5 bounded profiles, including M5 installed Debian packaging and recovery. Updated 2026-09-25. This document preserves the original infrastructure acceptance boundary; it does not establish company product validation. See the [product brief](product.md) and [M6–M10 roadmap](roadmap.md#m6--validate-the-first-company-job) for the next delivery goals and the [M5 report](experiments/m5/M5-REPORT.md) for current operating limits.

## Goal

Prove that an agent can execute useful bounded work on a Debian host through native operations, preserve progress across a kernel restart, and produce a verified artifact without a human driving the execution.

## Boundary

- One Debian machine and one administrative trust domain.
- One kernel service with persistent metadata, event journal, and artifact storage.
- One agent runtime adapter with a deterministic test double and one real model integration.
- Bounded concurrency and one-level parent/child delegation through the installed M4 coordinator; up to four independent inline hash/copy jobs.
- A fixed installed operation registry. No arbitrary plugin installation.
- Scoped workspace I/O, controlled local tool execution, event waiting, and independent result checks.
- A CLI/API for submission, inspection, cancellation, and grants. No new UI is required.
- No SaaS writes, real customer data, broad host filesystem access, or unrestricted outbound worker networking.

## Implementation Constraint

Apply [open source first](docs/standards-first.md) to every required behavior. Before implementing a mechanism, record the selected standard/project or a demonstrated gap using the [review register](docs/standards-review-2026-09-22.md). The requirements below specify outcomes, not a mandate to write a database, scheduler, identity issuer, sandbox, or agent framework. Reused implementations must pass the same acceptance tests.

## First Acceptance Scenario

Give an agent a directory of fictional incident records and the objective: produce a structured incident index and a Markdown summary, then update them when one additional record arrives.

1. An authenticated supervisor is bound as the task’s accountable principal, provisions an agent identity, grants only permissions they currently hold and may delegate, including read access to inputs and write access to an output namespace, and sets a deadline, execution limits, and model budget.
2. The agent discovers the permitted operations, inspects input objects, and chooses its steps. A fixed test double exercises the same interface for deterministic recovery tests.
3. It runs an installed local validation/transformation tool in a confined worker, publishes versioned output artifacts, and checkpoints explicit progress.
4. It registers a durable event wait from a known cursor. Stop the kernel at this boundary, start it again, and publish the new input event. Also add an input file while the service is down: the input adapter must discover it on restart and durably record the corresponding event.
5. The task resumes from durable state, processes the new input, and updates outputs without repeating previously committed effects.
6. A deterministic verifier checks the index against the input set and confirms every summary reference resolves. This checks observable acceptance criteria, not universal semantic correctness.
7. The task terminates with artifact references, operation receipts, and usage totals. No dashboard interaction is needed after submission; the new input is supplied by the test harness.

The real-agent run must show the agent choosing operation calls from the registry. A hard-coded workflow passing the test-double suite alone does not demonstrate agent-first execution.

## Required Kernel Behaviors

| Area | MVP acceptance |
|---|---|
| Identity and authority | Authenticate caller and accountable supervisor; task permissions never exceed the supervisor’s current permissions/delegation authority. Deny ungranted access and propagate supervisor revocation/disablement. |
| Task lifecycle | Persist queued/running/waiting/terminal states; support cooperative checkpoints and cancellation. |
| Operations | Typed discovery/invocation, stable IDs, explicit side effects, receipts, and retry rules. |
| Storage | Versioned objects and conditional updates; restart-safe state and artifacts. |
| Events | Durable cursors and wakeups with no lost event at the checkpoint/wait boundary. |
| Execution | Enforce declared filesystem, network, process, time, and resource limits on Debian. |
| Budgets | Bound steps, model calls/tokens, tool duration, output size, and concurrency; report actual usage. |
| Recovery | Handle crashes before dispatch, after effect/before receipt, and after commit without unsafe replay. |
| Completion | Independent checks determine success; agent claims and tool exit codes alone do not. |

## Failure Demonstrations

Deny a cross-namespace read and a symlink escape. Reject an operation after grant revocation. Stop a timed-out process tree. Surface malformed agent output and model failures as bounded errors. Prevent a duplicate request with changed arguments from reusing an earlier operation ID. Preserve an uncertain external effect as `outcome_unknown` until reconciliation rather than inventing success.

A task restart resumes at an explicit durable boundary; it does not restore arbitrary native process memory or replay a nondeterministic model call transparently.

## Not in This MVP

Multi-host execution, multi-tenant enterprise deployment, general browser automation, email/CRM connectors, arbitrary network access, a knowledge graph, unconstrained self-modification, a new human command center, and support claims for other Unix kernels.

## Relationship to Existing Code

Keep the existing demo runnable during implementation. Introduce a separate kernel package; do not redefine the demo's customer `ProcessObject` as an agent task. Reuse typed models, policy patterns, and audit ideas only after adapting them to durable, authenticated kernel semantics. Current demo tests remain regression checks and do not count as the acceptance tests above.
