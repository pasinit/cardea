# M5 operational acceptance

Date: 2026-09-25. **Result: M5 complete for the supported single-Debian-host profile.** This report extends the preserved [packaging/reboot evidence](PACKAGING-REPORT.md); it does not replace that earlier run.

## Scope and decisions

[ADR-018](../../docs/decisions/018-m5-debian-package.md) supplies standard Debian packaging and systemd ownership. [ADR-019](../../docs/decisions/019-m5-operational-recovery.md) reuses SQLite backup/integrity, Alembic, DBOS current-store recovery, journald and Python stdlib. The bounded custom addition is a recovery epoch at the existing transactional policy boundary: old snapshots cannot establish current authority, budget consumption or external effects.

The delivered profile remains one persistent Debian host with confined per-job processes, real SPIRE authentication, native MCP for the bounded M3 incident runtime and async native A2A for the bounded M4 coordinator. No new remote-peer protocol, scheduler, general-purpose agent execution or per-job VM is introduced.

## Acceptance matrix

| Gate | Evidence and result |
| --- | --- |
| Fresh install and enrollment | Preserved revision 1 report: inactive/unconfigured install, dedicated identities/accounts, isolated imports, source/license inventory, real SPIRE, workers and native A2A. |
| Portable contracts and regressions | 305 backend and 4 frontend tests pass. Root lint and type checks, M2 host-tooling checks and M5 packaging/fixture checks pass. |
| Real-provider regressions | 45 existing M1 cases pass in the full run; the revised snapshot-restore case passes separately after updating its deliberately superseded replay expectation. Total 46 cases. |
| Recovery correctness | Portable tests cover artifact digest damage, missing/version-changed references, expired versus live leases, queued/running work, disabled identities, stale and previously absent scopes, missing fresh budgets and successful fresh host work. |
| Crash-safe publication | Private staging, standalone backup/sidecar refusal, corrupt-destination preservation, failure before publication, interruption after marker/replace, refusal to open pending recovery, repeatable completion and dangling-marker rejection pass. |
| Upgrade compatibility | Portable M4-to-M5 transactional failure rollback and unsupported runtime refusal pass. Actual revision 1→2 upgrade preserves existing receipts; both pre-migration new-package startup and old-package startup on M5 refuse. Reinstalling revision 2 preserves state. |
| Full-machine interrupted-work recovery | Installed barrier after a real host start survives a full-machine reboot as `outcome_unknown`, with no successful result. A durable event wait retains its receipt and resumes to verified completion. |
| Snapshot restore and fresh execution | Installed post-snapshot spending/revocation, artifact-checked restore, disabled principals/cleared policy, unchanged receipts, freshly authorized historical reads, sealed-scope denial and successful fresh-scope A2A with two children all pass. The host tombstone for the post-snapshot task is retained. |
| Diagnostics and retention | Portable private error redaction, stopped/active checks, count/age retention, dry-run/apply, service-lock and symlink denial pass. Installed corruption and two publication-interruption cases recover safely. Four aged generated backups are pruned while two recent snapshots and user/history data remain. Online/offline diagnostics, CLI verification, package checksum verification, unit validation and dependency consistency pass. |

## Interpretation and limits

Reboot recovery uses the current database and preserves normal wait semantics. Snapshot restore deliberately fails unfinished historical work, records uncertain running operations without replay, disables old principals and removes policies. Fresh provisioning is required. The new namespace prefix fences all old scope names even when absent from the snapshot; fresh budgets must be provisioned in the new scope. Completed receipts and outputs remain unchanged and require current read authority.

The package does not automatically enroll, migrate, start on install or grant permissions. The compatibility claim is revision 1/schema M4 to revision 2/schema M5 with the same DBOS 3.0.0 runtime, plus tested refusal of incompatible versions. Locked inputs and recorded hashes support repeatable installation; byte-identical independent package builds are not claimed.

Task/artifact/event/idempotency history remains lifetime-retained. Generated backups have explicit age/count pruning; user backups, forensic originals and host tombstones are excluded. Decommissioning and storage erasure are explicit host-owner procedures. Diagnostics report local readiness, not provider liveness or completion. No malicious-host-admin tamper proof, cross-host failover or external exactly-once execution is claimed.

A second Unix adapter, supervisor UI and escalation-app port are conditional roadmap items; this profile has no demonstrated need for them. Other hosts remain unsupported. The existing M4 load warning is unchanged; M5 operational acceptance is not a new performance comparison or a claim to have resolved it.

## Run notes

The persisted test machine's short-lived SPIRE node enrollment expired while powered off. The disposable node was explicitly re-enrolled against the retained provider before baseline acceptance; the Cardea task store was preserved. This was an identity-fixture renewal, not a recovery of authority from a snapshot.

The first installed post-reboot assertion read `state` from the API envelope instead of its nested `receipt`. Its failure log is retained. A later negative-test request omitted its required empty payload and was corrected before admission. Both corrected phases resumed from the same retained state; no tasks or disk were reset.

The initial real-provider restore test still expected M1's old raw-snapshot waiting-task replay. M5 intentionally supersedes that behavior. The updated case verifies disabled restored principals, explicit provisioning, retained receipt and refusal to resume the sealed task. Other 45 cases passed unchanged.

The final package `cardea-kernel_0.1.0-2_amd64.deb` is 39,674,006 bytes, SHA-256 `04aa139c86d873314fd973a47d24d05ee5aae98cd265987008de6d543557bfa1`. All 75 staged source files match the final tested workspace. The [source manifest](evidence/completion/source-manifest.json) and [package metadata](evidence/completion/package.json) are retained. All 75 **installed** source files and the privileged helper match the manifest; [installed inventory](evidence/completion/installed-inventory.json) records exact package/host versions. `dpkg -V`, `systemd-analyze verify` and `pip check` pass. The final store has 9 tasks and 16 objects with integrity/reference verification passing.

The guest is Debian 13.7 amd64, kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, Python `3.13.5-2+deb13u5`, SPIRE `1.15.3`, DBOS `3.0.0`. It runs under QEMU TCG with 2 vCPUs and 1.5 GiB RAM, inside an owned Docker container limited to 2 CPUs/3 GiB on the macOS arm64 host. This establishes operational behavior, not native-hardware performance.

The observed boot ID changed from `9e7322b3-50ba-4b5e-b896-aedb60174cb7` to `aa3d52ed-9980-435f-92f8-ab0d2e6086e4`. [Result data](evidence/completion/result.json) records receipts, fresh output, recovery prefix and retention. [Tombstone checks](evidence/completion/tombstones.log) confirm both the interrupted and post-snapshot jobs remain retired. Raw test-provider configuration, keys and tokens are excluded from collected evidence; no model credential or paid call was used.

The owned VM is powered off after checking its mount, disposable sentinel, inactive service/provider units and absent worker units/processes. Its outer container is removed; the disk and package are retained under `/tmp/cardea-m5-vm`. See [cleanup](evidence/completion/cleanup.json). The standalone package also remains there for review. No repository commit or public release was made.
