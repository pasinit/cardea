# Operating the single Debian host

M5 supports the fixed Debian profile in [packaging](../packaging/README.md), using SQLite, Alembic, DBOS and systemd. Installation does not grant authority. The daemon runs as `cardea-kernel`; confined jobs use `cardea-worker` on the same machine. Use the authenticated client for tasks and the explicit host-owner commands below for maintenance. See [ADR-019](decisions/019-m5-operational-recovery.md) and the [acceptance report](../experiments/m5/M5-REPORT.md) for evidence and limits.

## Configuration and enrollment

Keep `/etc/cardea/config.json` privately owned by `cardea-kernel`, mode `0600`, inside root-owned `/etc/cardea`. Keep `data_dir` at `/var/lib/cardea`, mode `0700`. Provision SPIRE/OIDC identities separately; provider enrollment and model keys are neither package contents nor backup contents. Renew an expired host enrollment using your identity provider's procedure. The service fails closed when it cannot authenticate its runtime.

Review a private provisioning document for current supervisor, agent and runtime identities, scoped permissions and budgets. Run `cardea provision` as the kernel account. Provisioning replaces current policy; it cannot change immutable identity/task provenance or bypass the live supervisor/ancestor/platform ceiling.

## Confined worker profiles

Installed operations and agent sessions may run as confined workers under profiles that the host owner defines in `/etc/cardea-host-workers.json` (root-owned, not group- or world-writable). See [ADR-028](decisions/028-configurable-worker-profiles.md) for the format and limits. Networked profiles reach only their loopback egress proxy, which must enforce the same hostname list as the profile. Keep session credentials in root-only files under `/etc/cardea/credentials/`. After changing the file, restart `cardea-kernel`. The package installs the matching `/usr/libexec/cardea-host`; source-based acceptance hosts must rerun their helper installer, or the source-hash probe refuses to start. Debian enforcement of these profiles is not yet verified.

## Restart and reboot

Use `systemctl restart cardea-kernel` or reboot the host. DBOS recovers the **current** shared database. Durable waits remain waiting and can receive authenticated input/resume. A host intent interrupted before receipt settlement becomes `outcome_unknown`, even if its process might have finished. It is never automatically retried or called successful. Preserve `/var/lib/cardea-host`, including reconciliation tombstones; do not clear it to force a retry.

## Upgrade

The verified compatibility path is package `0.1.0-1` / schema `m4_001` to `0.1.0-2` / schema `m5_001`, keeping DBOS `3.0.0` and workflow version `cardea-m1-v1`. Older service schema migrations have portable regression coverage. Arbitrary future dependencies, runtime versions and host versions require another fit test.

1. Arrange a maintenance window, stop admissions and reconcile outstanding work. Stop with `systemctl stop cardea-kernel`.
2. As `cardea-kernel`, run `cardea backup /private/path/pre-upgrade.sqlite3 --config /etc/cardea/config.json`. The destination must not already exist.
3. Install the reviewed package. Package replacement stops the daemon and leaves it stopped; scripts neither migrate nor provision.
4. Run `cardea init --config /etc/cardea/config.json` as `cardea-kernel`. It checks compatibility, preserves a pre-migration snapshot and applies Alembic transactionally. Repeating `init` on the current schema changes nothing and makes no redundant backup.
5. Run `cardea verify --config /etc/cardea/config.json`, then start the service and inspect `cardea diagnostics` plus authenticated `/v0.2/system` and a permitted test task.

A failed migration rolls back. An unsupported schema/runtime refuses startup. Do not downgrade the database: an older package refuses an M5 store. Reinstall the supported package to recover from accidental binary downgrade; existing receipts remain unchanged.

## Backup and snapshot restore

A stopped-service backup uses SQLite's backup API and contains metadata, objects, journal, policies, budgets and DBOS state consistently. It is a private standalone SQLite file, independent of WAL sidecars. It contains sensitive workload and authority history: retain it privately, use your storage provider's encryption/access controls, and transfer it through an authenticated private channel.

