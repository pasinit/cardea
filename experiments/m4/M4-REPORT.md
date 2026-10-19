# M4 acceptance report

2026-09-24. **Complete for the bounded delegation profile.** Native async A2A, final Debian recovery/denial acceptance and the corrected matched load campaign pass. [ADR-017](../../docs/decisions/017-m4-native-a2a-delegation.md) records the open-source-first selection and exact custom integration gaps.

## Delivered behavior

An installed deterministic coordinator delegates 1–4 independent inline SHA-256/copy jobs to a distinct logical worker agent on the same Debian host. Native A2A 1.0/Python SDK 1.1.5 supplies Agent Card discovery, messages, JSON-RPC transport, task/status/history/list/artifact access and cancellation. Both agents use the trusted broker; confined workers receive no credentials. This coordination profile makes zero model calls.

Immutable parent/slot/child/message associations commit with task admission and DBOS enqueue. A2A task IDs are the actual child IDs; existing SQLite tasks, receipts and objects remain authoritative. Separate DBOS coordinator and worker queues prevent waiting parents from occupying all worker slots. Lost replies are reconciled from committed associations. Existing conditional object writes publish the parent report and verification once.

Every child is within the original supervisor's live allow/delegate permissions, platform policy, all ancestor grants/deadlines and the parent allocation. Each slot consumes one step, 10,000 tool milliseconds and 65,536 output bytes; the parent reserves one additional step and 32,768 bytes for publication. Child slots are not recycled. Dispatch additionally consumes the cumulative supervisor host pool. Children cannot submit/resume more work, and older endpoints cannot create unallocated descendants under a coordinator. Cancellation and recorded failure stop new admission and propagate to children; the parent waits for their stop/reconciliation before becoming terminal.

Successful peer artifacts are checked independently against the authorized input and stored child verification. An explicit `minimum_success` objective can tolerate failed children, which remain failed and visible in the final report. A native completed status alone never satisfies verification. See the [request example and commands](README.md).

## Executed checks

| Check | Observed result | Evidence |
| --- | --- | --- |
| Full backend and frontend regression | 290 backend tests, including 28 delegation cases; 4 frontend tests passed | [Test log](evidence/release/tests.log) |
| Native SDK persistence fit | 2 passed: SQLite restart/owner isolation and direct stale-save behavior | [Fit log](evidence/final/sdk-fit.log), [source](test_sdk_fit.py) |
| Real SPIRE/OIDC M1 regression | 46 passed in 263.69 seconds with updated dependencies | [Log](evidence/release/m1-regression.log) |
| Final Debian M4 acceptance | 7 passed in 281.47 seconds | [JUnit](evidence/release/debian.xml), [log](evidence/release/debian.log), [tasks and receipts](evidence/release/debian.json) |
| Static checks | Backend/frontend lint and types, M4 harness lint/types passed | [Lint](evidence/release/lint.log), [types](evidence/release/types.log), [harness](evidence/final/harness-checks.log) |
| Independent load package | 21 tests plus lint/types passed | [Checks](evidence/final/load-checks.log) |

The native SDK SQL store does persist tasks and history. Its direct save operation does not enforce Cardea terminal transitions, and its separate asynchronous transaction cannot join current DBOS/Cardea admission. The selected SDK request-handler extension exposes read-through task views; no second scheduler, mailbox, protocol or writable task store was introduced. This is bounded SDK client/server interoperability, not a claim of universal A2A conformance.

### Debian scenario results

| Scenario | Observed outcome |
| --- | --- |
| Native discovery, two child jobs, both required | Parent and both children succeeded; two verified receipts |
| Daemon interruption after committed child admission, before reply | Restart reused the same child IDs; both receipts succeeded, no duplicate effects |
| Daemon interruption before parent verification | Restart published one verified parent result over the same two children |
| Revoke delegation before peer admission | Parent failed; no children admitted |
| Cancel parent after worker launch | Both children and parent cancelled; worker stop observed |
| Parent budget permits only one of two slots | Second admission denied; first child stopped and parent failed |
| Corrupt one worker's returned result | Hash child and receipt failed independent verification; copy succeeded; parent completed the explicitly permitted partial objective |

