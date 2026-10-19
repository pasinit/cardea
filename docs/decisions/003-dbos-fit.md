# ADR-003 — DBOS First Runtime Fit

Date: 2026-09-22. Owner: kernel/runtime. Scope: DBOS portion of R05/M1.

**Decision: keep DBOS on the shortlist; do not adopt it yet.** Runtime probes confirm useful durability and identify boundaries that Cardea must enforce. At this stage, the next comparison was Temporal against the same fault points, followed by Absurd and Hatchet. The subsequent Temporal results are recorded in [ADR-004](004-temporal-fit.md). After the four-candidate first pass, [ADR-007](007-runtime-integration-direction.md) selects DBOS for a bounded integration experiment, still without production adoption. No production kernel code or application dependencies were changed.

## Experiment

Use DBOS 3.0.0 and Python 3.12.0 in a separately locked [experiment project](../../experiments/dbos/README.md), with two storage profiles: SQLite and a disposable PostgreSQL 14.18 cluster. Host: macOS arm64, not Debian. PostgreSQL listens only on a private Unix socket and is removed after the run; no existing database service is used. Both profiles use polling, not LISTEN/NOTIFY. The exact dependency inventory, test outcomes, observations and source hashes are in [the evidence record](dbos-fit-evidence-2026-09-22.json).

The harness uses public DBOS workflow, step, messaging and startup-recovery APIs. Worker processes are hard-killed after controlled markers. A separate SQLite database stands in for an external provider and records committed effect counts. The replacement experiment suspends the old process and launches its replacement with the same executor identity; it is intentionally outside a normal deployment with distinct live executor IDs, and does not test automatic lease expiry or failure detection.

All keys, input and authority data are fictional. The intent, revocation and generation controls are minimal SQLite test fixtures that isolate integration requirements. They are not proposed production mechanisms, and are not substitutes for the R06 authorization or R07 host decisions. Their use is justified solely as test instrumentation alongside the reused DBOS engine and standard SQLite transaction behavior.

## Observed Results

The same observations were reproduced on SQLite and PostgreSQL. Passing observation tests include explicit demonstrations of behavior that **does not satisfy Cardea's requirements without integration**.

| Probe | Observed outcome | Meaning for Cardea |
|---|---|---|
| Kill after a completed step, restart in another process | Saved output remained; committed output count stayed at 1 | Completed-step recovery works in the tested configuration. |
| Send messages before receive, during waiting, or while worker is dead | Messages returned in order; repeated explicit idempotency key did not produce a second message; unrelated topic ignored | Useful durable messaging primitive. Exact registration races and retention still need tests. |
| Kill during durable timer | Original timer completed after restart without repeating output or restarting a full delay | Timer recovery works in this bounded case. |
| Kill while waiting for a child | Child result recovered; one output per parent/child | Successful child dependency recovery works; child failure and cancellation propagation remain untested. |
| External effect commits; worker dies before step result is saved | Raw recovery committed the effect a second time, even with step exception retries disabled | Step durability does not make arbitrary effects exactly once. |
| Same crash, with a separately persisted intent fixture | Effect count stayed at 1; recovery returned `outcome_unknown` | Demonstrates a required broker responsibility; full reconciliation is not implemented by this fixture. |
| Permission checked in a completed step, then revoked while waiting or before restart | Cached approval still allowed the effect | Authorization must not be a replay-cached decision. |
| Fresh permission check inside the effect fixture after revocation | Effect denied on ordinary continuation and restart; count stayed at 0 | Rechecking at the effect boundary works with the fake local authority source. |
| First attempt fails, supervisor revoked before retry | Fresh check on the next attempt denied the effect | Retry entry must revalidate authority. |
| Reuse completed workflow ID with changed arguments | DBOS returned the original result; it did not produce Cardea's changed-request conflict | Cardea still needs stable operation IDs plus normalized request-digest comparison. |
| Replace suspended worker between steps, then resume old worker | Old execution adopted the recorded winner's result; external effect count stayed at 1 | DBOS detected duplicate execution at the step boundary. |
| Replace worker already inside the external-effect step | Both workers committed the effect; count became 2 | Engine checkpoint ownership cannot preempt arbitrary in-flight external code. |
| Same replacement with a generation check inside the fake effect transaction | Effect count stayed at 1 | Fencing must reach the effect boundary; this toy transaction is not real remote-service fencing. |
| Cancel while worker is inside an ordinary synchronous step | Status changed to `CANCELLED`; worker stayed alive; next effect did not run | Cancellation stops subsequent progression, not the running process itself. |
| Cancel while already inside the external-effect step | Status remained cancelled but the released step committed one effect | Host termination/reconciliation remains necessary; status alone is insufficient evidence. |

