# ADR-005 — Absurd Runtime Fit

Date: 2026-09-22. Owner: kernel/runtime. Scope: R05/M1, third shortlisted engine.

**Decision: keep Absurd as a viable candidate; no adoption yet.** All 26 observation cases passed, including demonstrations of unsafe raw behavior. The requirements come from [ADR-002](002-workflow-engine-comparison.md), with [DBOS](003-dbos-fit.md) and [Temporal](004-temporal-fit.md) as comparators. Subsequent [Hatchet results](006-hatchet-fit.md) and the [integration direction](007-runtime-integration-direction.md) complete the first shortlist pass; R05 and M1 remain open.

## Decision Before Implementation

Use Absurd SQL 0.5.0 and Python `absurd-sdk==0.5.0` (Apache-2.0) on disposable PostgreSQL 14.18, with Python 3.12 on macOS arm64. Release 0.5.0 was published on 2026-08-04; the maintained, non-archived repository was screened in ADR-002. This selects isolated experiment dependencies only, not a production engine. No Cardea data or production dependency changes.

Primary sources: [tagged source and license](https://github.com/earendil-works/absurd/tree/0.5.0), [release](https://github.com/earendil-works/absurd/releases/tag/0.5.0), [tagged Python SDK](https://github.com/earendil-works/absurd/blob/0.5.0/sdks/python/README.md), and [tagged SQL](https://github.com/earendil-works/absurd/blob/0.5.0/sql/absurd.sql). The release SQL and tagged SQL have matching SHA-256 `d34309370c539f3a51f2b36b69b1f77551f8e4a14480a1c8def8bb8f40fd9aab`; vendor the unmodified release with its license for reproducibility. Main-branch documentation is not proof of pinned release behavior.

Requirements: persist completed steps across process/database restarts; durable events/timers/child results; expired-claim overlap; uncertain external effects; argument conflicts under an idempotency key; authority revocation on resume/retry; cancellation and queued deadlines. Count committed fictional provider effects independently of engine status.

Reuse public SDK methods and the unmodified SQL engine. Custom code is restricted to test barriers, fake effect/authority/intent/generation controls and observations. Read queue tables for fault placement and evidence only; do not replace scheduling or mutate engine state to force desired outcomes. Native lease expiry uses distinct workers. Child waits use separate queues as required by the pinned SDK. Existing v0.1 Cardea contracts remain unchanged.

At the start of this experiment, DBOS and Temporal were tested comparators and Hatchet remained untested. Decide using recorded fit and integration gaps, not novelty or popularity. Production authentication, supervisor/ancestor authority, effect reconciliation, Cardea/engine transactions, Debian operations, upgrades and restore remain separate gates. Revisit if the tested engine requires excessive custom integration or upstream maintenance/license changes.

## Integration Findings During Setup

The pinned SDK binds a registered task name to one queue. The initial child fixture attempted to override that queue and was correctly rejected; it now uses a distinct child task registration in the child queue. With psycopg 3.3.6, `spawn()` returns UUID objects although `SpawnResult` annotates IDs as strings. Persisting the raw child ID through a JSON checkpoint failed. The fixture now applies `str()` to that standard UUID at the serialization boundary; it does not modify the SDK or invent a new ID format.

The synchronous child-result wait polls and heartbeats while retaining a worker slot; it does not release that slot through the event-suspension mechanism. This is visible in the pinned SDK implementation, not a throughput benchmark. The first profile does not evaluate the asynchronous worker, partitioned queues, clock skew, or cleanup/retention. In particular, the installed 0.5.0 Python `sleep_for()` uses the worker's current time to calculate its wake target; main-branch claims about database-clock fixes must not be assumed for this pinned package.

## Observed Results

The [evidence record](absurd-fit-evidence-2026-09-22.json) contains all 26 outcomes, committed effect counts, run/checkpoint snapshots, dependency inventory, source hashes and license/release provenance. Observations use one PostgreSQL profile; test counts are not a ranking against DBOS's two profiles or Temporal's development server.

| Probe | Observed outcome | Meaning for Cardea |
|---|---|---|
| Kill worker after completed output and registered wait; restart | Output count stays 1 and original result returns | Completed-step recovery works in this case. |
| Same boundary, also restart PostgreSQL immediately | Output count stays 1 after reopening the same database | Database crash recovery observed; not backup/restore or migration evidence. |
| Event before receive, during wait, or while worker down | Distinct names return `first`, `second`; a second emit to the first name with changed payload is ignored | First-write-wins named events, not a consumable FIFO mailbox. Exact concurrent registration race remains unrun. |
| Await the same name twice | Both waits return the first payload | Waiting does not consume the cached event. |
| Start a second task using the same event name in the same queue | New task sees the original task's cached payload | Event names are queue-scoped. Map task/correlation/event identity explicitly; names are not Cardea global cursors or an authorization boundary. |
| Kill during durable timer | Original target survives; no full new delay or duplicate output | Bounded timer recovery passes; clock skew remains untested. |
| Kill parent and child workers while waiting, then restart both | One parent output, one child output; child result returned | Child success recovery works with distinct queue/task registrations. Parent wait holds a sync worker slot. |
| External commit followed by worker death before checkpoint | Retried step produces a second effect | Same uncertain-effect gap as DBOS and Temporal. |
| Same crash with persisted-intent fixture | Count stays 1; result is `outcome_unknown` | Positive test control, not a reconciliation implementation. |
| Same crash, one allowed attempt | Count stays 1; task fails with `$ClaimTimeout` | Retry policy can stop repetition; Cardea still needs an uncertainty/receipt mapping. |
| Saved permission check, revoke while waiting or before restart | One effect is still allowed | Cached permission is historical, not current authority. |
| Fresh check before effect or on retry after revocation | Effect denied, count 0 | Positive fake-provider control; full supervisor/ancestor enforcement remains unimplemented. |
| Reuse idempotency key with changed arguments, while running or completed | Original task/result returned; no conflict | Like DBOS, the key alone does not enforce Cardea's request-digest contract. |
| Cancel synchronous pause between effects | Cancelled state; next checkpoint rejects continuation; no new effect | Cancellation constrains engine progress without terminating the worker process. |
| Cancel heartbeating pause | Heartbeat raises cancellation and handler exits | Useful cooperative path; no process-tree guarantee. |
| Cancel inside an unheartbeating effect | Cancelled state, but released code commits one effect | Status does not prove external execution stopped. |
| Expire suspended worker claim, let distinct worker complete, resume old worker between steps | Winning effect count 1; old checkpoint cannot advance | Native claim/retry state prevents stale checkpoint continuation after reassignment. |
| Same expiry inside unheartbeating effect | Both workers commit, count 2; recorded task result remains the winner's | Checkpoint protection cannot fence arbitrary provider effects. No SDK interruption opt-out was needed in this synchronous profile. |
| Same overlap with fake generation guard | Stale worker denied, count 1 | Guard placement works in the toy transaction; remote-provider fencing untested. |
| Queue task with two-second `max_delay`, start worker afterward | State remains pending before polling; claim sweep cancels it; output count 0 | Delay policy stops late dispatch, but status changes require scheduler activity. Not a complete absolute task-deadline mapping. |
| Explicit `retry_task` after terminal failure | Same task ID runs again, reusing output checkpoint | Native operator retry can reopen an engine task. Cardea's terminal task contract requires a deliberate mapping, such as a separately linked new task. |

## R05 Acceptance Status

| Case from ADR-002 | Absurd status |
|---|---|
| F01 acknowledged progress/restart | Passed in bounded worker and PostgreSQL restart cases. |
| F02 event races/deduplication | Partial: before/during/offline waits, first-write-wins, repeated waits and queue scope tested. Simultaneous registration races, ingestion cursors and retention unrun. |
| F03 timer/dependency recovery | Partial: timer and child success pass. Clock skew, child failure and cancellation propagation unrun. |
| F04 expired-worker fencing | Partial: natural claim expiry, distinct-worker reassignment and stale checkpoints tested. In-flight external effects can repeat; real provider fencing and Cardea lease mapping unrun. |
| F05 uncertain effects | Raw retries duplicate effects. One-attempt policy fails conservatively but supplies no Cardea uncertainty classification. Intent fixture only; reconciliation unrun. |
| F06 changed-request conflict | Native idempotency keys do not compare arguments. UUID serialization needs a small standard conversion in the tested SDK/driver pair. Production digest/receipt mapping unimplemented. |
| F07 authority ceiling | Partial fictional revocation tests only. Authentication, ancestor intersection, disablement, approver/service bypass and dispatch/revocation races unrun. |
| F08 cancellation/deadlines | Partial: checkpoint/heartbeat cancellation and queued max-delay tested. In-flight effect can continue; absolute deadlines and descendant termination unrun. |
| F09 Cardea/engine two-store commits | Not run. The fictional provider database is not Cardea integration. |
| F10 upgrade/restore | Not run. Same-version database restart is not upgrade/restore. |
| F11 completion verification | Not run against Cardea's verifier contract. Engine success alone is insufficient. |
| F12 Debian operations/cost | Not run. No performance ranking from these macOS tests. |

## Comparison and Next Decision

Absurd demonstrates durable execution with PostgreSQL and ordinary Python workers, without Temporal's separate orchestration server. It still adds PostgreSQL to Cardea's initial SQLite direction. Its deployment shape merits consideration; this experiment does not establish lower operating cost, production readiness or better performance.

The core Cardea integration requirements remain across the three engines tested at this stage: live authority at the effect boundary, stable operation digests and receipts, uncertain-outcome reconciliation, real host enforcement and independent verification. Absurd adds specific mappings for persistent queue-scoped event names, separate child queues, blocking synchronous child waits and terminal operator retries. Prefer maintained components for these remaining responsibilities; the fictional guards do not approve custom production mechanisms.

The subsequent Hatchet experiment is in ADR-006. ADR-007 compares adapter and deployment requirements and selects DBOS for the next bounded integration experiment, keeping one owner of scheduling/state progression. Do not build a second custom dispatcher around the selected engine. Preserve v0.1 contracts until a tested, explicit migration exists. M1 provider/bootstrap, authenticated service/client, and the Cardea/engine commit boundary remain separate unfinished work.

## Reproduction and Verification

Run `make absurd-fit`; setup is in the [experiment README](../../experiments/absurd/README.md). The final run produced **26 passed, 0 skipped, in 41.22 seconds**, on macOS arm64 / Python 3.12.0 / PostgreSQL 14.18. SQL and SDK are pinned to 0.5.0. The temporary private PostgreSQL cluster and workers were stopped after the run. No existing service or production kernel state was used.

Isolated Ruff and strict mypy pass (three Python files). Existing application regression checks pass: 138 backend tests, four frontend tests, lint, and Python/TypeScript type checks. The existing FastAPI/Starlette TestClient deprecation warning remains. Repository metadata confirms non-archived status and source license Apache-2.0; no transitive dependency security audit was performed. Production code and application dependencies are unchanged.
