# M1 load-test result report

Campaign started: 2026-09-24T00:03:23.214529+00:00.
Campaign ended: 2026-09-24T00:18:48.583977+00:00.
Execution order: stress, repeated reference, independent comparison. All times are UTC.

**Verdict: all executed correctness checks passed.**

Vegeta issued 9,355 measured requests: 7,085 accepted, 2,044 capacity rejections, 0 unexpected HTTP/transport errors. Fault-window denials/unavailability are counted separately in the raw summaries. Warmups and coordinated retry/capacity scenarios are excluded from these totals.


**Main finding:** this setup completed all submissions at 25 requests/second. At 50/second it reached the active-task limit and returned HTTP 429 for about half the attempts. Across the reference and comparison, every admitted task completed and all correctness checks passed. The independent comparison triggered no provisional performance warnings.

The three reference runs admitted 485–491 of 1,000 requests at 50/second; the comparison admitted 493. Median useful completion throughput fell from 24.08/s at an offered 25/s to 20.82/s at 50/s. Reference task-completion p95 rose from 0.32–0.78 seconds to 5.14–5.40 seconds. HTTP response latency alone would understate the queueing cost.

This places the observed overload boundary between the tested 25/s and 50/s points for this workload and host. It is not a production capacity guarantee. Use this reference to detect changes; measure longer runs on native Debian before setting an operating limit. No causal profiler was run, so the report does not attribute the bottleneck to a particular library.

## Reference and independent comparison

Unchanged M1; three reference repetitions and one subsequent comparison. Each rate has a fresh store, four warmup tasks, 20 seconds of traffic, and up to 90 seconds to drain. Payload contains a fixed 1,024-character data string. Latency below covers all HTTP responses, including rejections; accepted-only p95 and task completion latency are also retained in every point summary.

| Offered req/s | Reference HTTP p95 ms, median [range] | Comparison p95 ms | Reference completed/s, median [range] | Comparison completed/s | Comparison accepted / rejected |
|---:|---:|---:|---:|---:|---:|
| 1 | 69.37 [45.65–70.33] | 43.67 | 0.99 [0.99–0.99] | 0.99 | 20 / 0 |
| 5 | 25.01 [24.52–26.20] | 26.03 | 4.94 [4.94–4.95] | 4.94 | 100 / 0 |
| 10 | 15.65 [15.13–16.58] | 15.71 | 9.87 [9.87–9.88] | 9.90 | 200 / 0 |
| 25 | 133.74 [121.20–163.50] | 129.97 | 24.08 [23.94–24.65] | 24.67 | 500 / 0 |
| 50 | 228.28 [200.84–275.78] | 205.70 | 20.82 [20.71–20.89] | 21.38 | 493 / 507 |

Provisional performance warnings (>25% HTTP p95 increase or >20% completion-rate decrease): none.

Completion rate divides successful tasks by submission-through-drain wall time (including result decoding). It is useful campaign throughput, not peak scheduler capacity. Vegeta's rate excludes the last interarrival interval; `requests_per_configured_second` supplies an additional pacing check. These short repetitions and provisional thresholds are an initial reference, not an SLO.

## Adversarial scenarios

| Scenario | Result | Evidence |
|---|---|---|
| retry-storm | PASS | {"conflict_status": 409, "unique_task_ids": 1} |
| waiting-capacity | PASS | {"admitted": 100, "inspection_ms": 7.220874998893123, "overflow_status": 429} |
| receipt-stability | PASS | All pre-crash receipt hashes remained unchanged |
| revocation-ceiling | PASS | {"writes_after_policy_cursor": 0} |
| crash-restart | PASS | {"distinct_credentials_used": 1, "expected_denial": 0, "failures": {}, "fault_unavailable": 32, "states": {"succeeded": 218}} |
| live-revocation | PASS | {"distinct_credentials_used": 2, "expected_denial": 151, "failures": {}, "fault_unavailable": 0, "states": {"succeeded": 49}} |
| provider-outage | PASS | {"distinct_credentials_used": 2, "expected_denial": 43, "failures": {}, "fault_unavailable": 0, "states": {"succeeded": 82}} |
| soak | PASS | {"distinct_credentials_used": 6, "expected_denial": 0, "failures": {}, "fault_unavailable": 0, "states": {"succeeded": 1500}} |

## Environment and limits

Linux x86_64; Python 3.12.14; SQLite 3.46.1. cgroup CPU `200000 100000`, memory `2147483648`, PIDs `512`.

Debian amd64 runs under emulation on this Apple Silicon host. Service, SPIRE, generator and observer share the constrained container. These results support repeatable local comparisons; they do not establish native Debian capacity. Real SPIFFE authentication is exercised; OIDC/Keycloak load is not.

The oracle checks acknowledged task persistence, submission deduplication, one canonical output version, successful publish receipts, independent verification artifacts, SQLite integrity/foreign keys and post-revocation object-write events. It checks stored supervisor bindings but does not add descendant/delegation load scenarios. Receipt stability is compared across the crash. No production application changes were made to improve these numbers.

Unrun: overnight soak, disk-full/slow-I/O faults, native Linux capacity, OIDC load, remote clients and arbitrary agent/model workloads. The five-minute soak cannot establish absence of slow leaks. A CPU limit is a quota, not exclusive cores.

## Resource observations and validation

The five-minute 5/s soak completed 1,500/1,500 tasks with six supervisor credentials observed. Service RSS was 167.4 MiB at the first sample and 175.3 MiB at the last; file descriptors were 44 → 43. SQLite grew from 0.12 to 25.56 MiB as task history accumulated. WAL remained bounded in the sampled interval (peak 4.18 MiB). These observations do not prove absence of longer-term leaks or unbounded history growth.

The independent saved-response audit passed for all 24 measured points: no duplicate acknowledgment for unique submitted requests, missing acknowledged ID, or wrong request scope. The crash included 53 pre-crash receipts, all unchanged after recovery. The retry storm returned one task for 32 identical submissions and HTTP 409 for changed arguments. Saturation produced four waiting workers plus 96 queued tasks; all 100 were subsequently cancelled.

Validation: 13 package tests, package lint/type checks, wheel build/install/CLI/resource checks, 144 backend tests, four frontend tests, and root lint/type checks passed. All 40 original M1 source hashes still match the earlier acceptance evidence. The default M1 application and prior evidence were not modified.

The Docker VM exposed 10 CPUs and approximately 7.65 GiB RAM; other user containers remained active. Reference and comparison used the same final load-image ID and matching environment/harness fingerprints. Stress used the prior image; the only measurement-source difference was stricter baseline/comparison input validation, which stress does not invoke. A package build overlapped part of the soak. Image IDs and these execution details are recorded in [host-and-validation.json](host-and-validation.json).

## Evidence

- `reference/run.json`, `comparison/run.json`, `stress/run.json`: full campaign manifests.
- `baseline.json`: exclusive measured reference; `comparison.json`: matching-point comparison.
- Each load point: `vegeta.json`, compressed original `requests.bin.gz`, normalized `requests.jsonl.gz`, `resources.json`, `audit.json`, `summary.json`.
- Profiles, dependency locks, generator digest, source fingerprints and environment are recorded. Request credentials and private provider state are not retained.

Additional evidence: [baseline](baseline.json), [comparison](comparison.json), [saved-response audit](final-audit.json), [point metrics CSV](points.csv), and [package instructions](../../README.md).
