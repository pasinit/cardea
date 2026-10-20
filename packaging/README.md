# Debian package and operations

This internal package delivers the bounded M1–M4 runtime on the **whole Debian host**. Jobs still run in separate confined systemd/bubblewrap processes. See [ADR-018](../docs/decisions/018-m5-debian-package.md). Operational recovery, upgrades, diagnostics and retention follow [ADR-019](../docs/decisions/019-m5-operational-recovery.md) and the [operating guide](../docs/operations.md).

## Build

Stage only source, package metadata and hash-locked production dependencies:

```sh
make m5-stage M5_STAGE=/tmp/cardea-build
```

Copy that directory to a Debian 13 amd64 builder. Install `debhelper`, `dh-virtualenv=1.2.2-1.7`, `python3-venv`, `python3-pip` and `build-essential`, then run `dpkg-buildpackage -us -uc -b` inside it. The build downloads only binary Python wheels and checks the exported lock hashes. The resulting `.deb` contains its private dependency environment; installation does not invoke pip. An isolated/reproducible builder and byte-identical output remain future hardening; keep the lock, source manifest, package hash and build environment inventory together.

## Install and enroll

The supported execution profile is deliberately narrow: Debian 13 amd64, a Debian 13 6.12 LTS kernel (`6.12.N+deb13-amd64` or `-cloud-amd64`; acceptance ran on `6.12.107+deb13-cloud-amd64`), systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, cgroup v2 CPU/memory/pids controllers and the non-setuid bubblewrap launcher. Updated versions require another host fit; installation alone does not certify enforcement.

1. As host owner, install with `apt install ./cardea-kernel_0.1.0-2_amd64.deb`. It creates separate `cardea-kernel` and `cardea-worker` system accounts, a private store and the fixed helper. It neither enables nor starts the service.
2. Enroll the machine with your existing SPIRE deployment. Its Workload API socket must be reachable by `cardea-kernel`; register the runtime identity against that account. Enroll supervisors through the existing SPIFFE/OIDC contracts. No test provider or authority document ships in this package.
3. Adapt `/usr/share/doc/cardea-kernel/config.example.json` into `/etc/cardea/config.json`. Keep `data_dir` at `/var/lib/cardea`. Set the real trust domain, socket and runtime ID. The configuration must be owned by `cardea-kernel:cardea-kernel`, mode `0600`; `/etc/cardea` is root-owned and not service-writable. Optional broker/identity secret files have the same private ownership requirement. Do not place secrets in command arguments or unit environment text.
4. Initialize with `runuser -u cardea-kernel -- cardea init --config /etc/cardea/config.json`. Explicitly provision your reviewed private policy with `runuser -u cardea-kernel -- cardea provision /path/to/private-policy.json --config /etc/cardea/config.json`. The policy must also be readable and privately owned by that account. Installing a service grants no task authority.
5. Add systemd ordering against your identity-provider unit if needed. Enable with `systemctl enable --now cardea-kernel.service`. The private socket is `/var/lib/cardea/kernel.sock`; local callers need OS socket access **and** authenticated Cardea authority. Account switching does not replace SPIFFE/OIDC authentication.

`cardea --help` exposes the existing authenticated client and administrative commands. The service uses isolated Python imports from root-owned `/usr/lib/cardea`; it does not load code from the caller's working directory or `PYTHONPATH`.

## Operate and upgrade

Use `systemctl status cardea-kernel` for process state. The authenticated `/v0.2/system` response reports enabled profiles; task receipts/events remain the source of execution evidence. Do not export raw journald logs or configuration as a supposedly redacted support bundle. `/usr/share/doc/cardea-kernel/dependency-inventory.json.gz` records dependency versions and declared license metadata; their installed license files remain in the environment. This is an inventory, not a license or vulnerability audit. Cardea is licensed under [Apache-2.0](../LICENSE) (declared 2026-10-10); this package is still a pre-release for the fixed profile, not a supported public release.

Package replacement stops the daemon before changing its files and does not automatically restart it or migrate its database. First quiesce admissions and reconcile outstanding tasks/host effects. Stop the service, take the existing stopped-service SQLite backup, retain host reconciliation state, then install the replacement. Run only the explicit migration path verified for that source/target version, validate and restart. The bounded upgrade path is revision 1/schema M4 to revision 2/schema M5, with the same pinned DBOS runtime. An older package refuses the newer schema; reinstall the supported package rather than downgrading the database. Package removal leaves data, helper tombstones, configuration and accounts; it does not erase audit or policy history.

Snapshot restore now validates artifacts in private staging, seals historical execution, disables restored principals and requires fresh policy and budgets in a new recovery namespace. Ordinary current-store reboot recovery retains wait semantics. Follow the [complete recovery and retention procedure](../docs/operations.md); do not clear recovery markers or host reconciliation tombstones. No automatic deletion is installed.

The earlier development helper moved from `/usr/local/libexec/cardea-host` to `/usr/libexec/cardea-host`. Existing source-based acceptance machines should rerun their explicit helper installer. Never retain a stale extra sudoers rule as an alternative packaged authority path; remove the old development installation when migrating a host. This increment tests fresh package installation, not that migration.
