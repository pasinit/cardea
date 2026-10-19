# ADR-015 — M2 accounting, retention and acceptance

Date: 2026-09-24. Owner: kernel/host integration. Status: implemented and verified; M2 acceptance complete for the fixed Debian profile.

## Requirements and milestone boundary

Close M2's R06/R07 and host-admission R09 gates: fixed installed operations, explicit immutable input and task-owned output scopes, live supervisor/platform authority, fail-closed Debian confinement, atomic budget reservations, measured settlement, conservative unknown consumption, and recoverable deletion of completed worker payloads. Debian remains one persistent host for all services and workers. Model/provider accounting is M3; delegated child budgets are M4; reboot/upgrade packaging is M5. Broad ambient host filesystem access and arbitrary plugins are outside the MVP, not prerequisites for closing M2.

## Reuse assessment

Reuse pinned SQLite (public domain), SQLAlchemy/Alembic (MIT), DBOS 3.0.0 (MIT), Casbin 1.43.0 (Apache-2.0), systemd 257.13/bubblewrap 0.12 (LGPL-2.1-or-later), and Python's standard library (PSF). Exact inventories and maintained upstream checks are in ADR-010/012 and the lockfile. No dependency upgrade is selected.

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) already supply the shared writer serialization and atomic receipt/history commit. Add domain records for host-owner budget limits and task-keyed reservations in that same transaction; there is no new transaction engine or scheduler. Existing store/DBOS fit evidence establishes the transaction boundary; concurrency and rollback tests for these new records remain mandatory.
- [limits 5.8.0](https://limits.readthedocs.io/en/stable/) (MIT, maintained project) provides windowed rate limiting with Redis/Memcached/MongoDB backends. Those counters do not join the existing SQLite receipt transaction or represent unknown host consumption. It was documentation-screened, not installed or benchmarked. It may suit API pacing later, but not this cumulative execution ledger.
- [systemd resource controls](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml) supply actual CPU/memory and enforced limits; service monotonic execution timestamps supply elapsed worker time. These do not reserve cumulative supervisor allocations across jobs or atomically settle Cardea receipts. Keep only that domain integration custom.
- [systemd-tmpfiles](https://github.com/systemd/systemd/blob/main/man/tmpfiles.d.xml) supplies age-based cleanup but cannot establish receipt commit or stop confirmation. Age alone must not erase a no-relaunch fence. Reuse Python's [descriptor-based `shutil.rmtree`](https://docs.python.org/3/library/shutil.html#shutil.rmtree) for deletion, and SQLite in the root helper for durable retired-job tombstones. Selection is conditional on symlink-denial, repeated cleanup, crash-window and replay-fence tests. Do not implement a filesystem walker.

## Integration

The installed registry declares `host.sha256` and bounded `host.copy`. Both consume inline JCS or explicitly granted immutable stored bytes, use the same confined profile, and publish only task-owned objects in the authorized destination namespace. Copy returns base64-encoded bytes and byte count, independently checked against input. No caller commands, paths, mounts, plugins or verifier selection are added.

Host owners provision cumulative tool-millisecond/output-byte and concurrent-operation ceilings per authenticated supervisor and namespace. Omitted allocations deny new host work. Provisioning replaces limits without resetting historical charges or reservations. Reserve the full fixed operation maximum before launch in the same commit as intent. Known successful/failed execution settles bounded observed elapsed milliseconds and output bytes; missing measurements and interrupted intent charge the full maximum. Settlement is task-keyed and idempotent, releasing unused reservation without decreasing recorded receipt usage. Policy/limit reductions block subsequent dispatch and are checked during execution. Child host tasks remain rejected.

Cleanup runs after terminal receipt commits and during periodic/startup maintenance. It only processes terminal tasks, confirms the unit/cgroup is stopped, durably records a helper tombstone, then removes bounded staging/output data. A lost cleanup acknowledgment retries safely. Tombstones forbid reuse even after deletion or an older kernel-store restore. Unknown receipts remain unknown; cleanup never manufactures success. Permanent compact receipt/tombstone metadata is retained; bulk history pruning belongs to M5. Cleanup backlog is observable, and the existing 256-payload cap remains fail-closed.

Alembic adds the budget extension without changing v0.1 records or existing request digests. Backup/restore must cover the new tables; restoring an M1 backup requires explicit migration before serving. Old unfinished host work is reconciled without relaunch, with conservative accounting imported during migration.

## Acceptance before completion

Test two installed operations, disclosure/discovery filtering, cross-object/namespace denial, independent verification, supervisor disablement and live revocation, concurrent/exhausted allocations, measured settlement/release, unknown charges, migration/restore and failed migration rollback. Test cleanup failure/retry, active-job denial, symlink refusal, capacity reuse and tombstone replay protection. Run real authenticated Debian scenarios plus hostile production-profile probes for paths, symlinks, network, secrets, descendants, CPU/memory/process/output limits and unsupported-host rejection. Keep earlier evidence immutable and publish a requirement-by-requirement M2 acceptance report. Revisit for new operations/profiles, model accounting or child delegation.

## Executed fit and decision closure

The [M2 acceptance report](../../experiments/m2/M2-REPORT.md) records 221 backend and 4 frontend passes, including 83 portable host/service cases, plus 46 real-provider M1 regressions, 20 authenticated Debian scenarios and 11 hostile production-profile probes. Lint, type checks and experiment checks pass. Concurrency, exhausted allocation, measured release, conservative unknown charges, migration/restore, failed cleanup, lost acknowledgment and tombstone replay tests passed. The existing shared SQLite transaction and standard-library deletion mechanisms meet this profile’s requirements; only the documented domain records and coordination are custom. No new runtime dependency was required.

The [manifest](../../experiments/m2/evidence/completion/manifest.json) pins tested source and evidence hashes; earlier evidence remains immutable. Full-system Debian acceptance uses one emulated persistent host for all services/workers. Production deployment packaging and reboot/disaster-recovery procedures remain M5; provider and transitive child accounting remain M3/M4.
