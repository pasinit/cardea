# cardea-load

An independently installable load-test package for Cardea's authenticated M1 Unix-socket API. It composes Vegeta with the existing real SPIRE/M1 fixtures and a Cardea-specific durable-state oracle. It does not import the production application into its own interpreter or change application dependencies.

Decision and alternatives: [ADR-011](../../docs/decisions/011-m1-load-testing.md). Profile: [m1-v1.json](profiles/m1-v1.json). Results: [2026-09-24 campaign](reports/2026-09-24-m1/REPORT.md).

## Install and validate

```sh
uv sync --locked --project packages/cardea-load
uv run --locked --project packages/cardea-load pytest packages/cardea-load/tests
uv run --locked --project packages/cardea-load ruff check packages/cardea-load
uv run --locked --project packages/cardea-load mypy packages/cardea-load/src packages/cardea-load/tests
uv build --project packages/cardea-load
```

The wheel includes the fixture adapter, profile and dependency lock. `cardea-load --help` exposes `run`, `baseline`, `compare`, and `report`. The target checkout and its API interpreter are explicit inputs; installing this wheel does not install Cardea. Native execution also needs the pinned SPIRE binaries used by `experiments/m1/provider_lab.py` and a verified Vegeta binary.

## Reproduce in disposable Debian

Build the existing pinned [M1 provider image](../../experiments/m1/Dockerfile) as `cardea-m1-acceptance:local`, then:

```sh
packages/cardea-load/build-image.sh
mkdir -p /tmp/cardea-campaign
for kind in reference comparison stress; do
  docker run --rm --platform linux/amd64 --cpus=2 --memory=2g --pids-limit=512 \
    -v /tmp/cardea-campaign:/results cardea-load:m1 run \
    --kind "$kind" --repo /work --vegeta /usr/local/bin/vegeta \
    --profile /work/packages/cardea-load/profiles/m1-v1.json --output "/results/$kind"
done
```

Use the same immutable image for all three runs. `M1_IMAGE` can select a previously built target image when building the load image. The measured source files and dependency locks are fingerprinted; a mutable Docker tag alone is not provenance. Build verifies the upstream Vegeta archive SHA-256. Supply `DOCKER_HOST`/`DOCKER_CONFIG` externally if needed; the script does not alter global Docker settings.

Create the baseline **once**, then compare and render:

```sh
uv run --locked --project packages/cardea-load cardea-load baseline \
  --run /tmp/cardea-campaign/reference/run.json --output /tmp/cardea-campaign/baseline.json
uv run --locked --project packages/cardea-load cardea-load compare \
  --baseline /tmp/cardea-campaign/baseline.json --run /tmp/cardea-campaign/comparison/run.json \
  --output /tmp/cardea-campaign/comparison.json
python packages/cardea-load/tools/audit_results.py /tmp/cardea-campaign
uv run --locked --project packages/cardea-load cardea-load report \
  --campaign /tmp/cardea-campaign --output /tmp/cardea-campaign/REPORT.md
```

Output directories and baseline/report files must be new. Existing results are never overwritten. A run exits 2 when correctness checks fail and still saves its evidence. Unexpected fixture/engine failures leave an incomplete `run.json` when setup progressed far enough; keep those artifacts. Performance warnings remain warnings. A reference containing correctness failures can be preserved but cannot approve a comparison. Different profiles, environment limits, locks or harness fingerprints are incomparable; changed target application source is allowed and recorded.

## What is measured

- Three reference repetitions and one independent comparison at 1, 5, 10, 25 and 50 submitted tasks/s, 20 seconds per point, four warmups, fresh storage per point and up to 90 seconds of drain.
- Real SPIFFE supervisor authentication; private credential refresh every five seconds, supervisor JWT-SVID TTL 120 seconds. Credentials feed Vegeta through a pipe and are never saved in results. The five-minute soak verifies use of multiple credentials.
- HTTP latency, accepted-only latency, pacing, 429 capacity rejection, task completion latency/rate, task failure codes, service RSS/CPU/FD and SQLite/WAL samples. Generator, SPIRE and service share the container's CPU/memory limits.
- A 32-way identical-request race and changed-argument conflict; four waiting workers and 100 active tasks; cancellation under saturation; SIGKILL/restart, live supervisor revocation and SPIRE outage during traffic; a five-minute soak.
- Durable checks: all acknowledged IDs exist, deduplication, exact canonical artifacts, one output version, completed receipts, independent verification artifacts, database integrity, stable pre-crash receipts and no object-write events after the revocation's committed journal cursor.

