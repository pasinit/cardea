# M5 first increment — installed Debian service

Date: 2026-09-24. **Result: package installation and the bounded reboot scenario pass. M5 remains in progress.** Decision: [ADR-018](../../docs/decisions/018-m5-debian-package.md). [Operator instructions](../../packaging/README.md), [repeatable fixture](README.md).

## Delivered

`cardea-kernel_0.1.0-1_amd64.deb` installs the existing bounded runtime without a source checkout or developer environment. Debian debhelper/dh-virtualenv supply the private dependency environment, package metadata and systemd integration. Production dependencies come from the existing uv lock with download hashes. Installation performs no pip operation, creates separate unprivileged service/worker accounts, and leaves the service disabled and inactive until explicit configuration, enrollment and provisioning.

The root-owned entry point ignores caller working directories and `PYTHONPATH`. The fixed, argument-free sudo helper now lives at `/usr/libexec/cardea-host`; its source digest must match the installed adapter. The systemd service has private state, a read-only system filesystem and explicit writable state paths. Broker privileges remain bounded by the existing helper; task authority still comes from the authenticated live supervisor and ancestor ceilings. Package scripts do not create application identities, grants, migrations or restore operations.

## Observed acceptance

The package was built and installed on a fresh Debian cloud-image machine, not the earlier M2–M4 source-based installation. The machine is an amd64 QEMU guest on an arm64 macOS host; each job remains a confined process on that one persistent Debian machine.

| Check | Result |
| --- | --- |
| Locked Debian package build and first installation | Pass; package SHA-256 and exact OS/tool versions recorded |
| Dependency consistency and systemd unit validation | Pass; `pip check`, `systemd-analyze verify`, `dpkg -V` |
| Installed source and helper identity | All 73 source-file hashes match the included source manifest; helper digest matches |
| No automatic authority or daemon start | No config/database created by installation; unit disabled and inactive |
| Worker and unenrolled local account invoking root helper | Denied |
| Worker reading service configuration | Denied |
| CLI with poisoned working directory and `PYTHONPATH` | Ignores the injected module and loads installed code |
| Authenticated native async A2A delegation | Parent and two hash/copy children complete with independent verification |
| Supervisor `host.copy` revocation before reboot | A new delegation requiring that permission is denied |
| Whole-machine reboot | Boot ID changes; packaged service starts automatically through systemd |
| Stored results after reboot | Parent success, output, child associations/usage and receipt unchanged |
| Revocation after reboot | New delegation remains denied |
| New permitted operation after reboot | Confined hash worker completes with passing verification |
| Existing portable regression checks | 290 backend tests, 4 frontend tests; root lint/type checks pass |
| Packaging and changed host harness checks | Ruff/mypy and package shell syntax pass |

Real SPIRE 1.15.3 provides fixture enrollment and identity verification. It is deliberately external to the package. The reboot scenario uses deterministic host operations; it does not repeat M3's paid model acceptance or the entire M1–M4 integration/load matrix.

## Evidence and corrections

- [Installed inventory](evidence/installed-inventory.json): Debian 13.7, kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, Python 3.13 and exact build dependencies.
- [Source manifest](evidence/source-manifest.json), [74 installed distributions and license-file references](evidence/dependency-inventory.json), [package verification](evidence/package-verification.log).
- [Acceptance summary](evidence/acceptance.log), [pre-reboot](evidence/before.log), [post-reboot](evidence/after.log), [persisted result/receipt](evidence/reboot.json).
- [Build](evidence/build.log), [installation](evidence/install.log), [tests](evidence/tests.log), [lint](evidence/lint.log), [types](evidence/types.log), [packaging checks](evidence/packaging-check.log), [host checks](evidence/host-check.log).

The recipe fit corrected inventory-generation ordering and switched dh-virtualenv to Python's standard-library venv backend because a minimal builder did not install the optional `virtualenv` command. A superseded trial build used slower default processing/compression; the final recipe excludes the private environment from timestamp normalization and uses Debian's gzip compression option. Build warnings about merged-/usr loader diversion and existing vendor-library/debug metadata were retained; dependency resolution and installed execution pass.

The first acceptance attempt failed in the fixture: its readiness loop did not catch an HTTP transport error while the new daemon socket was absent. [That failure is retained](evidence/initial-readiness-failure.log). The daemon itself became healthy. The fixture now retries transport readiness within its existing deadline. After confirming zero tasks had been admitted, the disposable provider/store fixture was reset and the complete scenario passed with the unchanged package.

The package is 39,666,552 bytes, SHA-256 `7aedaf59ffe51151d1ff8d7a6af3c3cbe2f7eb089f6bd21b7f377fc5055a4863`. The local artifact is retained in `/tmp/cardea-m5-vm/cardea-kernel_0.1.0-1_amd64.deb`; it is not committed as a binary. This proves repeatable locked installation inputs, not byte-identical builds. License metadata/file references are an inventory, not a redistribution approval or vulnerability audit; Cardea itself has no declared redistribution license.

The owned acceptance machine was powered off and its container removed after confirming no worker units were running. The disk and verified package remain available locally; see [cleanup evidence](evidence/cleanup.json).

## Remaining M5 gates

1. Test source/target schema and runtime upgrades, including failed upgrades and recovery. This increment stops the service before replacement; it does not certify an upgrade/downgrade matrix.
2. Design and verify conservative operational restore: artifact integrity, stale supervisor grants, rolled-back budgets, host tombstones and uncertain external effects. The existing raw snapshot restore must not be treated as safe operational rollback.
3. Exercise interrupted work across machine reboot, beyond the completed-state persistence demonstrated here.
4. Deliver a redacted support-diagnostics command and explicit retention/deletion policy. Existing authenticated diagnostics and journald are available; no new exporter or automatic deletion is installed.
5. Run the resulting operational acceptance suite. M4's provisional load-latency warning is unchanged and was not remeasured here. Additional hosts, arbitrary remote peers and a supervisor UI are not implied.
