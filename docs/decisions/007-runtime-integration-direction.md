# ADR-007 — Runtime Integration Direction

Date: 2026-09-22. Owner: kernel/runtime. Scope: next R05/M1 integration experiment.

**Decision: select DBOS 3.0.0 for the next bounded Cardea integration experiment. Production adoption remains undecided; R05 and M1 stay open.** Keep Temporal as the service-based fallback, and retain Absurd and Hatchet as viable alternatives. This chooses what to test next, not permission to replace persisted contracts without a migration.

## Requirement and Evidence

Cardea needs durable headless tasks with recoverable progress, events and timers, while preserving operation receipts, terminal task semantics, live supervisor/ancestor authority and independent completion verification. Prefer the smallest supported composition that meets those requirements; do not build a competing custom workflow scheduler.

[ADR-002](002-workflow-engine-comparison.md) screened 17 projects using primary sources, pinned releases, licenses and maintenance evidence. The four shortlisted projects now have isolated runtime observations: [DBOS](003-dbos-fit.md), [Temporal](004-temporal-fit.md), [Absurd](005-absurd-fit.md), and [Hatchet](006-hatchet-fit.md). Source licenses inspected are MIT for DBOS, Temporal server and Hatchet core/sidecar, and Apache-2.0 for Absurd. Those checks do not constitute a transitive security or distribution audit. All experiments use fictional data on macOS; none is a production or Debian assessment.

| Candidate | Tested shape and useful evidence | Remaining fit cost / reason for disposition |
|---|---|---|
| **DBOS 3.0.0 — next integration candidate** | Python library; completed steps, timers, child results and messages recover on SQLite and PostgreSQL. Explicit message deduplication tested. | Fits the current language and initial database direction without adding a separate orchestration server. Two-store consistency, natural ownership failover and production operating cost are unverified. A shared database product name does not establish a shared transaction. |
| **Temporal SDK 1.33.0 / server 1.32.0 — fallback** | Development server; durable histories, signals, timers, children, native retry/cancellation/ID policies tested. | Adds a separate service and workflow/activity boundary. Revisit if distributed ownership, operational requirements or DBOS integration gaps justify it. The development server is not production deployment evidence. |
| **Absurd SQL/SDK 0.5.0 — viable alternative** | PostgreSQL functions and Python workers; database restart and natural claim-expiry overlap tested. | Requires PostgreSQL for the initial deployment. Pinned synchronous SDK needs mappings for queue-scoped first-write-wins events, separate child queues, occupied child-wait slots and terminal operator retry. These are concrete tradeoffs, not rejection for being newer. |
| **Hatchet SDK 1.41.0 / embedded engine 0.107.0 — viable alternative** | Official native sidecar with PostgreSQL messaging; public durable/child task APIs, event lookback and worker recovery tested. | Adds engine and PostgreSQL processes. Scoped lookback is not a consumable mailbox; repeated keys return collisions rather than Cardea receipts. Screened main engine 0.107.2 remains untested. No measured requirement yet outweighs the closer DBOS fit. |

This is an architectural fit judgment, not a performance ranking. Test counts, elapsed suite times and project popularity are not comparable scores. Hatchet has narrower acceptance coverage than the earlier suites; unrun cases remain unknown, not failures.

## Responsibilities and Smallest Adapter

DBOS would own runnable queues, durable execution history, saved results, timers and workflow messages inside the **trusted** runtime service. Cardea would expose its existing task/operation contract, broker effects and project inspectable state. Do not layer another dispatcher over DBOS or leave two components independently deciding whether the same task is runnable.

The existing Cardea store remains the baseline until the integration demonstrates a mapping. Preserve v0.1 digests and terminal states. Engine retry attempts may continue within an active task; explicit reruns of terminal tasks need separately linked tasks. Engine IDs, namespaces and event keys are correlation data, not authenticated identity or grants. Cardea's global event cursor cannot be replaced by a workflow-local offset without an explicit contract change.

All four experiments leave responsibilities at the broker boundary: request-digest conflicts, fresh authorization for every new effect attempt, uncertainty/reconciliation and verified completion. A task must never exceed its authenticated supervisor's current permissions/delegation authority, platform policy or any ancestor's effective authority. Cached workflow results cannot restore revoked permission; privileged runtime credentials cannot enlarge a task grant. The experiments' fake authority and intent tables do not select production mechanisms for these responsibilities. R06/R07 and the existing storage review still apply.

## Next Bounded Task: Prove the Commit Boundary

**Follow-up, 2026-09-23:** [ADR-008](008-dbos-storage-integration.md) completes this bounded experiment with 28 local cases against the actual Cardea store. Shared-file native transactions close admission and publication gaps; distinct-executor takeover is explicitly initiated and broker-fenced. Automatic failover and production migration remain unverified. The requirements below preserve the original experiment scope.

Before adding production dependencies or implementing a dispatch loop:

1. Inspect the **pinned 3.0.0** datasource/transaction APIs and test their use with a disposable copy of Cardea's SQLite schema. Current upstream documentation describes transaction tracking in the application database; verify the release implementation before relying on it. Compare that supported reuse with a standard transactional outbox/reconciliation design if admission still crosses independent commits. Record concrete gaps before any custom adapter.
2. Write down the single owner of each transition: admission, scheduling, wait registration, effect intent, result/receipt and terminal verification. Identify which existing lease/recovery functions become broker guards or are replaced, rather than silently running alongside engine ownership.
3. Inject crashes before and after Cardea admission, engine enqueue, result receipt and terminal publication. Reopening must preserve acknowledged work, reject changed-argument repeats, prevent contradictory terminal state and surface uncertain effects without blind replay. Test stale old workers at the broker boundary and recovery using supported executor identities; ADR-003's same-executor replacement is insufficient for this gate.
4. Reject a success-shaped result with incorrect artifact content using Cardea's verifier contract. Include current-authority denial on resume/retry. Keep authentication fixtures explicitly fictional until provider integration exists.

Prefer the upstream [datasource transaction API](https://docs.dbos.dev/python/tutorials/transaction-tutorial) and [database configuration](https://docs.dbos.dev/python/tutorials/database-connection) over a new transaction framework. DBOS recommends PostgreSQL for production; the initial SQLite choice needs a measured single-host support/operations decision, not an assumption of production parity. A datasource transaction is not automatically atomic with admission into a separate engine system database, and cannot atomically commit an arbitrary external effect.

Success of this bounded task permits a concrete integration decision, not M1 completion. M1 also needs real SPIRE Workload API/bootstrap validation, trusted workload/task/supervisor bindings, and an authenticated Unix-socket service/client. M2 must establish live authority enforcement and Debian confinement before autonomous tool execution. Upgrade/restore, event retention/races, absolute deadlines and Debian operational measurements remain explicit gates in their milestones.

## Revisit Triggers

Reopen the choice if supported DBOS APIs cannot preserve Cardea's receipt/state contracts, require a parallel custom scheduler, cannot meet ownership/commit tests, or have unacceptable measured Debian costs. Recheck dependency maintenance/license changes before adoption. Prefer another maintained candidate or an explicit storage migration over accumulating bespoke recovery machinery.
