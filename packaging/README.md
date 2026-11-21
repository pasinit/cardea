# Debian package and operations

This internal package delivers the bounded M1–M4 runtime on the **whole Debian host**. Jobs still run in separate confined systemd/bubblewrap processes. See [ADR-018](../docs/decisions/018-m5-debian-package.md). Operational recovery, upgrades, diagnostics and retention follow [ADR-019](../docs/decisions/019-m5-operational-recovery.md) and the [operating guide](../docs/operations.md).

## Build

Stage only source, package metadata and hash-locked production dependencies:

```sh
make m5-stage M5_STAGE=/tmp/cardea-build
```

Copy that directory to a Debian 13 builder of the target architecture (amd64 or arm64). The build first checks the tree against the stage's `source-manifest.json` (`debian/verify_stage.py`) and refuses any file added, changed or missing. From macOS, copy without metadata: macOS `tar` and `cp` add an AppleDouble `._NAME` file beside each file with extended attributes, which would otherwise be packaged (use `COPYFILE_DISABLE=1 tar`). Install `debhelper`, `dh-virtualenv=1.2.2-1.7`, `python3-venv`, `python3-pip` and `build-essential`, then run `dpkg-buildpackage -us -uc -b` inside it. The build downloads only binary Python wheels and checks the exported lock hashes. The resulting `.deb` contains its private dependency environment; installation does not invoke pip. An isolated/reproducible builder and byte-identical output remain future hardening; keep the lock, source manifest, package hash and build environment inventory together.

`sh packaging/container_build.sh STAGE OUT` does the same in a fresh `debian:13` container of the machine's architecture, then installs the result in a second fresh container and checks `dpkg -V`, `pip check`, the accounts, the sudoers mode and the installed imports. CI runs it on amd64 and arm64 runners. A container is not a host: that job proves the package builds and installs, not that confinement works.

### Architectures

The package is built for **amd64** and **arm64** (`Architecture: amd64 arm64`). The exported lock already carries `manylinux` aarch64 wheels for CPython 3.13 for every binary dependency; the build's `--only-binary=:all:` and hash checks apply unchanged. `packaging/fetch_gitleaks.py` and `fetch_opencode.py` pin Linux aarch64 artifacts too.

| Architecture | Status |
|---|---|
| amd64 | The supported profile. The M2–M5 host fits and acceptance ran on it (an emulated Debian 13 amd64 guest). |
| arm64 | **Development profile.** The package builds and installs on Debian 13 arm64 and the helper accepts Debian's arm64 6.12 kernels. On 2026-11-17 the M2 host fit, the ADR-028 probes and the Debian acceptance scripts ran on a Debian 13 arm64 dev host (kernel `6.12.115+deb13.1-cloud-arm64`) and passed ([evidence](../experiments/debian-acceptance/evidence/arm64-lima/summary.json)). It remains a development profile until that host type is adopted as supported. |

## Install and enroll

The supported execution profile is deliberately narrow: Debian 13 amd64, a Debian 13 6.12 LTS kernel (`6.12.N+deb13-amd64` or `-cloud-amd64`, with or without a point-release suffix such as `+deb13.1`; acceptance ran on `6.12.107+deb13-cloud-amd64`; on the arm64 development profile the helper accepts `6.12.N+deb13(.N)-arm64` or `-cloud-arm64`, for example `6.12.115+deb13.1-cloud-arm64`, and refuses a kernel of the other architecture), systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, cgroup v2 CPU/memory/pids controllers and the non-setuid bubblewrap launcher. Later Debian stable or security updates of the same upstream version and Debian revision are accepted (for example `systemd 257.13-1~deb13u2` or `sudo 1.9.16p2-3+deb13u5`), both by the package's dependencies and by the helper's probe ([ADR-018 amendment](../docs/decisions/018-m5-debian-package.md#amendment-2026-11-21-security-updates-within-the-accepted-package-lines)). A new upstream version or Debian revision requires another host fit; installation alone does not certify enforcement.

1. As host owner, install with `apt install ./cardea-kernel_0.1.0-2_amd64.deb` (`_arm64.deb` on arm64). It creates separate `cardea-kernel` and `cardea-worker` system accounts, a private store and the fixed helper. It neither enables nor starts the service.
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
