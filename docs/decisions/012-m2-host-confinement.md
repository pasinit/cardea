# ADR-012 — M2 Debian host confinement fit

- Date: 2026-09-24; owner: kernel/host execution; gates: R07 and host portion of R09.
- Status: systemd + bubblewrap selected for the bounded Debian host integration, following the executed fit below. Production adapter and M2 acceptance remained incomplete at this decision. Subsequently completed for the fixed Debian profile; see the [M2 acceptance report](../../experiments/m2/M2-REPORT.md) and [ADR-015](015-m2-accounting-retention-and-acceptance.md).

## Requirement

Run fixed installed operations as unprivileged workers with only declared inputs, private scratch, bounded outputs, no kernel storage/provider credentials, denied network access, enforced CPU/memory/process/deadline limits and whole-descendant cancellation. Missing enforcement must reject the profile. Host status/exit is not objective verification. Kernel integration must retain authenticated supervisor/ancestor ceilings and durable intent/reconciliation.

## Candidates and primary sources

| Candidate | Reuse and remaining fit question | License / maintenance |
| --- | --- | --- |
| Debian systemd transient services + cgroup v2 | Native lifecycle, process-tree cancellation, resource accounting, mount/network restrictions, RootDirectory, credentials/users and seccomp; test whether these suffice without another launcher | LGPL-2.1-or-later core, component exceptions; Debian-maintained package, exact guest version recorded after provisioning. [Execution](https://github.com/systemd/systemd/blob/v257/man/systemd.exec.xml), [resources](https://github.com/systemd/systemd/blob/v257/man/systemd.resource-control.xml) |
| bubblewrap + systemd | Fine-grained unprivileged mount/user/PID/network namespaces; requires a separate cgroup/lifecycle owner and a complete policy | LGPL-2.1-or-later; upstream v0.13.0 released 2026-09-22. [Source and limitations](https://github.com/containers/bubblewrap), [releases](https://github.com/containers/bubblewrap/releases) |
| NsJail | Integrated namespaces/cgroups/rlimits/seccomp with protobuf configuration; compare privilege/setup and supervision needs before adding a separate launcher | Apache-2.0; maintained upstream, exact release not selected. [Source](https://github.com/google/nsjail) |
| OCI runtime (runc/crun) | Standard container configuration and lifecycle; candidate if image distribution or broader workloads require it | Apache-2.0 / GPL-2.0-or-later respectively; version/operational fit unrun. [OCI runtime spec](https://github.com/opencontainers/runtime-spec), [runc](https://github.com/opencontainers/runc), [crun](https://github.com/containers/crun) |

First fit: systemd-only configuration. No candidate is rejected merely for unfamiliarity; introduce another launcher only for a measured missing guarantee. Recheck exact package provenance and security status before production selection. Upstream documentation at v257 matches Debian's base family; actual package fixes are recorded separately.

## Environment and acceptance

M1's Debian 13.7 container runs on Docker Desktop's LinuxKit kernel, with read-only cgroup mounts and namespace creation denied. It is not the M2 host. Use a disposable Debian 13 amd64 QEMU guest with its own kernel, systemd and writable cgroup v2. QEMU runs without privileged Docker mode, host cgroup mounts or host device access. Pin the cloud image digest and record guest packages/kernel; do not interpret emulated timing as native performance.

Probe success, input-read/output bounds, forbidden storage/credential access, path/symlink and host-socket/network denial, unprivileged execution, child-process cancellation, timeout, memory/process limits and observable CPU quota. Retain machine-readable results and exact profile configuration. Resource attacks must be confined to the disposable guest and bounded transient units.

## Integration and compatibility

This record precedes the disposable fit harness. It does not change v0.1 persisted records, v0.2 API or the M1 runtime. A successful host fit alone does not close M2: installed registry, live authorization at dispatch/retry/resume, durable reservations/intents and crash reconciliation still require production integration and denial tests. Native macOS cannot advertise this profile.

## Revisit

Revisit on missing enforcement, Debian/systemd/kernel upgrades, arbitrary plugins, allowed networking, persistent output mounts, privilege separation changes or broader host support. Add results and selection status only after executed checks.

## Initial fit finding

The systemd-only profile passed the first nine probes, but the expanded scratch-capacity probe failed: DynamicUser's implicit PrivateTmp overrode the requested 1 MiB temporary mount, including when PrivateTmp=no was explicit. The mounted hierarchy also retained host cgroup metadata. The raw failed run is retained in `experiments/m2/evidence/systemd-only-gap.json`. Do not treat the initial subset as full acceptance. Evaluate bubblewrap's explicit filesystem/PID/user namespace configuration inside systemd's resource-controlled service, keeping cgroup ownership and cleanup with systemd. This is the concrete gap motivating composition rather than custom namespace code.

## Executed composition and decision

The combined profile passes 11 probes in Debian 13.7, including the previously failing scratch-capacity check. See the [report](../../experiments/m2/REPORT.md) and exact source/environment evidence. Select Debian systemd 257.13-1~deb13u1 plus bubblewrap 0.12.0-1~deb13u1 for the bounded host integration. No custom namespace, seccomp compiler or cgroup manager is needed. NsJail/OCI remain untested alternatives, not rejected implementations.

The outer service uses a dedicated unprivileged system account. DynamicUser implies RestrictSUIDSGID, whose systemd filter returns ENOSYS for openat2; bubblewrap 0.12 requires that syscall for safe path resolution. This was reproduced in the guest and confirmed in [systemd's filter implementation](https://github.com/systemd/systemd/blob/v257/src/shared/seccomp-util.c) and [bubblewrap's mount setup](https://github.com/containers/bubblewrap/blob/v0.12.0/bubblewrap.c). Do not enable bubblewrap's non-security-boundary fallback. Systemd proc masking also interfered with bubblewrap's proc mount; the selected configuration gives filesystem/PID isolation to bubblewrap rather than layering conflicting outer mount controls. NoNewPrivileges, empty capability bounds, read-only/nosuid mounts and tested privilege denial retain the privilege boundary.

Systemd retains CPU/memory/process accounting, deadline enforcement, seccomp socket denial and whole-cgroup cleanup. Bubblewrap supplies a minimal read-only root, declared inputs, isolated proc/dev, a 1 MiB scratch mount and nested-user-namespace denial. The exact configuration is the fit artifact, not an agent-controlled property dictionary. Production selection must pin/check these facilities, fail closed on unsupported hosts and constrain dispatch to fixed installed operations.

Remaining gates: authenticated narrow launcher boundary, input staging and output promotion, installed operation registry, live resource/tool grants and supervisor/ancestor ceilings, durable dispatch intents/reservations and restart reconciliation. A host exit code is still not completion evidence. R07 integration, R06 and the accounting portion of R09 remain open; M2 is in progress.
