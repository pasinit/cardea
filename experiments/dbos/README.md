# DBOS Fit Experiment

A reproducible evaluation of DBOS 3.0.0 for Cardea's R05 decision. This is test code, not the kernel runtime or a production broker. Dependencies are isolated in this directory's locked uv project; the application dependency set is unchanged.

From the repository root:

```bash
make dbos-fit
```

Requires Python 3.12, uv, and PostgreSQL `initdb`/`pg_ctl` on PATH for both profiles. The test creates a disposable private PostgreSQL cluster under `/tmp`, listening only on its private Unix socket, and destroys it afterward. It does not connect to an existing PostgreSQL service. No Docker or identity provider is needed. On a machine without PostgreSQL binaries, PostgreSQL cases are explicitly skipped; this does not establish PostgreSQL compatibility.

Run only SQLite, or record JUnit evidence:

```bash
uv run --project experiments/dbos --locked pytest experiments/dbos/tests -k sqlite
uv run --project experiments/dbos --locked pytest experiments/dbos/tests --junitxml=/tmp/cardea-dbos-fit.xml
```

Each case uses fresh workflow storage plus a separate SQLite database as a fictional external system. Workers run in subprocesses; tests use SIGKILL for crashes and SIGSTOP/SIGCONT for an unfenced replacement probe. The same executor identity is intentionally reused for restart. The replacement probe is not a supported two-live-executor setup or a natural lease-expiry test. PostgreSQL uses DBOS polling rather than LISTEN/NOTIFY in this first comparison, matching the SQLite profile; the notification-based path is untested.

The fake external system counts committed effects rather than relying on log lines. Small fixtures demonstrate three responsibilities that do not belong to cached workflow decisions: a durable intent before an uncertain effect, a fresh revocation check, and a current owner generation. Those checks use SQLite transactions and fictional local data. They are test controls, not new authorization, fencing, or reconciliation implementations. They neither authenticate callers nor model the full supervisor/ancestor permission intersection.

**A green suite includes assertions of unsafe raw behavior.** Tests deliberately record duplicated effects, stale cached authority, and accepted changed arguments as measured integration gaps. Green means observations match the pinned experiment, not that DBOS alone satisfies Cardea's contract. The suite also verifies restart recovery, message ordering/deduplication, timers, child success, and duplicate execution handling at step boundaries.

Results and acceptance limits: [DBOS fit report](../../docs/decisions/003-dbos-fit.md). Candidate comparison: [ADR-002](../../docs/decisions/002-workflow-engine-comparison.md).