Global Vegeta percentiles include rejected requests; accepted-only p95 is separate. Completion latency uses persisted task creation/final-update timestamps and standard-library inclusive quantiles. Completion throughput includes submission, response completion, decoding and drain time, excludes warmup and final audit. Resource observation is every 0.5 seconds; short peaks can be missed. Acknowledged-task checks do not assume an HTTP timeout means admission failed: the audit also retains unacknowledged committed submissions.

The baseline is unchanged M1 measured repeatedly, not a fabricated target or a comparison with a different workflow engine. Reference min/median/max and provisional warnings (>25% p95 increase or >20% completion-throughput decrease) expose repeatability. Three short repetitions do not establish a statistical service-level guarantee.

## Scope and safety

Only disposable provider/kernel state is used. The harness is trusted test administration; actual load goes through authenticated HTTP. It cannot grant production agent authority. Cleanup terminates owned processes and deletes private state. Do not point the fixture at a production data directory.

The current results are amd64 Debian under emulation on Apple Silicon, with other host workloads present. They are not native Debian capacity or host-isolation evidence. OIDC load, overnight soak, storage exhaustion/slow-I/O, descendant delegation load, remote clients, arbitrary tools and model execution are unrun. M1 acceptance and M2 confinement remain distinct.

## M3 agent/tool comparison (removed)

The [executed M3 campaign report](reports/2026-09-24-m3/REPORT.md) records the `m3-incidents-v1` contract (ADR-016). Its `agent-run`, `agent-baseline` and `agent-compare` commands were removed with the incident agent-task profile ([ADR-032](../../docs/decisions/032-remove-incident-agent-tasks.md)); the report remains as historical evidence and can be reproduced only at a commit before that removal.

## M4 native A2A delegation profile

`delegation-run`, `delegation-baseline` and `delegation-compare` are a separate `m4-load-1` contract. Each run uses four parents through two closed-loop clients, two child jobs per parent and real SPIRE plus confined Debian hash/copy workers. Reference runs contain two fresh-store repetitions; comparisons contain one. The fixture checks all receipts and independent verification, duplicate admission, object versions and database integrity. It records native A2A discovery/send/get round trips including authentication and transaction time; these timings do not isolate protocol-only overhead. No model calls are made.

```sh
uv run --locked --project packages/cardea-load cardea-load delegation-run \
  --kind reference --repo "$PWD" --api-python "$PWD/apps/api/.venv/bin/python" \
  --output /private/fresh-reference
uv run --locked --project packages/cardea-load cardea-load delegation-baseline \
  --run /private/fresh-reference/run.json --output /private/baseline.json
uv run --locked --project packages/cardea-load cardea-load delegation-run \
  --kind comparison --repo "$PWD" --api-python "$PWD/apps/api/.venv/bin/python" \
  --output /private/fresh-comparison
uv run --locked --project packages/cardea-load cardea-load delegation-compare \
  --baseline /private/baseline.json --run /private/fresh-comparison/run.json \
  --output /private/comparison.json
```

Run these on the prepared Debian host with `SPIRE_BIN` pointing at its installed provider. From the development host, `experiments/m4/load_vm.py --state <owned-vm> --prepare --output <fresh-report-dir>` orchestrates the same independent package. M1/M3 baselines are preserved and are incompatible with this workload. This small comparison is a regression reference, not a saturation or soak result.

The [2026-09-24 M4 report](reports/2026-09-24-m4/REPORT.md) contains the accepted reference/comparison, native round-trip timings and preserved initial timeout failure.
