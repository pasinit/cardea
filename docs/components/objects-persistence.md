# Objects and Persistence

**Reuse decision.** Retain SQLite transactions and evaluate existing backup/migration tools. Do not build an artifact engine until bounded in-database objects demonstrably fail requirements. See [R04](../standards-review-2026-09-22.md#r04--storage--m1m5).

See [Storage](storage.md) for table layout, read/write protocols, durability, and operational plans.

**Purpose.** Preserve task state and versioned artifacts together with their event history.

**Design.** Keep a private SQLite database using WAL, FULL synchronization, numbered migrations, and explicit database identity. Trusted transactions store tasks, leases, receipts, object metadata/bytes, and journal entries. Objects use namespace/resource/version handles and content digests. Writes compare expected versions; bounded object bytes, currently at most one MiB, commit with their metadata.

**Boundary and recovery.** Only trusted service code accesses storage. A failed mutation aborts the transaction even when its exception is caught. Reject unknown schemas and stale revisions. Larger external artifacts need a separate recoverable publication protocol before introduction. Audit is application-append-only, not tamper-proof against administrators.

**Acceptance.** Crash tests preserve acknowledged commits, roll back incomplete writes, and prevent partial object publication or competing revision claims.

**Status.** Implemented in [store.py](../../apps/api/src/cardea/kernel/store.py). M1 adds live authenticated task/namespace access controls. Read auditing and large-artifact storage remain future work.
