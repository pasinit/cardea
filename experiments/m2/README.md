# M2 Debian confinement fit

A disposable Debian 13 amd64 VM tests a systemd + bubblewrap composition against bounded hostile workers. The guest has its own Debian kernel and systemd PID 1. Docker hosts QEMU with software CPU emulation; it is not the worker sandbox and receives no privileged mode, host devices or host cgroup mounts.

The VM represents one persistent Debian host for the duration of the experiment and runs many worker jobs. Each worker is a process isolated by systemd/bubblewrap on that host; no Debian VM is created per job. QEMU and Docker let a macOS development machine run the Debian acceptance environment. The deployment target is the Cardea stack running directly on a Debian machine, whether bare metal or a whole-machine VM.

This is a host-component experiment, **not an agent-facing launcher or completed M2**. Do not expose its root-admin command builder to agents. Production integration still needs an installed-operation registry, narrow launcher authorization, live supervisor/ancestor checks, durable intent/reservations, result verification and crash reconciliation. The existing authenticated daemon and production dependencies are unchanged.

## Run

Prerequisites: Docker, SSH and the existing API development environment. Pick a **new** absolute state directory. The script refuses an existing one. Its private state includes a disposable VM key; keep it out of Git and reports.

```sh
uv run --locked --project apps/api python experiments/m2/vm.py start --state /tmp/my-cardea-m2-fit
make m2-host-fit M2_STATE=/tmp/my-cardea-m2-fit
uv run --locked --project apps/api python experiments/m2/vm.py stop --state /tmp/my-cardea-m2-fit
make m2-host-check
```

`start --image /absolute/path/to/base.qcow2` can reuse a cached image; the same pinned SHA-512 is required. The versioned Debian cloud image URL identifies build `20260914-2601`; its frozen digest is checked independently. The runner pins the emulator base image and direct Debian package versions, installs bubblewrap 0.12.0-1~deb13u1 in the guest, and creates a dedicated locked system account. The accepted result records the guest package inventory; the environment manifest records source and image identities.

On this Mac the Docker environment was explicitly selected per command:

```sh
export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock
export DOCKER_CONFIG=/tmp/cardea-m1-docker-config
```

These settings are host-specific; use your normal Docker context elsewhere. No Docker global settings are changed. `stop` removes only the container ID recorded in that state directory, retaining the stopped guest files and report for inspection.

## Tested profile

`systemd_profile.py` declares the candidate profile; `host_fit.py` runs inside the marked disposable guest only. Systemd runs bubblewrap under the dedicated `cardea-fit` account. Bubblewrap gives the worker UID 65534 inside private user/PID/mount/IPC/UTS/cgroup namespaces, drops capabilities, disables further user namespaces, clears its environment, and exposes only read-only declared inputs and `/usr`, minimal devices, proc and private scratch. Systemd supplies no-new-privileges, a private network, seccomp restrictions and cgroup v2 limits. Socket creation is denied, including Unix sockets. Standard output/error go to a per-unit file opened by systemd and bounded by `RLIMIT_FSIZE`.

Limits: 64 MiB memory, no swap, 8 processes/threads, 50% of one CPU, 8-second runtime (2 seconds in the deadline probe), 1-second stop timeout, 64 KiB per output/scratch file, and 1 MiB scratch tmpfs. Probes attempt privilege/namespace escape, filesystem/symlink/secret access, socket creation, resource exhaustion, detached double-fork survival and cross-worker scratch reuse.

`--probe` reports necessary host facilities without dispatch; failure exits 78. Passing preflight merely permits running the fit suite and never establishes enforcement. macOS and the ordinary M1 Docker container are recorded as ineligible.

## Limits of the result

- The administrative fixture runs as guest root; every tested worker is unprivileged. Least-privilege Cardea-to-systemd dispatch is still an integration requirement.
- The worker cannot see host files, host PIDs or the cgroup filesystem through its mounted view. This is not a general host-information confidentiality guarantee: Linux proc exposes some host-wide metadata. The dedicated outer account is shared by these test services; production account and launcher ownership remain to be integrated.
- `/usr` is a trusted, read-only runtime tree. Production packaging must define its contents; do not put credentials there. Writable executable memory and nested user namespaces are denied. Scratch is `nosuid,nodev`; this profile does not promise `noexec` or prohibit all new namespaces. Read-only runtime executables remain usable.
- These tests establish the checked controls in this guest, not immunity to kernel vulnerabilities, side channels or hostile native extensions on arbitrary hosts.
- No full-OS reboot recovery, authority revocation during a running production operation, budget ledger, remote effects, or model execution is established here. Emulated timings are not performance capacity figures.

See the [executed report](REPORT.md), [ADR-012](../../docs/decisions/012-m2-host-confinement.md) and the checked-in evidence for results and remaining work.

The subsequent authenticated production integration has separate [setup instructions](INTEGRATION.md) and an [acceptance report](INTEGRATION-REPORT.md); this historical host-fit evidence remains scoped to its recorded source.
