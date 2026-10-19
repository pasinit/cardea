# ADR-006 — Hatchet Runtime Fit

Date: 2026-09-22. Owner: kernel/runtime. Scope: R05/M1, fourth shortlisted candidate.

**Status: isolated experiment complete; 18 observation cases passed, no production adoption.** Follow [ADR-002](002-workflow-engine-comparison.md), comparing the failure boundaries tested for DBOS, Temporal and Absurd.

## Decision Before Implementation

Use Python SDK 1.41.0 and the published native `hatchet-embedded` v0.107.0 sidecar (engine 0.107.0), with a private PostgreSQL cluster on macOS arm64/Python 3.12. Main engine 0.107.2 was screened in ADR-002, but there is no matching 0.107.2 embedded release at this check. Pin the available release explicitly; do not claim these results cover 0.107.2. Docker is installed but its daemon is not running; the official native sidecar avoids starting unrelated infrastructure or inventing a replacement deployment.

Sources: [embedded release](https://github.com/hatchet-dev/hatchet-embedded/releases/tag/v0.107.0), [pinned engine dependency](https://github.com/hatchet-dev/hatchet-embedded/blob/v0.107.0/go.mod), [SDK source](https://github.com/hatchet-dev/hatchet/tree/py/1.41.0/sdks/python), [embedded mode](https://docs.hatchet.run/v1/embedded), and [durable examples](https://github.com/hatchet-dev/hatchet/blob/py/1.41.0/examples/python/durable/worker.py). Engine and sidecar source use MIT licenses. Check binary SHA-256 against the release checksum before execution: macOS arm64 `3903d6c7057ee2d1bc4d3809981287191e3106a1f3be812ae78f7d77855b98ff`.

Reuse supported durable tasks, child tasks, events, timers, retries and cancellation. Public child-task invocation provides the saved-step comparison; do not use the SDK's private `_aio_memo` as a production API. Custom code is limited to fictional provider state, fault barriers and observations. No Cardea storage, real agent, privileged credentials or production dependency changes. Generated local test credentials stay in private temporary files and out of evidence.

Requirements remain F01–F12. Record exact runtime behavior and unrun cases; a packaging or API gap is not evidence that every Hatchet deployment fails. Production deployment, Debian isolation, full supervisor/ancestor authority, two-store consistency and verified task completion remain separate acceptance gates.

## Setup Findings

The engine rejected the initially inherited PostgreSQL `Europe/London` timezone. The fixture now starts PostgreSQL with `timezone=UTC`; it does not disable the engine's UTC check. The first smoke submission also raced workflow registration; waiting for the public worker lifespan callback fixes the harness ordering. These were configuration/harness failures, not proof that durable execution is unavailable.

The pinned SDK's task typing expects structured results; the fixture uses JSON mappings for permission and effect results. Workflows and events use unique namespaces, but the initial shared-tenant setup still produced a collision when different workflows used the same literal idempotency key. Each test now supplies a unique key. Namespaces are not authority boundaries.

`lsof` confirmed the sidecar's gRPC listener on `127.0.0.1` and REST on a wildcard TCP listener. The suite uses temporary ports and generated test credentials; it does not claim all services are loopback-only. PostgreSQL listens only through the private Unix socket. The SDK and embedded/server source licenses inspected are MIT. SDK 1.41.0 was published September 22, embedded 0.107.0 September 15, and screened main-engine 0.107.2 September 18. No transitive vulnerability audit was performed.

## Observed Behavior

Read-only engine SQL identifies acknowledged durable wait registrations before crash/revocation injection. This is test instrumentation against the pinned schema, not a proposed production adapter. Public child-task invocation supplies the completed-step boundary; the fixture never calls private memo APIs. See the [evidence record](hatchet-fit-evidence-2026-09-22.json) for exact outcomes, committed effect counts, durable-entry observations, versions and source hashes.

| Probe | Observed outcome | Cardea implication |
|---|---|---|
| Completed child result; hard-kill worker during registered parent wait; start distinct worker | Original child result returns and output count remains 1 | Bounded completed-child recovery works; this is not engine/database restart evidence. |
| Scoped lookback event before registration, during registered wait, or while worker is dead | Expected first and second event payloads returned; unrelated scope ignored | Useful durable waits. Retention, simultaneous registration races and a global Cardea cursor remain untested. |
| Two waits on the same key/scope with lookback and one prior event | Both waits return the same event | Lookback is not a consuming mailbox. Event identity/correlation needs an explicit mapping. |
| Kill during an acknowledged durable sleep | Workflow returns awake and completed output count remains 1 | Durable timer recovery observed; clock skew and exact deadline semantics remain unrun. |
| Cache permission as a child result, revoke during wait or before restart | Cached success still permits one effect | Authorization cannot be replayed as a historical decision. |
| Fresh permission check at the effect boundary or retry after revocation | No effect; denied result | Positive fictional control only; no full supervisor/ancestor authentication/enforcement. |
| External effect commits; result is lost through an injected exception or hard worker death | Raw retry commits a second effect in both cases | Durable task results do not make arbitrary external effects exactly once. |
| Same exception/crash boundaries with a persisted-intent fixture | Effect count remains 1; result is `outcome_unknown` | Positive control for conservative retry handling, not implemented reconciliation. |
| Reuse a TTL idempotency key, same or changed arguments, running or completed | `IdempotencyCollisionError` with original run ID for all four cases | Native collision handling does not implement same-request receipt return versus changed-request conflict. TTL expiry is also not receipt retention. |
| Cancel durable parent while synchronous child is inside effect barrier, then release child | Parent remains cancelled; one external effect commits | Cancellation status does not prove child code or external work stopped. Default SDK configuration; no force-kill opt-out was needed. |

## R05 Acceptance Status

| Case from ADR-002 | Hatchet status |
|---|---|
| F01 acknowledged progress/restart | Bounded completed-child result survives worker process-group death and new worker. Engine/database restart unrun. |
| F02 event races/deduplication | Partial: scoped lookback before/during/offline and repeated-event reads tested. Duplicate push semantics, exact registration race, retention and ingestion cursor mapping unrun. |
| F03 timer/dependency recovery | Partial: durable sleep and child result recovery tested. Child failure/cancellation mapping and clock skew unrun. |
| F04 stale-worker fencing | Unrun. Dead-worker recovery does not test resuming a live stale worker or fencing provider effects. |
| F05 uncertain effects | Raw exception retry and crash recovery both duplicate the external effect. Intent control returns `outcome_unknown` with one effect. Full reconciliation unrun. |
| F06 changed-request conflict | Collision reports original run ID without distinguishing identical versus changed requests. Cardea digest/receipt mapping unimplemented. |
| F07 authority ceiling | Fictional revocation controls only. Authentication, ancestor intersection, disablement and concurrent dispatch/revocation races unrun. |
| F08 cancellation/deadlines | Partial parent cancellation probe; child effect can continue. Absolute queued deadlines and host descendant termination unrun. |
| F09 two-store commits | Unrun. Fictional provider state is not Cardea storage integration. |
| F10 upgrades/restore | Unrun. |
| F11 completion verification | Unrun against Cardea's verifier contract. |
| F12 Debian operations/cost | Unrun. macOS suite time is not an operating-cost benchmark. |

Hatchet remains a viable candidate. [ADR-007](007-runtime-integration-direction.md) compares the four engines and chooses DBOS for the next bounded integration experiment. Production selection remains open. Revisit Hatchet if supported task coordination or service operations meet a requirement that cannot be satisfied with the closer library integration, or if DBOS fails the commit/ownership gate.


## Reproduction and Validation

Run `HATCHET_FIT_SIDECAR=<verified binary> make hatchet-fit`; see the [experiment README](../../experiments/hatchet/README.md). Final result: **18 passed, 0 skipped, in 269.57 seconds**, on macOS arm64 / Python 3.12.0 / PostgreSQL 14.18. The sidecar, worker process groups and temporary PostgreSQL cluster were stopped afterward. Recovery after worker death took roughly 29 seconds of result waiting in several cases with the default engine settings; these observations are neither a recovery SLA nor a performance comparison.

Isolated Ruff, formatting and strict mypy pass for four Python files. Existing application checks pass: **138 backend tests, four frontend tests, lint and Python/TypeScript type checking**. The existing FastAPI/Starlette TestClient deprecation warning remains. No production kernel code, Cardea data or application dependencies changed. R05 and M1 remain open, with the next integration test specified in ADR-007.
