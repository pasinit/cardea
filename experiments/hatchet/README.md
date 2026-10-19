# Hatchet fit experiment

Isolated R05/M1 probes using `hatchet-sdk==1.41.0`, the official native embedded sidecar **v0.107.0**, and disposable PostgreSQL. This is an observation harness, not a Cardea runtime or authentication implementation. See [ADR-006](../../docs/decisions/006-hatchet-fit.md).

## Reproduce on macOS arm64

Requires Python 3.12, uv, PostgreSQL `initdb`/`pg_ctl` on PATH, and the native release binary. The main engine version screened earlier was 0.107.2; the latest available sidecar at this assessment was 0.107.0. Do not silently substitute versions or architectures.

```sh
mkdir -p /tmp/cardea-hatchet-embedded-0.107.0
curl -fL 'https://github.com/hatchet-dev/hatchet-embedded/releases/download/v0.107.0/hatchet-embedded-sidecar_darwin_arm64' \
  -o /tmp/cardea-hatchet-embedded-0.107.0/sidecar
shasum -a 256 /tmp/cardea-hatchet-embedded-0.107.0/sidecar
# Expected: 3903d6c7057ee2d1bc4d3809981287191e3106a1f3be812ae78f7d77855b98ff
chmod +x /tmp/cardea-hatchet-embedded-0.107.0/sidecar
HATCHET_FIT_SIDECAR=/tmp/cardea-hatchet-embedded-0.107.0/sidecar make hatchet-fit
```

The fixture verifies that exact checksum before execution. It creates a private PostgreSQL cluster with Unix-socket access and UTC timezone. The sidecar uses PostgreSQL messaging, with no RabbitMQ or Docker daemon. It starts gRPC on loopback and its REST service on a temporary TCP port; do not assume the REST listener is loopback-only. Generated tenant credentials remain in a private temporary handshake file, never the evidence. No existing Hatchet service or Cardea database is used.

Workers run in disposable directories with filtered service environment variables. A lifespan marker waits for workflow registration before submission. Public child-task APIs supply persisted step results; read-only SQL observations locate durable wait registrations. The fixture does not mutate engine tables or replace the scheduler. Each case uses a unique namespace and operation key. The initial setup showed that different workflow namespaces alone did not prevent an idempotency-key collision in the shared tenant.

The separate `world.sqlite` records fictional effects, permission revocation and intent controls. An intentional error after a committed effect models a lost result; separate cases hard-kill the actual worker at that boundary. Killing a worker also kills its listener subprocess group. Engine and database teardown run on test exit. Test passes include intentionally unsafe native outcomes and do not mean all Cardea acceptance requirements passed.

```sh
uv run --project apps/api ruff check experiments/hatchet
uv run --project apps/api ruff format --check experiments/hatchet
uv run --project experiments/hatchet --with mypy mypy --strict \
  experiments/hatchet/runtime.py experiments/hatchet/worker.py experiments/hatchet/tests
```

The runtime dependencies are pinned in `uv.lock`; the optional mypy runner is a development check. These macOS results do not establish Debian support, confinement, restore, deployment cost or the full live supervisor/ancestor permission ceiling.
