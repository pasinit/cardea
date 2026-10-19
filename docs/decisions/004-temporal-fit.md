# ADR-004 — Temporal Runtime Fit

Date: 2026-09-22. Owner: kernel/runtime. Scope: Temporal portion of R05/M1.

**Decision: keep Temporal shortlisted; no adoption yet.** The isolated suite passed 26 observation cases, including unsafe behaviors that still require broker integration. Requirements come from [ADR-002](002-workflow-engine-comparison.md), using the [DBOS fault points](003-dbos-fit.md) as a comparison baseline. Subsequent Absurd and Hatchet results are recorded in [ADR-005](005-absurd-fit.md) and [ADR-006](006-hatchet-fit.md); [ADR-007](007-runtime-integration-direction.md) records the next integration direction. R05 remains open.

## Decision Before Implementation

Reuse Temporal Python SDK 1.33.0 (MIT) and Temporal CLI 1.9.1 (MIT), embedding server 1.32.0, for an isolated experiment. The server and SDK are maintained, non-archived upstream projects screened in ADR-002. The CLI release is dated 2026-09-14. Check the CLI release checksum before execution. This selects test dependencies only. No production application dependencies, kernel contracts, or Cardea stores change.

Primary sources: [SDK tagged source](https://github.com/temporalio/sdk-python/tree/1.33.0), [CLI release](https://github.com/temporalio/cli/releases/tag/v1.9.1), [server release](https://github.com/temporalio/temporal/releases/tag/v1.32.0), [SDK cancellation/activities](https://github.com/temporalio/sdk-python/blob/1.33.0/README.md), and [CLI server reference](https://docs.temporal.io/cli/server).

Run on macOS arm64 with Python 3.12, using a temporary loopback-only headless development server with file-backed SQLite. This profile does not establish production PostgreSQL support, Debian enforcement, server high availability, or operational cost. An isolated server is preferable here to connecting an existing service; production deployment remains a separate acceptance gate.

Requirements: completed progress, durable signals/timers/child results, activity timeout and stale worker effects, the external-commit/result gap, operation ID argument conflicts, fresh authority on continuation/retry, and cancellation. Kill worker processes at known boundaries and count committed effects in a separate fictional SQLite provider. Also hard-restart the server after acknowledged progress. Do not infer authority or verification from engine success.

Only test instrumentation is custom: barriers, fictional authority/intent/generation controls, and effect counters. These mirror DBOS controls to measure broker requirements; they are not production enforcement mechanisms. Workflow scheduling, history, activity retries, signal transport, timers, cancellation, and recovery use Temporal APIs. Do not build a competing scheduler or deduplication service.

DBOS remains the application-library comparator. Absurd and Hatchet remain planned; none is rejected without fit evidence. Revisit the shortlist if tested gaps require disproportionate custom integration, or deployment/license requirements change.

## Interpretation and Comparison Boundaries

Compare observable semantics, not raw green-test counts: DBOS has two storage profiles; this Temporal experiment has one development-server profile and several additional cases. Both engines require trusted broker checks for uncertain effects and live authority. Their workflow IDs, histories, signal IDs and cancellation states are not direct replacements for Cardea's operation IDs, global event cursor, receipts or verified terminal states.

The Temporal fixture uses real time and server-side activity start-to-close expiry. Workers have distinct identities, one activity slot, one activity poller, and two workflow pollers (the SDK minimum with workflow caching enabled). Workflow-task timeout is two seconds; sticky-queue timeout is one second. These bound fault-injection latency and are not production tuning or throughput measurements. The expired activity is tested with default SDK thread interruption and, separately, the explicit `no_thread_cancel_exception=True` option. Do not generalize the latter to default activities or claim default Python interruption stops an already-issued remote action.

During development, a child-workflow return annotation of `object` caused payload-decoding failure; the fixture now declares its actual JSON-compatible union. A twenty-second result timeout initially expired while outstanding polls on a suspended worker held workflow tasks. Reducing the workflow-task timeout and bounding worker slots made the intended fault placement explicit. An initial expectation of duplicate effects under default thread cancellation was incorrect: the SDK interrupted the old activity. The final scenarios distinguish that protection from explicitly disabled thread interruption.

## Observed Results

The [evidence record](temporal-fit-evidence-2026-09-22.json) contains all 26 outcomes, committed effect counts, ordered history event IDs/types, dependency versions, source hashes, tagged license hashes and the verified CLI archive checksum. Passing tests assert observations, including behaviors insufficient for Cardea.

| Probe | Temporal observation | DBOS comparison / implication |
|---|---|---|
| Completed step, worker SIGKILL/restart | One output effect; original result recovered | Both recover the tested completed work. |
| Completed step, worker and server SIGKILL/restart | One output effect after reopening the file-backed development server | Extra Temporal service-restart case; this is not a backup/restore or production HA test. |
| Signals before receive, during wait, while worker down | Ordered delivery; duplicate public RPC request ID suppressed; unrelated topic ignored | Both have usable durable messaging. Temporal's high-level `signal()` generates a fresh request ID, so equal payloads sent twice arrive twice. Business-event identity still needs mapping. |
| Durable timer / successful child, worker restart | Original timer completes; child result returns; outputs not repeated | Both pass bounded timer and successful-child recovery. |
| External commit, worker dies before result; retry enabled | External effect count becomes 2 | Same uncertain-effect gap as DBOS. |
| Same crash with persisted-intent fixture | Count stays at 1 and fixture returns `outcome_unknown` | Same broker requirement. This fixture does not reconcile the actual external outcome. |
| Same crash with activity retries disabled | Count stays at 1; workflow fails after activity timeout | Native policy prevents repetition but does not supply Cardea uncertainty classification or reconciliation. |
| Permission result saved, supervisor fixture revoked before continuation/restart | Cached permission still allows one effect | Same stale-authority issue. Completed activity results are historical facts, not current authority. |
| Fresh check at effect boundary or on retry after revocation | Effect denied, count 0 | Same positive fake-provider control; no authenticated supervisor/ancestor enforcement is established. |
| Reuse completed workflow ID, default policy, changed input | New run executes new input; output count 2 | DBOS returns the old result. Neither default provides Cardea's changed-request conflict contract. |
| Completed ID with `REJECT_DUPLICATE` | Rejects both identical and changed arguments | Useful native guard, but matching retries still need an authorized receipt/result lookup and digest comparison. |
| Running ID with `USE_EXISTING`, changed input | Existing execution/result returned | No argument conflict check. Explicit mapping required for both engines. |
| Cancel unheartbeating synchronous pause | Workflow becomes cancelled; worker stays alive; releasing pause does not run subsequent effect | Comparable to DBOS step-boundary cancellation. |
| Cancel while unheartbeating effect is already in progress | Workflow becomes cancelled; releasing the activity commits one effect | Neither cancellation status proves host/external execution stopped. |
| Cancel heartbeating synchronous activity | Activity catches SDK cancellation and exits; no subsequent effect | Positive native cancellation path; does not demonstrate process-tree termination. |
| Expire suspended worker's activity, default thread interruption | Attempt 2 runs on another worker; old Python barrier receives cancellation before it can commit | A stronger observed timeout control than DBOS's forced-replacement probe, but the fault profiles differ. No claim about preempting already-issued remote effects. |
| Same expiry, explicitly disable thread interruption | Both attempts commit; count 2 | Optional non-interruptible mode still requires effect-boundary controls. This is not Temporal's default threaded behavior. |
| Same non-interruptible activity with generation guard | Current worker commits; stale worker denied; count 1 | Fixture proves the guard's placement, not real remote fencing. |
| Execution timeout expires before any worker starts | Workflow times out; later worker performs no output | Native deadline includes queued time in the tested case. DBOS's absolute queued deadline was not tested. |

## R05 Acceptance Status

| Case from ADR-002 | Temporal status |
|---|---|
| F01 acknowledged progress/restart | Passed for bounded worker restart and file-backed development-server restart. |
| F02 event races/deduplication | Partial: before/during/offline, RPC request-ID deduplication, repeated high-level payloads and topic separation tested. Exact simultaneous registration race and retention unrun. |
| F03 timer/dependency recovery | Partial: timer and child success pass. Child failure/cancellation mapping unrun. |
| F04 expired-worker fencing | Partial: real activity start-to-close expiry, distinct workers, default interruption and disabled-interruption modes tested. Cardea lease mapping, crash/revocation races and real provider fencing unrun. |
| F05 uncertain effects | Raw retries duplicate effects; no-retry policy avoids repetition but leaves a timeout. Intent fixture only; reconciliation unrun. |
| F06 changed-request conflict | Native ID policies differ from Cardea semantics. Production digest/receipt mapping unimplemented. |
| F07 authority ceiling | Partial fictional revocation tests only. No authentication, ancestor intersection, disablement, approver/service bypass, or remote authorization/dispatch race proof. |
| F08 cancellation/deadlines | Partial: queued execution deadline and cooperative cancellation pass; unheartbeating in-flight effect can continue. Host process-tree termination unrun. |
| F09 Cardea/engine two-store commits | Not run. Separate fictional provider state is not a Cardea store integration. |
| F10 upgrade/restore | Not run. Same-version service restart is not backup/restore or schema migration. |
| F11 completion verification | Not run against Cardea's verifier contract. Engine completion is not verified task success. |
| F12 Debian operations/cost | Not run. macOS development-server duration is not a production benchmark. |

## Decision and Integration

Both engines remain viable candidates for durable execution. Temporal demonstrates useful policy controls for retries, thread interruption, ID reuse and queued deadlines. DBOS has the simpler application-library deployment shape; this experiment does not establish which is cheaper or easier to operate in production. No throughput, memory, storage-growth or recovery-cost comparison was performed.

Neither engine removes Cardea's need for a trusted operation boundary with current supervisor/ancestor authorization, stable request digests, receipts, uncertainty handling, host enforcement and independent verification. Evaluate maintained components for those responsibilities under R04/R06/R07/R10; the toy fixtures do not justify bespoke production replacements.

If Temporal is selected, let it own execution scheduling/history and map Cardea task state deliberately. Do not add a second custom dispatcher. Temporal's per-execution history IDs are not a database-wide Cardea event cursor. Run IDs and reuse policies must prevent silently reopening terminal Cardea tasks. Resolve the recoverable two-store commit boundary before integration. A production Temporal deployment needs its own storage, authentication, backup and Debian evidence; development-server SQLite is not a shortcut through those gates.

The subsequent ADR-005/ADR-006 experiments complete the first shortlist pass; ADR-007 selects DBOS for the next bounded integration test. Revisit DBOS versus Temporal once production deployment evidence and the size of required adapters are measured. Preserve all v0.1 Cardea receipt and lifecycle semantics until an explicit migration is selected and tested.

## Reproduction and Verification

Follow the pinned CLI setup in the [experiment README](../../experiments/temporal/README.md), then run `TEMPORAL_CLI=/path/to/temporal make temporal-fit`. The final full run produced **26 passed, 0 skipped, in 60.78 seconds**, on macOS arm64 / Python 3.12.0 with Temporal CLI 1.9.1, server 1.32.0, SDK 1.33.0. This is one development-server storage profile. No existing services or real agents were connected.

The isolated Python code passes Ruff and strict mypy (five source files). Existing application checks pass: 138 backend tests, four frontend tests, lint and Python/TypeScript type checks. The existing FastAPI/Starlette TestClient deprecation warning remains. No application dependency, production kernel code, or authentication implementation changed. Tagged MIT licenses and current non-archived repository metadata were checked; this is not a transitive dependency security audit.
