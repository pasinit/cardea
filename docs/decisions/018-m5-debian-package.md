# ADR-018 — Debian deployment, first M5 increment

Date: 2026-09-24. Owner: runtime/operations. Milestone: M5 (R04/R12).

**Status: implemented and verified for the initial package/reboot increment.** M5 was not complete at this increment; it was subsequently completed for the supported profile under ADR-019 (see the [M5 report](../../experiments/m5/M5-REPORT.md)). See the [packaging report](../../experiments/m5/PACKAGING-REPORT.md).

## Requirement

Install the existing bounded kernel on one persistent Debian 13 amd64 machine without a source checkout or developer environment. Preserve the authenticated private socket, dedicated kernel/worker accounts, live supervisor ceiling and exact supported host profile. Use explicit initialization/configuration; installing a package must not create identities, grants or model credentials. Verify package installation, confinement, service restart and reboot separately from historical source-run tests.

## Candidates and sources

| Candidate | Fit and maintenance/license evidence | Choice |
| --- | --- | --- |
| Debian debhelper/dpkg + systemd | Debian 13 maintained tooling; GPL-2-or-later debhelper, GPL-2-or-later dpkg, LGPL-2.1-or-later systemd core. Native ownership, dependency checks, service lifecycle and configuration preservation. [dh_installsystemd](https://manpages.debian.org/trixie/debhelper/dh_installsystemd.1.en.html), [systemd.exec](https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html). Exact installed build versions recorded by the fit. | Reuse. |
| dh-python/pybuild | Maintained Debian Python team tooling, [Debian package](https://packages.debian.org/trixie/dh-python). Appropriate for distro-managed Python modules, but Cardea's already selected exact A2A/MCP/DBOS/provider graph is locked in uv rather than supplied as matching Debian dependencies. No runtime fit claimed. | Revisit for distribution packaging; do not loosen tested runtime versions. |
| dh-virtualenv 1.2.2-1.7 | Available in Debian 13 with distro maintenance; upstream releases are slow. GPL-2-or-later. [Debian source](https://packages.debian.org/source/trixie/dh-virtualenv), [upstream usage](https://github.com/spotify/dh-virtualenv/blob/master/doc/usage.rst). Packages a private environment and handles relocation. Supports dependency-only installation and ordinary debhelper source installation. Python 3.13 fit passed with the standard-library venv backend; the optional standalone virtualenv command is not required. | Select with debhelper; no custom virtualenv relocator. |
| OCI image/Compose | Existing container infrastructure is useful for building. Does not replace the selected systemd/cgroup host boundary; putting the whole privileged kernel in a container needs a new host profile. | Retain QEMU container as test harness only. |
| SQLite backup API, existing Alembic | Existing selected storage facilities remain appropriate. [SQLite backup API](https://www.sqlite.org/backup.html). A valid snapshot alone cannot reconcile external effects or newer revocations. | Retain; operational restore safety remains a separate M5 gate. |
| journald and existing authenticated diagnostics | Already available with systemd and Cardea. OpenTelemetry is a later option for cross-host tracing; not required to introduce an exporter/network destination for one host. | Reuse; no second audit journal. |

## Bounded integration

Export production dependencies with `uv export --locked` and hashes. dh-virtualenv builds the private environment; source and fixed CLI wrapper live in root-owned system paths. Pin the supported Debian dependency versions, record installed Python versions/licenses and lock digest. There is no install-time pip/network operation. This increment establishes repeatable locked inputs, not byte-identical package builds or a vulnerability/license audit. Cardea itself had no declared redistribution license at the time; it has been licensed under [Apache-2.0](../../LICENSE) since 2026-10-10.

Move the single fixed host helper from the development `/usr/local/libexec` location to `/usr/libexec`, updating its installer and fit together. A dedicated unprivileged service calls only that argument-free sudo helper; workers remain separate and confined. The service needs that narrow sudo transition, so `NoNewPrivileges=yes` on the broker would break the chosen host adapter; workers continue to enforce it. Package removal preserves state and identities. No automatic schema migration, policy provisioning, downgrade, database restore or data deletion in package scripts. Stop the service before replacing package files; an operator explicitly validates/migrates before restarting.

Configuration remains a private service-owned regular file as required by the existing CLI. `LoadCredential` is a candidate for a later consistent admin/daemon secret path; this increment does not add a second credential loader. No credentials or example authority grants ship in the package. Identity provider enrollment remains an external prerequisite.

## Remaining gates and revisit triggers

The [observed fit](../../experiments/m5/PACKAGING-REPORT.md) uses debhelper 13.24.2, dh-virtualenv 1.2.2-1.7 and dpkg 1.22.22 on a fresh Debian 13.7 amd64 machine. Build, inactive install, isolated imports, worker denial, native A2A execution, completed-state/receipt persistence, persistent revocation and new permitted work after a whole-machine reboot pass. All 73 installed source hashes match the staged manifest. M5 additionally needs interrupted-work reboot cases, tested upgrades, conservative restoration (including artifact integrity and revocation/accounting rollback), redacted support diagnostics and retention guidance. A distribution release, Python ABI change, supported-host update or dependency security update requires renewed inventory/fit and acceptance; arbitrary remote peers and other Unix profiles remain outside scope.

## Subsequent completion

2026-09-25: [ADR-019](019-m5-operational-recovery.md) and the [M5 report](../../experiments/m5/M5-REPORT.md) close the remaining operational gates. The revision 1 observations above remain historical evidence.
