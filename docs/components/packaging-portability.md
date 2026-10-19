# Packaging and Portability

**Reuse decision.** Reuse Debian/systemd packaging, SQLite backup facilities, and evaluate standard telemetry. Operational logs do not replace durable audit evidence. See [R12](../standards-review-2026-09-22.md#r12--clients-packaging-and-diagnostics--m1m5).

**Purpose.** Make the verified runtime reproducible to install, operate, upgrade, and recover.

**Design.** Package one Debian service with a dedicated account, private socket/storage, restricted credentials, explicit configuration, and tested dependency versions. Provide migrations, backup/restore, and diagnostics identifying supported execution profiles. Keep the daemon separate from the legacy API. Additional Unix hosts implement the host contract and publish only guarantees demonstrated by conformance tests.

**Boundary and recovery.** Upgrades must preserve durable state or fail explicitly on unsupported versions. Restore checks database identity, schema, and artifact consistency. Diagnostics redact credentials and distinguish unavailable enforcement from healthy execution. Define retention/deletion before introducing sensitive data.

**Acceptance.** A reproducible Debian installation passes the acceptance suite after reboot and backup/restore; an unsupported host profile is rejected.

**M1 delivery.** An executable daemon/client, pinned Debian acceptance image, Alembic initialization, whole-store backup/restore and version checks now exist; see [ADR-010](../decisions/010-m1-authenticated-kernel.md). M5 subsequently supplies the system package and whole-machine reboot/restore evidence below.

**Status.** M5 complete for the fixed Debian profile. ADR-018/019 deliver the private package and systemd service, explicit enrollment, transactional upgrades, artifact-checked fenced recovery, allowlisted diagnostics and explicit backup retention. The [M5 report](../../experiments/m5/M5-REPORT.md) records installed upgrade/downgrade refusal, full-machine interrupted-effect and wait recovery, restored historical reads and fresh-scope A2A execution. See [operations](../operations.md). Other Unix hosts, arbitrary future versions and byte-identical independent builds are not certified.