One initial probe expected a duplicate effect after replacement between steps. That expectation was wrong: DBOS aborted the duplicate execution at the checkpoint boundary. The final suite separately tests that successful boundary protection and the still-vulnerable in-flight effect case.

## R05 Acceptance Status

| Case from ADR-002 | Current DBOS evidence |
|---|---|
| F01 acknowledged progress/restart | Passed for the tested completed-step case on both storage profiles. |
| F02 event races/deduplication | Partial: before receive, while waiting, offline delivery, explicit deduplication and topic separation pass. No injected simultaneous registration race or retention test. |
| F03 timer/dependency recovery | Partial: timer and successful child recovery pass; child failure/cancellation mapping unrun. |
| F04 expired-worker fencing | Partial: controlled same-executor replacement distinguishes step-boundary fencing from external effects. Natural expiry, unique-executor failover and real broker fencing unrun. |
| F05 uncertain effects | Raw engine insufficient: effect duplicated. Intent fixture surfaces uncertainty without duplication; end-to-end reconciliation unrun. |
| F06 changed-request conflict | Native workflow-ID reuse does not enforce Cardea's request-digest contract. Production adapter unimplemented. |
| F07 authority ceiling | Partial fixture only: stale cached checks fail; fresh checks deny simple revocation on continuation/restart/retry. Authentication, ancestor intersections, disablement, approver/service bypass and concurrent remote dispatch races unrun. |
| F08 cancellation/deadlines | Partial: cancelled status and subsequent-step stopping observed; in-flight synchronous effect can continue. Host descendants and absolute queued deadlines unrun. |
| F09 Cardea/engine two-store commits | Not run. The fake provider database is not an integration of Cardea's SQLite store with DBOS. |
| F10 upgrade/restore | Not run. |
| F11 completion verification | Not run against DBOS; successful workflow return is not verified task completion. |
| F12 Debian operations/cost | Not run. macOS test duration is not an operating-cost or throughput benchmark. |

## Integration Implications

DBOS can plausibly own workflow scheduling, saved step results, durable messages and timers. Keep those responsibilities in the selected engine rather than recreating them around it. Preserve Cardea's externally meaningful task/operation contracts only through an explicit mapping and migration plan.

The trusted operation broker must own current authorization, operation request conflicts, intent/receipt state, uncertain-outcome handling, and the host execution boundary. Whether any of those responsibilities can be supplied by further OSS components remains governed by R04/R06/R07; these experiments do not approve custom replacements.

In particular, do not wrap `check_permission()` in a durable step and treat its historical successful result as current authority. A new effect attempt must consult the live supervisor and ancestor ceilings. A completed effect can return a recorded receipt only through an appropriately authorized inspection path.

Before selection, establish one owner of task execution state and a recoverable transaction boundary between DBOS and Cardea data. Sharing the word “SQLite” does not make separate database files or connections one atomic transaction. A migration to PostgreSQL likewise requires tested persistence compatibility, not an implicit rewrite.

## Reproduction and Validation

Run `make dbos-fit`. The final run executes 38 observation cases: 19 on SQLite and 19 on PostgreSQL, with no skips. Dependencies are locked in [uv.lock](../../experiments/dbos/uv.lock). The experiment has separate Ruff and strict mypy checks. Existing application regression checks remain separate: 138 backend and 4 frontend tests, lint and type checking pass. The existing FastAPI/Starlette test-client deprecation warning remains.

No provider credentials, real agents, network-facing services or Cardea state were involved. No claim is made about deployed supervisor enforcement, security isolation, production PostgreSQL suitability, or other engines. R05 stays open. These findings justify the next comparison, not a production adoption.

Upstream references used to interpret the experiment: [DBOS lifecycle](https://docs.dbos.dev/python/reference/dbos-class), [configuration](https://docs.dbos.dev/python/reference/configuration), and [workflow messaging](https://docs.dbos.dev/python/tutorials/workflow-communication). Observations come from the pinned installed SDK and the test artifacts, not from assuming current documentation matches every release.