Portable tests additionally cover concurrent allocation races, changed duplicate messages, malformed/broader requests, cross-parent disclosure denial, native list/history/cancel, disabled ancestors, immediate delegation revocation, capped deadlines, interrupted publication rollback, a recorded failure surviving reconstruction, an older-endpoint grandchild bypass, and migration preserving an existing receipt. M2's conservative unknown-effect accounting and no-relaunch mechanism are reused; this milestone does not create a new remote-effect recovery contract.

## Matched load comparison

The separate package completed two reference repetitions and one comparison: **12/12 parents**, 24 child receipts and 72 output/evidence objects passed. Provisional warning fields: admission_p95_ms. See the [load report](../../packages/cardea-load/reports/2026-09-24-m4/REPORT.md), immutable reference and machine comparison. The workload uses four parents through two closed-loop clients, two child jobs per parent and real SPIRE/native A2A/confined workers. M1 and M3 reference files remain unchanged and are not compared against this different profile. Native round-trip timing includes serialization, Unix HTTP, authentication and handler transactions; it is not protocol-only overhead. The initial reference failed five client observations with read timeouts and is preserved separately. The native handlers remain async, with synchronous SPIRE/SQLite work awaited through Starlette’s worker pool; two additional regressions prove other requests can progress while those operations block. A later comparison still timed out on one inspection despite complete outputs; native read-only WAL snapshots now avoid unnecessary writer contention, with policy/data consistency and revocation tests. The same workload/timeouts pass after both corrections.

## Environment, provenance and limits

The whole acceptance machine is Debian 13.7 amd64, Linux 6.12.107, Python 3.13.5, systemd 257.13 and bubblewrap 0.12.0. Its two vCPUs and 1,536 MiB guest RAM run under QEMU TCG on Apple Silicon; the outer container has two CPU, 3 GiB RAM and 128-process limits. This is one persistent Debian machine running many confined jobs, not a Debian VM per job. Timings are emulated-host observations, not native capacity estimates. [Inventory](evidence/release/guest-inventory.json) and [host limits](evidence/verified/vm-environment.json) record exact versions and configuration.

The [source manifest](evidence/release/source-manifest.json) identifies the uncommitted tested checkout; uploaded API source hashes match it. A2A SDK 1.1.5, DBOS 3.0.0, SQLAlchemy 2.0.54 and Casbin 1.43.0 are pinned. The [dependency review](evidence/final/dependency-review.json) records primary release/license metadata and limited public advisory checks. No model/provider credential was needed, read or copied for M4. Evidence contains fictional inputs and task/receipt identities, not credentials or hidden reasoning.

Earlier attempts remain separate: `evidence/initial` failed in the test harness before submission because a client argument needed a keyword; `second` passed the initial native scenario; `full` stopped at a harness assertion using the wrong receipt field despite successful child recovery. Those harness issues were fixed. `acceptance` passed six scenarios and `final` passed seven before the last authority/failure-retention refinements. `verified` establishes pre-load-fix Debian acceptance. `async-verified` passed four scenarios before exposing a cancellation/protocol-error race; its failure is preserved. `completed` passed after acknowledged cancellation was given precedence over concurrent peer errors; the second load comparison then found residual reader/writer contention. A subsequent read-only snapshot correction removes writer reservations from inspection while retaining atomic policy/data reads; `release` records the final Debian and renewed real-provider acceptance.

This profile supports one-level installed inline host jobs within one trusted broker. Arbitrary remote peers, nested plans, delegated model jobs, arbitrary agent code, streaming/push, general mailbox traffic and cross-host execution remain unsupported. Stored-object child inputs have not been added. The existing M3 model/MCP application remains separate. Production installation/upgrade, reboot/restore operations and a second supported host adapter remain M5.

The owned acceptance VM was stopped after verification, with no running Cardea worker units left; its test disk is retained. See [cleanup](evidence/release/cleanup.json) and the [delivery document manifest](evidence/release/delivery-manifest.json). M5 is next. The measured admission-latency warning remains an operational follow-up, not a demonstrated capacity limit or a relaxed correctness criterion.
