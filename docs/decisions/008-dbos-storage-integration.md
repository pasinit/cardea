# ADR-008 — DBOS and Cardea Storage Integration

Date: 2026-09-23. Owner: kernel/runtime. Scope: R04/R05, bounded M1 experiment following [ADR-007](007-runtime-integration-direction.md).

**Status: bounded integration implemented and locally verified. Select shared-store native transactions as the integration direction; production adoption/migration remain open. R05 and M1 are not complete.**

## Requirements and Reuse Choice Before Implementation

Preserve Cardea v0.1 tasks, operation digests, receipts, journal, leases and verifier rules. Test atomic admission/enqueue, datasource result commits, interruption during broker effects, terminal publication and recovery ownership. Use a disposable instance of the actual Cardea schema and existing `KernelTransaction` operations. No authenticated client, real agent or host execution is introduced.

Pinned DBOS 3.0.0 (MIT; release and maintenance evidence in ADR-002/003) exposes `DBOSClient.enqueue_in_transaction(Connection | Session, ...)`; its source explicitly requires the transaction to target the DBOS system database. Its public `SQLAlchemyDatasource` records datasource results in the same application transaction, then records the enclosing step separately. SQLAlchemy's public connection/driver-connection interfaces can lend that transaction to Cardea's existing SQLite mutation code. Source inspection: installed `_client.py`, `_datasource.py`, `_datasource_sqlite.py`, `_recovery.py`; record hashes with final evidence.

Candidates:

- **Shared SQLite database + native transactional enqueue/datasource:** selected for this experiment. Initialize Cardea first, add DBOS-owned tables only in a disposable database, preserve Cardea schema identity/version and existing data. DBOS owns transaction commit; the adapter must not open or commit a second Cardea transaction.
- **Separate databases + transactional outbox:** valid alternative if shared persistence fails. Independent commits cannot provide admission/enqueue atomicity. First demonstrate that gap; do not build an outbox while a supported same-transaction API may meet the requirement.
- **Temporal/Absurd/Hatchet:** retain the previous measured alternatives. Do not switch engines merely because a datasource needs a small transaction adapter.

