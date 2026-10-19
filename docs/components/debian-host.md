# Debian Host Adapter

**Reuse decision.** [ADR-012](../decisions/012-m2-host-confinement.md) selects Debian systemd + bubblewrap after a real Debian VM fit. Reuse their cgroup, namespace and seccomp mechanisms; keep Cardea code to boundary integration. See [R07](../standards-review-2026-09-22.md#r07--host-execution--m2).

**Purpose.** Enforce the execution boundaries promised by the kernel using real host facilities.

**Deployment.** One persistent Debian machine hosts the Cardea services, durable storage and workers. Jobs run as isolated processes sharing that machine's Linux kernel. Debian is the platform for the whole system, not an OS image booted for each job. The disposable VM in the acceptance harness stands in for this machine when developing on macOS.

**Design.** Target Debian 13 amd64 with systemd and cgroup v2 under the verified fixed `debian-local-v1` profile. A narrow launcher creates unprivileged workers with granted read-only inputs, private scratch space, controlled outputs, process/mount/network isolation, and resource limits. Default worker networking is denied. Keep service storage, host secrets, and provider credentials outside worker reach.

**Boundary and recovery.** Probe required facilities before accepting the profile; missing enforcement rejects dispatch. On timeout, cancellation, or recovery, terminate/reconcile descendant processes and classify uncertain effects. The host owns native processes; Cardea owns durable task state.

**Acceptance.** Debian tests demonstrate symlink/path and network denial, inaccessible secrets, descendant termination, and enforced resource limits.

**Status.** The bounded composition passes 11 host probes (see the [report](../../experiments/m2/REPORT.md)); the first fixed operation now integrates authenticated host dispatch, bounded input/output handling and conservative daemon-crash recovery under [ADR-013](../decisions/013-installed-host-operations.md). ADR-014/015 extend this to exact-version inputs, a second installed operation, transactional budgets and durable payload retirement. See [M2 acceptance](../../experiments/m2/M2-REPORT.md) and [installed host operations](installed-host-operations.md) for verified scope and remaining limits. M1 Debian container acceptance establishes only the trusted userspace service profile; neither it nor macOS tests establish Debian worker isolation. Other Unix adapters require their own conformance evidence.
