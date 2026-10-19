# DBOS / Cardea Storage Integration Experiment

Tests DBOS 3.0.0 against the actual Cardea v0.1 SQLite store and lifecycle. This is a trusted, deterministic experiment with fictional identities and effects. It does not install a kernel daemon or change application dependencies.

The fixture deliberately uses one fixed task/workflow ID per disposable store. Scenario switches select fault controls; they are not a public task-admission API.

From the repository root, with Python 3.12 and uv:

```bash
make dbos-integration-fit
uv run --project experiments/dbos-integration --locked pytest experiments/dbos-integration/tests --junitxml=/tmp/cardea-dbos-integration-fit.xml
```

Each case creates private disposable SQLite storage. No PostgreSQL, Docker, model, identity provider or existing service is required. SIGKILL and SIGSTOP/SIGCONT require a Unix host; the recorded run is macOS, not Debian validation. Workers filter inherited DBOS/OTEL configuration, use explicit local database URLs and disable telemetry. Run the parent test process without DBOS Cloud/Conductor configuration.

`adapter.py` borrows a public SQLAlchemy transaction for existing Cardea mutations. Native `enqueue_in_transaction` commits task admission and DBOS enqueue together in one file; native `SQLAlchemyDatasource` commits local artifacts, receipts, checkpoints and saved transaction results together. DBOS owns dispatch/history; Cardea leases guard broker mutations. No second dispatcher or outbox is implemented. Independent-database negative controls demonstrate both admission/enqueue gaps.

`worker.py` exercises deterministic publication and verification, or a fictional provider in a separate SQLite file. Intent without a committed receipt becomes `outcome_unknown` on expired-lease recovery and is not dispatched again. Committed receipts survive replay. Engine `SUCCESS` can coexist with Cardea `failed` when artifact verification fails; clients must inspect Cardea's task outcome.

The authority fixture intersects six boolean controls. It tests fresh checks on dispatch, retry and resume, not authenticated principals, grants or a production policy engine. Distinct-executor takeover uses an explicit public resume after lease expiry; the old process then fails the broker guard. This is not automatic failure detection or remote-effect fencing. Same-executor restart is attempted only after the test proves the prior process died.

Results, limitations, dependencies and the next integration gate: [ADR-008](../../docs/decisions/008-dbos-storage-integration.md). The complete source and durable observations are bound by hashes in its evidence record. These tests remain separate from `make test` because they launch and kill subprocesses.
