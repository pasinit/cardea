# Absurd Fit Experiment

Isolated R05 evaluation of Absurd SQL 0.5.0 and Python SDK 0.5.0 on PostgreSQL. It does not integrate a Cardea runtime, authentication or authorization implementation. SDK dependencies have their own uv lockfile; the unmodified release SQL is vendored with its Apache-2.0 license.

```bash
make absurd-fit
```

Requires Python 3.12, uv, and PostgreSQL `initdb`/`pg_ctl` on PATH. Tested with PostgreSQL 14.18 on macOS arm64. Missing database binaries fail setup rather than silently skipping all cases. The fixture creates a private temporary database cluster listening only on a Unix socket, creates a fresh database per case, and removes the cluster after stopping test workers. It does not connect to existing PostgreSQL services. Pytest retains worker logs and fictional provider state under its normal temporary-directory retention policy.

To record evidence:

```bash
uv run --project experiments/absurd --locked pytest experiments/absurd/tests --junitxml=/tmp/cardea-absurd-fit.xml
```

The tests use the synchronous SDK's public worker with distinct worker identities, one concurrent task, a three-second claim, 50 ms polling and fixed 100 ms retry delay. Lease-expiry probes suspend the old process and let PostgreSQL reclaim work normally. No fake clock, modified scheduler, private SDK methods or manual engine-state writes are used. Read-only table queries place faults and capture run/checkpoint observations. Tests using explicit `retry_task` exercise the native operator API; they do not approve reopening terminal Cardea tasks.

Events are persistent names with first-write-wins payloads. Separate names model distinct events; repeated waits on one name return that same payload. Child-result waits use a second queue and worker because the pinned Python SDK rejects waiting on the same queue. These behaviors need a deliberate mapping to Cardea events and dependencies.

The external provider is fictional and uses a separate SQLite database. Tiny intent, fresh-authority and generation checks are test controls, not production security or reconciliation designs. No supervisor authentication, ancestor ceiling, process confinement, real provider or Cardea store is involved.

**Passing tests include unsafe engine-only behavior.** Green records the observed behavior, including stale cached permission checks and repeated external effects; it does not establish full Cardea conformance. This first profile covers the synchronous SDK with ordinary, unpartitioned queues. Async workers, partitioning, clock skew, retention, upgrades, backup/restore and Debian operations remain untested.

Results: [ADR-005](../../docs/decisions/005-absurd-fit.md). Comparison: [ADR-002](../../docs/decisions/002-workflow-engine-comparison.md).

## Vendored Source

`vendor/absurd-0.5.0.sql` is byte-for-byte the [0.5.0 release asset](https://github.com/earendil-works/absurd/releases/download/0.5.0/absurd.sql), matching the [tagged source](https://github.com/earendil-works/absurd/blob/0.5.0/sql/absurd.sql). SHA-256: `d34309370c539f3a51f2b36b69b1f77551f8e4a14480a1c8def8bb8f40fd9aab`. The fixture verifies this hash before installation. License: [upstream Apache-2.0](vendor/LICENSE).
