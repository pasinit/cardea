# Storage

**Reuse decision.** Reuse SQLite transaction and backup facilities; evaluate maintained migration tooling and record compatibility before changing stored records. See [R04](../standards-review-2026-09-22.md#r04--storage--m1m5).

**Purpose.** Preserve acknowledged progress, artifacts, and evidence across process restarts. The kernel service exclusively owns storage; clients use authorized operations.

**Implemented layout.** One private `kernel.sqlite3` database stores bounded artifact bytes alongside metadata. Its directory is created with mode 0700 and database with mode 0600; existing paths must be private and owned by the current OS user.

| Table | Stored data |
|---|---|
| `tasks` | Task snapshot, revision, state, embedded checkpoint and lease |
| `dispatch_leases` | Unique lease IDs and associated tasks |
| `operations` | Versioned receipts, request identity/digest, execution state |
| `objects` | Namespace, resource ID, immutable version, bytes, content digest |
| `events` | Ordered cursor, event identity, scope, correlation, payload |

**Write/read protocol.** Trusted transactions compare task revisions or expected object versions, validate worker leases, and commit mutations with journal events. Objects are capped at one MiB; reads resolve explicit versions and check their digest. M1 handlers enforce current namespace/task grants on reads and writes. Read access is not appended to the transactional mutation journal. A resource handle alone grants no access.

M2 [object-scoped host inputs](../decisions/014-object-scoped-host-inputs.md) reuse these exact versions and integrity checks with a 16 KiB worker-input bound. Both source-namespace and exact-version read/delegation policies must permit staging. The source reference is persisted in the task request and operation intent. Exact-version ACLs use tagged canonical keys in the existing `policies.namespace` column; this source-ACL extension preserves existing table and backup ownership.

**Durability and recovery.** SQLite WAL/FULL transactions and `BEGIN IMMEDIATE` protect local commits. Failed mutations abort the transaction. Recovery fences expired workers and preserves uncertain effects for reconciliation. External execution is outside the database transaction. Larger file-backed artifacts require a future recoverable publication protocol.

**Integration experiment.** [ADR-008](../decisions/008-dbos-storage-integration.md) adds DBOS tables to a disposable Cardea database and uses native transactional enqueue/datasources. Admission/enqueue and local artifact/receipt/checkpoint publication survive process-kill tests atomically. Existing v0.1 data and schema identity are preserved. The experiment preserved its historical evidence; [ADR-010](../decisions/010-m1-authenticated-kernel.md) implements Alembic provenance migrations and whole-store backup/restore for the installed service.

**Operations.** Alembic adds `principals`, `policies`, immutable `task_bindings`, `control_requests` and `service_meta` without rewriting v0.1 records. DBOS owns its native tables and migrations. Initialization/first runtime launch retain whole-store snapshots; unsupported versions fail closed. Stopped-service SQLite backup/restore covers both owners. M2 migration `m2_001` adds `host_budget_limits` and task-keyed `host_reservations`, imports historical host intents conservatively, and preserves receipt bytes. M5 restore accepts supported M1–M5 snapshots and migrates private staging before publication. It validates artifact digests/references and seals historical execution using `recovery_epochs`; all restored principals are disabled and policies removed. Fresh provisioning, namespaces and budgets are required. Terminal worker payloads are retired with durable helper tombstones; audit/object history is retained for the store lifetime. Explicit generated-backup pruning excludes live history and host tombstones. See the [operating guide](../operations.md). Monitor capacity. Raw credentials never belong in task or event records. Host administrators remain trusted.

**Acceptance/status.** The [store](../../apps/api/src/cardea/kernel/store.py) has portable crash, revision, lease, and publication tests. Authenticated task-scoped access, migration rollback, exclusive service ownership and shared-store restore are tested. M2 tests add transactional host reservations, measured/unknown settlement, allocation replacement, migration rollback and old-snapshot restore. M3 adds `m3_001` model allocations, durable effect reservations, round checkpoints and idempotent input deliveries, with migration rollback/old-backup tests. M4 adds `m4_001`: immutable parent/slot/child/message associations committed with admission and DBOS enqueue. A2A reads the existing task, artifact and receipt state; no second lifecycle store is maintained. Parent failure decisions awaiting worker stop use the existing control/event journal. See [persistence decisions](../runtime-decisions.md).

**Concurrent inspection.** ADR-017 separates read-only API/A2A snapshots from writer reservations. SQLAlchemy connections opened with SQLite `mode=ro` use `BEGIN` against the same WAL database; policy and task/artifact data share one short snapshot. Each request starts fresh, so committed revocations apply to subsequent reads. Admission, cancellation, budgets and effect publication keep `BEGIN IMMEDIATE`. Read connections cannot write, and no duplicate database or authority cache is introduced.
