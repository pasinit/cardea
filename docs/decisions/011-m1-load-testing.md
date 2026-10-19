# ADR-011 — Separate M1 load-test package and reference baseline

Date: 2026-09-24. Owner: kernel validation. Scope: M1 capacity/reliability measurements, not a new kernel capability.

**Status: implemented and verified for the recorded emulated Debian M1 campaign; native capacity remains unverified.**

## Requirements

Provide an independently installable `cardea-load` package, with its own dependencies and CLI. Exercise the existing authenticated Unix-socket API, retain reproducible raw measurements and durable correctness checks, create an immutable measured baseline, compare a subsequent run, and produce a result report. Keep application sources and previous acceptance evidence unchanged. Credentials and disposable provider state must not enter results. The user explicitly requested a separate package; this is not a premature shared application abstraction.

## Candidates and choice

- **Vegeta 12.13.0**, MIT, upstream release 2025-10-31, non-archived repository last pushed 2026-02-16 (checked 2026-09-24). Native Unix-socket support, fixed-rate pacing, bounded worker counts, streamed JSON targets, binary results and JSON latency reports meet HTTP load requirements. Select the verified release, contingent on actual socket/authentication fit. Reuse its scheduler and latency reporting; do not write another HTTP load generator.
- **Locust 2.46.6** documentation supports custom protocol clients and user journeys. It would require a custom HTTPX/Unix adapter and compatibility checks for gevent plus the SPIFFE gRPC client. Keep as a candidate for richer user journeys; not generally rejected. No Locust fit experiment is claimed.
- **Existing pytest/M1 harness + HTTPX** already exercises real SPIRE, shared-store recovery and authority. Reuse its disposable fixtures through a thin out-of-process adapter and use bounded standard-library thread pools only for coordinated race scenarios. A throughput-only HTTP tool cannot interpret Cardea's receipts, supervisor ceiling or durable task state; that application-specific oracle is the remaining custom scope.
- **psutil** provides process/resource sampling; **statistics/json/hashlib/argparse** provide simple aggregation, manifests and CLI composition. Use standard JSON for portable result/baseline files. No custom histogram, credential issuer, kernel dispatcher or synthetic authentication shortcut.

