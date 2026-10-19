# M4 native A2A load report

2026-09-24. Correctness: **12/12 parent tasks passed**, with 24 successful child receipts, 36 total task bindings and 72 independently checked output/evidence objects at version 1 across three fresh stores. Database integrity/foreign-key checks and duplicate admission passed. No model calls were made.

Profile: four parents per repetition, two closed-loop clients, two inline hash/copy children per parent, child concurrency two, real SPIRE JWT-SVID authentication and native A2A 1.0 over Unix HTTP on the confined Debian host. The [baseline](baseline.json) is derived from two [reference repetitions](reference/run.json); the independent [comparison](comparison/run.json) uses the same workload, harness, locks and host limits. Existing M1/M3 references are unchanged and incomparable with this profile.

| Run | Admission p95 | Completion p95 | Parents/second | Correctness |
| --- | --- | --- | --- | --- |
| Reference 1 | 3051.2 ms | 29.56 s | 0.0698 | 4/4 |
| Reference 2 | 737.0 ms | 28.07 s | 0.0697 | 4/4 |
| Comparison | 3105.1 ms | 27.65 s | 0.0681 | 4/4 |

Relative to the reference medians: admission_p95_ms: +63.9%; completion_p95_ms: -4.0%; completion_rps: -2.4%. Provisional warning fields: **admission_p95_ms**. Warning thresholds are >25% latency increase or >20% throughput decrease; they are not confidence intervals or service-level objectives. See [machine comparison](comparison.json) with exact fingerprints and ratios. Four samples per point and two reference repetitions are too small for capacity or statistical significance claims.

## Native A2A round trips in the comparison

| Method | Calls | Median elapsed | Sum elapsed |
| --- | --- | --- | --- |
| AgentCard | 10 | 306.9 ms | 3.16 s |
| GetTask | 12 | 741.9 ms | 9.21 s |
| SendMessage | 8 | 2598.2 ms | 21.50 s |

Round trips include serialization, private Unix HTTP, SPIRE authentication, policy and handler transactions. Concurrent durations can overlap; sums are not an additive decomposition of task latency or protocol-only overhead. Poll counts depend on completion timing. Timings come from an acceptance-only HTTPX transport wrapper that records method, numeric status and duration, never credential or payload content.

## Failed attempt and correction

The [initial reference attempt](../2026-09-24-m4-attempt1/reference/run.json) failed correctness because five client observations hit the unchanged 10-second read timeout. It is not an accepted baseline. Synchronous identity/SQLite operations in async A2A handlers blocked the service event loop under contention. The handlers remain async; Starlette's awaited worker pool now contains those blocking kernel operations. Two regression cases prove other authenticated requests progress while authentication or admission is blocked. The workload, two-client concurrency and client/peer timeouts were not relaxed. A second campaign passed both references but one comparison inspection still timed out while all outputs were stored; that [attempt](../2026-09-24-m4-attempt2/comparison/run.json) is also retained. Read-only API and A2A views now use native SQLite WAL snapshots with both policy and data in one transaction, without claiming the writer. Admission and effects keep immediate writer transactions. Regression tests prove concurrent inspection, committed revocation visibility and read-only enforcement. The final reference and comparison both use these corrections; no performance improvement ratio against the incomplete attempt is claimed.

## Environment and scope

Debian 13.7 amd64, Linux 6.12.107, Python 3.13.5, systemd 257.13, bubblewrap 0.12.0, SDK 1.1.5 and DBOS 3.0.0. QEMU TCG runs a two-vCPU/1,536-MiB guest on Apple Silicon inside a two-CPU/3-GiB/128-process container. This represents the whole persistent host, with many confined worker processes. It is not native Debian capacity or an overnight/saturation stress result. Other development-host activity was not eliminated.

Each run embeds source/harness hashes, dependency-lock hashes and guest limits. [Verified host limits](../../../../experiments/m4/evidence/verified/vm-environment.json) include the outer QEMU/container caps absent from the guest root cgroup. [Acceptance and source manifest](../../../../experiments/m4/M4-REPORT.md) describe the same final API implementation and its fault tests. Protocol timing is diagnostic; the baseline comparison covers admission latency, completion latency and parent throughput.