Primary sources: [DBOS client](https://docs.dbos.dev/python/reference/client), [datasources](https://docs.dbos.dev/python/tutorials/transaction-tutorial), [pinned source](https://github.com/dbos-inc/dbos-transact-py/tree/3.0.0), [SQLAlchemy SQLite transaction control](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl), [recovery ownership](https://docs.dbos.dev/production/workflow-recovery), [transactional outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html). Current docs guide discovery; pinned source and executed tests determine claims.

The smallest custom scope is experiment-only transaction borrowing, mapping Cardea task/operation IDs to stable workflow IDs, fictional authority/effect controls and fault injection. Use standard SQLAlchemy connection events to establish real SQLite `BEGIN IMMEDIATE`, foreign keys, WAL and FULL synchronous behavior. Preserve mutation-failure rollback and invalidate borrowed transactions after use. No copied lifecycle engine, custom scheduler, or production outbox.

## Ownership to Test

| Responsibility | Owner in experiment |
|---|---|
| Admission and changed-request conflicts | Cardea contract validation and SQLite transaction, with native DBOS enqueue in that transaction |
| Queue dispatch and workflow history/replay | DBOS only |
| Broker mutation ownership | Existing Cardea lease/revision checks; these are effect guards, not another runnable queue |
| Safe local artifact/receipt/checkpoint publication | Cardea mutations inside a DBOS datasource transaction |
| External-effect intent, uncertainty, result receipt | Cardea broker fixture using existing receipt semantics; external provider remains a separate database |
| Terminal success | Installed deterministic verifier reads actual artifact bytes; Cardea lifecycle validates its evidence |
| Restart | First prove process death, then reuse the configured stable executor ID for documented local startup recovery |
| Distinct-executor takeover | Explicit public resume after fencing; not automatic failure detection or a production supervisor |

Fresh fictional authority must be checked before each new effect, including resume and retry. It is not a replay-cached permission step and does not implement authenticated supervisor/ancestor enforcement. Cardea terminal states never reopen even if an operator resumes an engine workflow. Engine completion and verified Cardea success are different observations.

## Acceptance

Inject process death before/after admission, enqueue, datasource commit, external intent/effect/receipt, and terminal publication. Check actual durable rows and committed effects, not only workflow status. Reject changed request digests, bad artifacts and revoked authority. Suspend an old worker, expire/fence its lease, complete through a distinct executor, then verify old broker writes/effects are denied. Report unsupported automatic failover, host termination, restore and production migration separately.

## Results and Decision

The experiment reuses the actual `cardea.kernel` modules without modifying them. **28 cases passed** on macOS arm64 / Python 3.12.0, DBOS 3.0.0 and SQLite 3.42.0. [Machine-readable evidence](dbos-integration-evidence-2026-09-23.json) records dependency versions, source hashes, case outcomes and durable observations; [reproduction instructions](../../experiments/dbos-integration/README.md) describe the isolated environment. These are bounded integration observations, not 28 completed production acceptance requirements.

Regression checks also pass: 138 backend and 4 frontend tests, root lint/type checks, and isolated Ruff formatting/lint plus strict mypy for all five experiment source files. Existing experiment evidence hashes remain valid. The JUnit export emitted two property-format warnings; all 28 cases were recorded, including the negative controls' observation properties.

| Boundary | Observed result |
|---|---|
| Shared admission/enqueue, four kill points | Before commit both disappear; after commit both survive. Retry produces one task and workflow. |
| Independent-database controls, both commit orders | A crash leaves either a Cardea task without a workflow or a workflow without a Cardea task. |
| Datasource publication before/after commit | Artifact, receipt, checkpoint and datasource result roll back or survive together; recovery creates one output version. |
| Terminal publication before/after commit | Recovery preserves one verification artifact and a matching successful Cardea task. |
| External intent/effect/receipt boundaries | Missing receipt becomes `outcome_unknown`, with task failure pending reconciliation; no blind repeat. A committed receipt is reused even if the engine step was not recorded. |
| Duplicate and changed requests | Same request returns existing admission/receipt; changed arguments fail with `request_conflict`. Existing v0.1 digest rules are reused. |
| Incorrect output bytes | Verifier fails the Cardea task even when DBOS reports workflow `SUCCESS`. |
| Fresh fictional authority | Six separate ceiling denials on resume and additional new-dispatch/retry denials produce no new operation or provider effect. |
| Ownership takeover | After lease expiry, explicit public resume lets executor B finish; resumed executor A is denied as stale. The terminal Cardea state and DBOS success remain intact. |
| Cancellation | Explicit engine resume cannot reopen an already cancelled Cardea task. Running host-process cancellation is untested. |
| Borrowed transaction failure | A caught failed mutation still aborts the caller transaction; a borrowed transaction cannot be reused after exit. |
| Existing-store compatibility | DBOS bootstrap preserves a pre-existing completed demo task, journal, readable artifacts, Cardea application ID and storage version. |

**Choose supported transaction composition for the next production design.** For the initial single-file shape, `enqueue_in_transaction` removes the need for an outbox; `SQLAlchemyDatasource` closes the local publication/replay gap. The adapter translates existing Cardea mutations into the caller-owned transaction and supplies uncached broker guards. Its custom scope is justified by the existing v0.1 contract, not a missing workflow scheduler. Split persistence would reopen the outbox decision.

`DBOS.launch()` initializes DBOS tables before clients enqueue; constructing a `DBOSClient` alone does not perform migrations. The experiment lends the actual driver connection using public SQLAlchemy interfaces, verifies a real transaction is active, and never commits inside the Cardea adapter. The owning transaction controls commit. DBOS owns runnable work and replay; Cardea retains inspectable task state, operation receipts, journal and broker lease checks. Wait/message projection is not integrated in this experiment.

The lockfile additionally pins SQLAlchemy 2.0.54 and Pydantic 2.13.5 (MIT), using Python's SQLite driver (SQLite is public domain). DBOS license/release maintenance evidence is inherited from the immediately preceding pinned assessment; this run adds an exact dependency/source inventory. No transitive vulnerability audit or Debian operational measurement is claimed. Production kernel dependencies are unchanged.

## Remaining Gates and Next Work

- **Identity and authority:** perform the M1 SPIRE Workload API/bootstrap experiment next, including trust domain, audience, expiry, rotation and unavailable-provider rejection. Establish authenticated workload/task/supervisor bindings before exposing this adapter. The six booleans are fictional; admission/grant authorization, transitive policy graphs and in-flight revocation are not implemented here.
- **Schema and operations:** write and test a coordinated DBOS/Cardea migration, backup/restore, retention and upgrade policy before production integration. Preserving one existing v0.1 store does not prove rolling upgrade or restore compatibility. Shared persistence couples engine-history retention to Cardea receipts. Reassess SQLite's supported single-host profile and upstream PostgreSQL production recommendation.
- **Recovery ownership:** same-executor recovery requires confirmed prior process death. Distinct-executor recovery here requires explicit administrative resume after lease expiry; no automatic failure detector or distributed recovery service was selected. A production supervisor must establish ownership safely and test failure/restart on Debian. No private DBOS recovery API is used.
- **Effects and terminal mapping:** no reconciliation adapter for unknown provider outcomes exists. Remote providers cannot participate in the SQLite transaction, and lease checks cannot undo an effect already issued. Map generic engine failures and in-flight revocation to conservative Cardea outcomes before production. The fixture's short lease has no heartbeat/renewal implementation.
- **Remaining runtime conformance:** authenticated submit/inspect/cancel over the selected Unix-socket transport, multi-task concurrency, event/wait races and projection, deadlines, budget accounting and process-tree cancellation remain separate work. M2 host confinement still precedes real agent tools.

Revisit this choice if required DBOS upgrades cannot share transactions safely, independent persistence becomes necessary, or measured ownership/operational needs exceed this single-host shape. Prefer the documented maintained alternatives or a standard outbox over a second custom execution engine.