An old snapshot cannot know about later revocations, spending or effects. Restore therefore seals historical execution rather than resuming it:

1. Stop the service and confirm the old executor is stopped. Retain the host reconciliation directory; never roll it back with a database snapshot.
2. Run `cardea restore /private/path/snapshot.sqlite3 --config /etc/cardea/config.json` as `cardea-kernel`. It rejects unsafe files, sidecar-dependent copies, unsupported versions, broken artifacts/references, clock regression and still-live copied worker leases. A lease rejection requires waiting for its real expiry.
3. Restore validates and migrates private staging before publication. It keeps the original DB/WAL/SHM in a private `before-restore-<uuid>/` forensic directory, even if corrupt. A durable `.restore-in-progress` marker blocks opening interrupted publication; rerun the restore to finish. **Do not delete the marker to force startup.** Validation failure before publication leaves the destination unchanged.
4. Completed receipts/results remain unchanged. Unfinished tasks are failed; running operations become `outcome_unknown`. All restored principals are disabled and all restored policies removed. Provision current identities and reviewed authority explicitly before starting the daemon.
5. The command, diagnostics and authenticated `/v0.2/system` expose `new_namespace_prefix`, such as `r<32 hexadecimal characters>.`. New effectful work must use that prefix and newly provisioned scoped budgets. Old namespaces are permanently sealed for execution, including namespace names absent from the snapshot. Historical reads still require current supervisor/ancestor/platform rights. Re-provisioning old rights or raising old budgets cannot reopen them.

Every subsequent restore creates another prefix. Stable request IDs can still retrieve a historical task under read authority; this does not dispatch it. Restoring after total-host loss cannot establish whether external effects occurred: use external reconciliation evidence. No snapshot rollback undoes an external effect.

## Diagnostics and verification

`cardea diagnostics --config /etc/cardea/config.json` may run online. Its JSON contains a fixed allowlist of dependency versions, configured-profile booleans, task-state counts, local checks and the recovery prefix. Errors use bounded codes; raw configuration, identities, paths, payloads, provider errors and journals are excluded. `ready` means the local schema/database, service lock and configured host probe passed. It is not proof of provider health, supervisor authority or successful task completion; check those through authenticated operations and receipts.

`cardea verify --config /etc/cardea/config.json` requires the service to be stopped. It checks supported schema/runtime, SQLite integrity/foreign keys, task and receipt snapshots/indexes, all object digests and installed-profile artifact references. Verification detects inconsistency; it does not authenticate a backup against a malicious host administrator. Host-root compromise remains outside the trust boundary.

Use local journald for operational investigation. Raw logs are not a redacted support bundle. The host owner controls journald limits using its standard configuration; Cardea does not change global host log retention.

## Retention and deletion

Task, object, event, provenance, budget, receipt and idempotency history is retained for the lifetime of a store. Host payload cleanup is recoverable and retains reconciliation tombstones. Partial history or tombstone garbage collection is unsupported because deleting it could destroy evidence or permit duplicate effects.

For automatically named SQLite maintenance snapshots, stop the service and run:

```sh
runuser -u cardea-kernel -- cardea prune-backups --config /etc/cardea/config.json --keep 3 --days 7
```

This previews eligible generated filenames. Add `--apply` to delete them. A snapshot must be older than the chosen age **and** outside the newest minimum count. The command refuses symlinks and active-service access. It never deletes the live database/WAL, user-named backups, forensic recovery directories, task data or host tombstones. Three copies/seven days is a starting policy for generated snapshots; no scheduled deletion is installed.

Whole-store decommissioning is an explicit host-owner operation: stop and reconcile work, revoke provider enrollment and credentials, retire every retained backup/forensic copy and then retire storage. Package removal preserves accounts, configuration and data. Unlinking files is not secure erasure; use the actual storage/encryption provider's erasure guarantees where required. Do not introduce real sensitive data without an appropriate lifetime-retention and decommissioning policy.
