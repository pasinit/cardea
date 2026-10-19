# M2 host fit — 2026-09-24

**Result: 11/11 checks passed in a freshly provisioned Debian VM.** Select systemd + bubblewrap for the bounded Debian host integration. M2 remains in progress: these results establish the tested host controls, not production dispatch or complete milestone acceptance.

## Environment and reproduction

Debian 13.7 amd64, kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, Python 3.13.5 and cgroup v2. QEMU 10.0.13 runs with software emulation on an arm64 Mac, inside an unprivileged Docker container. The guest has two virtual CPUs and 1536 MiB RAM. Timings are diagnostic, not native performance figures.

The accepted run used `vm.py start` with a digest-checked cached Debian cloud image, then `make m2-host-fit`, without manual guest changes. See [run instructions](README.md), [machine-readable results](evidence/debian-host-fit.json) and [environment/source manifest](evidence/environment.json). No transient test units remained afterwards. The disposable VM was stopped after evidence collection; private VM keys are outside the repository.

## Observed checks

| Check | Result |
| --- | --- |
| Declared input read, unprivileged UID, no capabilities/new privileges | Pass |
| Host secrets, symlink/path traversal, kernel storage paths, host sockets and read-only write denial | Pass |
| IPv4, IPv6 and Unix socket creation denied | Pass |
| Scratch file limited to 64 KiB | Pass |
| Scratch is fresh per worker and limited to 1 MiB total | Pass |
| UID escalation, nested user namespaces and writable executable memory denied | Pass |
| Fork attempts encounter the 8-task cgroup ceiling | Pass |
| Combined stdout/stderr file cannot exceed 64 KiB | Pass |
| 256 MiB allocation exceeds the 64 MiB memory ceiling and is killed | Pass |
| Long-running worker terminated at the 2-second test deadline | Pass |
| 50% CPU quota throttles; cancellation removes detached, double-forked descendants | Pass |

The final cancellation probe observed four processes in the service cgroup, verified CPU/memory/process control values, then verified the observed PIDs disappeared and the cgroup was empty or removed. `TREE_READY` ensures the detached descendant exists before cancellation.

Both [macOS](evidence/macos-preflight.json) and the [ordinary M1 container](evidence/m1-container-preflight.json) fail preflight with exit 78. Preflight success alone never claims enforcement.

## Why this composition

Systemd-only passed the initial narrower suite but failed the expanded 1 MiB scratch-capacity test. Its dynamic-user private mounts overrode the intended mount. The [failed result](evidence/systemd-only-gap.json) is retained as historical negative evidence, with its then-current profile properties and source hashes; it is not an accepted run of the final source.

The combined profile also exposed an incompatibility: systemd DynamicUser implies a filter that blocks `openat2`, required by bubblewrap's safe path handling. Systemd's outer proc masking interfered with mounting an isolated proc filesystem. The selected composition uses a dedicated unprivileged account, gives bubblewrap control of the filesystem/PID boundary, and leaves resource control, deadlines and cleanup with systemd. The exact requirements, sources, alternatives and decision are in [ADR-012](../../docs/decisions/012-m2-host-confinement.md).

## Validation and remaining work

`make m2-host-check` passes Ruff and mypy for all three experiment scripts. Repository `make test` passes 144 backend and 4 frontend tests; `make lint` and `make typecheck` pass. All 40 source hashes in the M1 acceptance manifest still match. Production dependencies and kernel behavior were not changed by this experiment.

The worker receives a trusted read-only `/usr` tree, minimal devices and isolated proc. Scratch is `nosuid,nodev`, not `noexec`; some Linux host-wide metadata remains visible through proc. These are bounded fit results, not a general security audit or proof against kernel exploits.

Next implementation gate: a fixed installed-operation registry and narrow authenticated host launcher, declared-input staging and bounded output promotion, live supervisor/ancestor authority checks at dispatch/retry/resume, and durable intent/reconciliation across crashes. Resource budget reservation and provider accounting also remain open. Worker exit status never proves the task objective was achieved. MCP stays in M3; A2A stays in M4.
