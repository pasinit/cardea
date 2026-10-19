# M3 bounded agent load report — 2026-09-24

**Result: all correctness checks passed; no provisional regression warnings.** This is a repeatability reference for the unchanged M3 implementation with a deterministic provider fixture. It is not evidence of live model behavior, a throughput improvement or native Debian capacity.

## Matched workload

The separate load package ran two reference repetitions, saved an immutable [baseline](baseline.json), then ran an independent [comparison](comparison.json). Each repetition used a fresh store, four incident tasks, two closed-loop authenticated clients, two rounds per task and identical model/host allocations. Each task waited for an authenticated additive record between rounds. SPIRE, DBOS, SQLite, native OpenRouter/MCP SDKs and real systemd/bubblewrap workers were active. Only the model HTTP response was deterministic; there were no OpenRouter calls or credit charges.

The same source, dependency locks, harness fingerprint and observable guest limits were verified for both campaigns. [Reference evidence](reference/run.json) and [comparison evidence](comparison/run.json) retain per-task receipts, artifacts, verification and durable-state counts. The known outer VM/container limits are recorded with [M3 acceptance](../../../../experiments/m3/M3-REPORT.md).

| Metric | Reference min / median / max | Comparison |
| --- | --- | --- |
| Admission p95 | 2.083 / 2.357 / 2.631 s | 0.434 s |
| Completion p95 | 25.198 / 26.441 / 27.683 s | 25.394 s |
| Completion throughput | 0.0723 / 0.0751 / 0.0780 tasks/s | 0.0806 tasks/s |
| Four-task campaign duration | 51.30–55.33 s | 49.63 s |

Admission measures the authenticated submit request. Completion latency runs from submission through observed success, including the first round, event delivery and second round. Campaign throughput also includes each client's post-completion receipt/artifact inspection before its next task. Startup/final database audit are outside that throughput interval. Authentication, MCP and confinement costs are included but not individually isolated.

Admission varied sharply despite unchanged source. The lower comparison latency is not an optimization result. This small campaign under amd64 emulation on a shared development machine provides only a local comparison reference; two repetitions and four samples per point do not establish a statistical guarantee. Warning thresholds are the existing provisional 25% latency increase / 20% throughput decrease. The M1 baseline has a different workload and remains unchanged and incomparable.

## Correctness and boundaries

All 12 tasks completed: 24 synthetic model calls, 24 confined tool calls, 48 successful receipts, 24 output versions and 24 verification-object versions. Every output preserved the exact incident set/content and valid summary references. Repeated input delivery did not add effects or versions. Every store passed SQLite integrity/foreign-key checks and had exactly four durable task bindings and 16 effects. No acknowledged task disappeared.

The earlier [setup attempt](setup-attempt.json) failed during environment inventory before task submission because `systemd` was not on Debian's PATH; querying `systemctl --version` corrected it. Its output was not treated as a measured reference. No production behavior changed to obtain the passing campaign.

This is a bounded concurrent profile, not saturation testing. Crash/revocation/disconnect coverage is recorded separately in the [M3 acceptance report](../../../../experiments/m3/M3-REPORT.md). Real provider latency, usage and cost were unmeasured at campaign time. Subsequent live `z-ai/glm-4.7` restart/new-input acceptance passed with 1,541 reported tokens; see the M3 acceptance report. The measured load runs and baseline remain frozen before that compatibility follow-up.
