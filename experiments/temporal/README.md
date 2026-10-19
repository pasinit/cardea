# Temporal Fit Experiment

Isolated R05 evaluation using Temporal Python SDK 1.33.0, CLI 1.9.1 and its embedded server 1.32.0. This is test instrumentation, not Cardea's runtime or authorization layer. Dependencies are locked separately from the application.

## Run

Requires Python 3.12, uv and the pinned Temporal CLI. Install the appropriate CLI artifact from the [official 1.9.1 release](https://github.com/temporalio/cli/releases/tag/v1.9.1), verifying its SHA-256 against the release's `checksums.txt`. The tested macOS arm64 archive is `temporal_cli_1.9.1_darwin_arm64.tar.gz`, SHA-256 `41e0425378fcb4fb5766340b97435e20fe47bbff2d7bf644ec2d51f7662b7c56`.

```bash
TEMPORAL_CLI=/absolute/path/to/temporal make temporal-fit
```

Alternatively, put that exact CLI on PATH. Missing or mismatched CLI versions fail setup rather than silently skipping the experiment. The harness starts its own headless server on loopback with an ephemeral port and a temporary file-backed SQLite database. It never connects to an existing Temporal service. Worker processes have distinct identities and are killed at test fault points and during teardown; the test server is stopped afterward. Pytest retains temporary fictional data and logs for diagnosis under its normal temporary-directory retention policy.

To capture evidence:

```bash
TEMPORAL_CLI=/absolute/path/to/temporal uv run --project experiments/temporal --locked pytest experiments/temporal/tests --junitxml=/tmp/cardea-temporal-fit.xml
```

The development server is deliberately a semantics-testing profile. It does not validate production deployment, PostgreSQL, upgrades, multi-host failure, authentication, or Debian isolation. Time is real; no time-skipping test server is used.

## What Is Tested

Completed-activity and service restart, signals around waits, explicit RPC request-ID deduplication, durable timers and child success, external-commit/result gaps, revoked cached authority, fresh authority checks, retry, workflow ID policies, cancellation, and queued execution deadlines. Distinct-worker overlap uses a real server-side three-second activity start-to-close timeout while the old worker is suspended; it does not reuse a live executor identity. This differs from the DBOS forced-replacement fixture. Workers have one activity slot, one activity poller and two workflow pollers; workflow-task timeout is two seconds and sticky-queue timeout is one second, to keep fault detection bounded. These are fixture settings, not production tuning. The default threaded-activity interruption is tested separately from the explicit `no_thread_cancel_exception=True` option; the latter is not the default or a simulation of every remote I/O behavior.

The fake external system is a separate SQLite database. Intent, authority and owner-generation checks are test controls: they do not implement Cardea authentication, the supervisor/ancestor ceiling, real provider fencing, or reconciliation. Temporal workflows use the default deterministic-workflow sandbox; that sandbox is not host security confinement.

**Passing observations include unsafe behavior.** A green suite does not mean Temporal satisfies Cardea's complete acceptance criteria. It records what happened, including repeated effects, stale authorization, and ID policies that require an explicit Cardea mapping.

See [ADR-004](../../docs/decisions/004-temporal-fit.md) for measured results and comparison with [DBOS](../../docs/decisions/003-dbos-fit.md).
