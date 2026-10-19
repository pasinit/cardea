# ADR-019 — M5 operational recovery and maintenance

Date: 2026-09-25. Owner: runtime/operations. Status: implemented and verified for the supported Debian profile; see the [M5 report](../../experiments/m5/M5-REPORT.md). Scope: complete M5 for the existing single-Debian-host profiles, following ADR-018.

## Requirements and candidates

Recovery must not revive old authority, refund previously spent budgets or replay uncertain effects. Upgrade failure must preserve state; unsupported schema/runtime versions must fail closed. Support diagnostics must exclude credentials and workload contents. Retention must preserve provenance, idempotency fences and live state.

| Reuse candidate | Fit, license/maintenance and choice |
| --- | --- |
| SQLite backup API and integrity/foreign-key checks | Selected public-domain engine, shipped by maintained Python/Debian. [Backup API](https://www.sqlite.org/backup.html) captures the whole shared database. [VACUUM INTO](https://www.sqlite.org/lang_vacuum.html) is also supported upstream, but compaction does not solve historical authority or external effects. Reuse backup; add only application artifact/reference checks. |
| SQLAlchemy 2.0.54 / Alembic 1.20.0 | Existing maintained MIT dependencies, already fitted transactional migrations. Reuse for the M4-to-M5 extension, rollback and explicit upgrade checks; do not write a migration engine. |
| DBOS 3.0.0 | Existing pinned MIT workflow owner. [Workflow management](https://docs.dbos.dev/python/tutorials/workflow-management) supplies recovery/cancel/fork facilities. Native recovery remains correct for a restart of the current store. Fork/resume from an old snapshot cannot establish current authority or whether later effects occurred; do not use it to replay restored work. No second scheduler. |
| Debian dpkg/debhelper/systemd | ADR-018 selected versions/licenses apply. Reuse package lifecycle, private service ownership and journals. Test package revision 1 to 2, refusal of the old package on the new schema, failed migration and recovery to the supported package. No promise about arbitrary future versions. |
| journald, logrotate and OpenTelemetry | [journald retention](https://manpages.debian.org/trixie/systemd/journald.conf.5.en.html) handles operational logs; logrotate is for ordinary files, not a SQLite/WAL history. OpenTelemetry adds an exporter but does not make payloads safe to disclose. Use a small explicit metadata projection for support output; never copy raw logs/configuration. Keep host-wide log policy under the host owner's control. |
| Python stdlib filesystem/JSON/hash facilities | Reuse maintained PSF-licensed Python for private descriptor checks, staging, fsync and bounded generated-backup pruning. No custom encryption, archive format or secure-delete claim. |

## Recovery choice and exact remaining gap

Current-store crash/reboot recovery preserves normal task/wait semantics. Snapshot restore is a different operation: copy into private staging, verify database identity, supported runtime/schema, model snapshots and every object digest/reference, migrate staging, then atomically publish. Never modify the live database on validation/migration failure. Refuse a live service, unsafe file/symlink, regressing clock or still-live copied worker lease. Reuse existing lease recovery and task/operation transitions to fail unfinished restored tasks conservatively; running effects become `outcome_unknown`, never successful or automatically replayed. Completed receipts/results remain unchanged.

Each restore appends a UUID recovery epoch. All restored execution scopes become read-only. New effectful operations require a namespace beginning with the fresh epoch prefix reported by maintenance diagnostics. This is necessary even for scopes absent from the snapshot: they might have consumed capacity after it. Clear all old policy and disable restored principals; the host owner must explicitly provision current identities/permissions and fresh scoped budgets. Immutable principal and task bindings remain enforced. The generation check applies at the existing transactional policy boundary and cannot be overridden by broader grants, service credentials or budget increases. Reads still require fresh supervisor/ancestor/platform authority. This small domain fence is the gap neither SQLite backup nor workflow replay can cover; it is not a new identity or scheduling protocol.

Restoration retains the host's reconciliation/tombstone storage and never rolls it back. After total-host loss, any real external effects still require external reconciliation; a new generation does not claim to undo or know them. Provider credentials and enrollment are provisioned separately and never included in support output or the package.

## Maintenance, upgrades and retention

Explicit `init` validates compatibility and migrates under the exclusive service lock, retaining a pre-migration snapshot only when an upgrade is needed. Package replacement stops the service; package scripts do not migrate or provision authority. The pinned DBOS release remains unchanged. An older package must refuse an M5 store.

`verify` checks stopped storage and artifacts. `diagnostics` reports only an allowlist of versions, checks, counts, enabled profiles and the recovery prefix, with stable error codes instead of raw exceptions; it may run while the daemon is active. Generated-backup pruning requires the stopped-service lock, supports dry-run, keeps a configurable minimum and age, and never follows symlinks or deletes external/user-named backups, live DB/WAL, task history or host tombstones.

Task/artifact/event/idempotency history is retained for the lifetime of a store. There is no safe partial history GC in the selected bounded profile. Full decommissioning is an explicit host-owner operation after stopping/reconciling all work and retiring all backup copies; use storage-provider encryption/erasure guarantees when required. Define this honestly rather than calling unlink secure erasure. Generated maintenance backups default to a documented seven-day/three-copy retention recommendation, applied explicitly; no hidden deletion or global journald reconfiguration.

## Acceptance and revisit

Portable tests cover corruption, referenced-artifact loss, private file handling, stopped-store locking, migration rollback, incomplete restoration, stale grant/budget denial, fresh-scope execution and retention/redaction. Installed Debian acceptance must cover a real upgrade/downgrade refusal, an interrupted host effect across whole-machine reboot, a durable wait across reboot, restored results and fresh policy/new-scope execution. Re-run real-provider and relevant existing tests; retain earlier evidence unchanged. Record exact code/package hashes and cleanup. New runtime/schema releases, external artifact storage, partial-history GC, multi-host execution or automatic post-snapshot effect reconciliation require renewed decisions and fit tests.