Primary sources: [Vegeta](https://github.com/tsenart/vegeta), [release](https://github.com/tsenart/vegeta/releases/tag/v12.13.0), [Locust custom clients](https://docs.locust.io/en/stable/testing-other-systems.html), [HTTPX transports](https://www.python-httpx.org/advanced/transports/), [psutil](https://psutil.readthedocs.io/). Record exact dependency locks, binary digests and maintenance/license observations with the package.

## Frozen measurement design

Use unchanged M1 as the reference implementation, identified by source hashes rather than Git HEAD alone (the workspace includes uncommitted implementation). Performance comparison requires the same workload profile, generator version, OS/architecture, resource limits and Python/dependency environment. Different application source is permitted and identified explicitly; different environments/profiles are **incomparable**, not regressions. Baseline creation is explicit and exclusive; comparison never overwrites a reference.

Default reference: three repetitions of authenticated task submission at 1, 5, 10, 25 and 50 requests/second, 20 seconds per measured window, fixed small JSON payload, fresh kernel store per point, warmed-up service, actual credential renewal and post-window drain/audit. Record request latency separately from completion/drain time and achieved pacing separately from requested rate. A subsequent single repetition of those same points compares against baseline medians/ranges. Three samples and short windows establish an initial reference, not a statistical service-level guarantee. Low-load 1 request/second is also an internal control; overload points are not classified as regressions merely because they are slower than that control.

Additional bounded scenarios: waiting-task/100-task saturation; retry storms and argument conflicts; crash/restart under traffic; supervisor revocation under traffic; real provider outage; and a five-minute steady-load soak. Baseline failure to achieve the requested rate, unexpected errors, or durable invariant violations remains recorded, never silently discarded. Longer overnight soak, storage exhaustion/slow I/O, native Debian capacity and multi-host tests are separate unrun follow-ups.

Correctness gates: no missing acknowledged task, duplicate artifact publication, false verified success, receipt corruption, post-revocation authorized effect, or database integrity/foreign-key failure. Failed tasks remain failures even if HTTP admission succeeded. Expected rejection (429 capacity, deliberate authorization/provider denial) is categorized separately from unexpected HTTP/server failures. Performance gates are provisional warning thresholds: >25% p95 latency increase or >20% completion-throughput decrease against the median matching point, also report baseline spread and repeatability; correctness failures are unconditional failures.

## Execution and ownership

Run in disposable Debian 13 amd64 Docker state using the existing pinned M1 provider image, with explicit 2 CPU / 2 GiB / 512 PID limits. This host executes amd64 through emulation; publish that limitation and generator/provider co-location. Do not claim native Debian performance. Sample service RSS/CPU/file descriptors, database/WAL growth and host/cgroup context. The harness is trusted test administration, not another agent authority path. It may inspect the disposable database and change fixture policy, but all load traffic uses real authenticated HTTP. Private short-lived credentials travel only through private files/pipes.

## Acceptance

Before the campaign: test metric classification, failed/mismatched baselines, exclusive baseline creation, token renewal, generator/socket fit and the durable oracle. Smoke runs are harness validation and excluded from the reference. Then freeze the profile, run the reference, independent comparison and adversarial scenarios, retain all outcomes, and write a report with measured limits and unresolved cases. Revisit tool selection if generator saturation, credential buffering or Unix transport behavior prevents valid measurements.

## Dependency and distribution evidence

Direct Python dependencies are locked separately from the API: [HTTPX 0.28.1](https://pypi.org/project/httpx/0.28.1/) (BSD-3-Clause), [psutil 7.2.2](https://pypi.org/project/psutil/7.2.2/) (BSD-3-Clause), and [rfc8785 0.1.4](https://pypi.org/project/rfc8785/0.1.4/) (Apache-2.0). Registry pages, installed package metadata and lock resolution were checked on 2026-09-24. HTTPX/RFC 8785 already match the application selections; no replacement transport or canonicalization implementation is introduced. This is a license/maintenance/fit check, not a comprehensive vulnerability audit. Build and development dependencies are also captured in the package's own `uv.lock`.

Vegeta release archives match the GitHub release API's SHA-256 asset digests: Darwin arm64 `fc408e242c4f4839e6fe536dbf1130bb02f430134827f6d831bf367a0929a799`; Linux amd64 `e8759ce45c14e18374bdccd3ba6068197bc3a9f9b7e484db3837f701b9d12e61`. Native and emulated Debian five-request authenticated smoke runs each completed five verified tasks. Smoke evidence is excluded from the reference. The wheel builds and installs independently and includes its fixture, profile and lock. Application source hashes still match all 40 files in the existing M1 acceptance manifest.

The Locust candidate's [2.46.6 release](https://github.com/locustio/locust/releases/tag/2.46.6), published 2026-09-17, was confirmed during the campaign. Vegeta was chosen for its direct Unix-socket fit and reusable standalone CLI, not because the alternative was outdated.


## Executed result (2026-09-24)

[Report](../../packages/cardea-load/reports/2026-09-24-m1/REPORT.md), [immutable baseline](../../packages/cardea-load/reports/2026-09-24-m1/baseline.json), and [comparison](../../packages/cardea-load/reports/2026-09-24-m1/comparison.json) are retained with raw compressed Vegeta results, point audits, resource samples and source/environment manifests. The package is independently installable and its final source matches the reference/comparison harness fingerprint.

All 24 measured points and four additional scenario checks passed. There were 9,355 measured requests: 7,085 admitted and completed, 2,044 capacity rejections, 194 intentional authentication/authority denials, and 32 crash-window transport failures. No unexpected errors, missing acknowledged tasks, duplicate publications, false successful verification, changed pre-crash receipts, post-revocation writes or database integrity failures were observed. A separate saved-response audit also passed. No JWT-shaped credentials were found in the retained artifacts.

At 25 submissions/s all tasks completed; at 50/s the 100-active-task bound rejected about half the attempts. The reference median completion rates were 24.08/s and 20.82/s respectively, including drain/measurement overhead. The independent comparison triggered no provisional performance warnings. The five-minute 5/s soak completed 1,500 tasks and exercised six credentials; it is not overnight or leak-proof evidence.

Package validation: 13 tests, lint, strict type checks, wheel build/install/resource checks. Existing checks: 144 backend and four frontend tests, root lint/type checks. All 40 original M1 acceptance source hashes remain unchanged. Execution used Debian 13.7/Python 3.12.14/SQLite 3.46.1 under amd64 emulation with 2 CPU / 2 GiB / 512 PID limits. Native macOS and Debian smoke checks were excluded from the baseline. The stress phase ran before the performance phases so credential renewal was proven first; only baseline/comparison-input validation changed between the stress and final image, not the stress engine or oracle. See the host manifest for image IDs, non-exclusive host workload and a build overlapping the soak.

Remaining work: native Debian repetition, longer windows/soak, storage faults, OIDC load, descendant delegation under load, profiling before attributing the throughput limit, and later arbitrary-tool/model workloads. These do not change M1's bounded scope or imply M2 host confinement.
