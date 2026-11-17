# ADR-038: Throwaway Test Environments for Agent Teams

- **Date / owner / milestones:** 2026-11-16; kernel/host execution and runtime maintainers. Roadmap gap T9. Builds on ADR-012 and ADR-028 (host confinement and worker profiles), ADR-023 (installed operations), ADR-026 (receipts, scopes and reconciliation), ADR-029 (run evidence), ADR-031 (context operations), ADR-034 (records, state machines and receipt evidence), ADR-030 (the small-host profile) and [ADR-037](037-coding-and-vcs-operations.md) (coding and version-control operations, proposed in parallel), whose repository aliases this record reuses.
- **Status:** accepted, 2026-11-16. The owner answered every open question as recommended (see [Decisions](#decisions-owner-2026-11-16)). The process tier is selected as the default. Implemented with portable tests only (2026-11-16): the ADR-034 receipt-result guard, and `env.try` in the process tier with the run table, the window, the rerun limit and the toolchain image make target; see [Implementation status](#implementation-status). Nothing is verified on Debian, and no fit test below has run. The Debian acceptance script is written and unrun.
- **Requirement:** an agent team, typically a developer, a reviewer and a product-manager agent, needs evidence that a given commit actually works before it is accepted. Evidence from reading the diff is not enough. One installed operation, `env.try`:
  1. builds or installs a given commit of a given repository into a throwaway, confined instance;
  2. runs named scenarios against it, such as the repository's `make try`, getting-started use cases, end-to-end checks and `make teams`;
  3. returns bounded evidence: pass or fail per scenario, a log tail, and artifacts stored as Cardea objects with a receipt, which a reviewer or product-manager agent can cite on a record;
  4. tears everything down under a hard cap on time and resources, whatever the outcome.

  Constraints:
  - **The commit is untrusted code.** It was written by an agent. Building it and running its scenarios executes whatever the repository's build files say. Nothing it runs may reach the Cardea store, the host's credentials, other jobs or the network beyond an allow-list.
  - **No caller supplies a command line.** Callers name a repository, a commit and scenarios from host-owner configuration, as ADR-028 workers never accept argv from a caller.
  - **Same input, same receipt.** The same commit and scenario set within a window gives the same receipt, rather than spending the cap again.
  - **Concurrency is bounded per host.** It is a property of the host, not of any group's budget.
  - **Authority does not widen.** Running `env.try` is an ordinary installed operation under the live ceiling (ADR-010). A run's grants do not widen because a test passed.

## Candidates and primary evidence

Checked 2026-11-16. Each version is the latest upstream release published on GitHub that day, unless the row says otherwise. Debian 13 package versions are recorded at acceptance, as ADR-012 does. A row marked "untested" has not been run here and is not rejected.

| Candidate | What it offers | Fit for this requirement |
|---|---|---|
| **ADR-028 confined worker**: systemd + bubblewrap ([bubblewrap](https://github.com/containers/bubblewrap) v0.13.1, 2026-10-14, LGPL-2.0-or-later per its source; [systemd](https://github.com/systemd/systemd) v262.2 upstream, Debian 13 ships 257, LGPL-2.1-or-later) | Already selected and implemented for installed operations. systemd owns the cgroup (memory, CPU, tasks, wall time, whole-tree kill) and bubblewrap owns the namespaces and a minimal read-only root. Networking is denied, or limited to a loopback egress proxy that enforces a hostname allow-list. The root helper accepts only fixed-shape requests. ADR-012's hostile probes and ADR-028's portable tests cover the composition. | **Strongest reuse; it has known gaps.** (a) The commit shares the host kernel, so a kernel escape is outside its threat model, which ADR-028 already names as gVisor's revisit trigger. (b) There is no init or systemd inside, so scenarios that need their own cgroups or confinement cannot run nested; Cardea's own confined-worker probes are one example. (c) Scratch is a RAM tmpfs, too small for real builds without a disk-backed, size-capped workspace. |
| **systemd-nspawn** (part of systemd, same versions and license; [manual](https://github.com/systemd/systemd/blob/v262.2/man/systemd-nspawn.xml)) | `--ephemeral` runs a throwaway snapshot of a root directory or image, which is discarded at exit. It can boot an init inside, and its network options include `--private-network`. It runs as a systemd unit with the same resource controls. | **Untested.** It closes gap (b) for scenarios that need an init inside, with no new dependency. It still shares the host kernel and needs root to start, so it would go behind a new fixed-shape helper action. It also needs a host-owner root image. |
| **OCI containers, rootless**: [Podman](https://github.com/containers/podman) v6.2.0 (2026-11-10, Apache-2.0) with [crun](https://github.com/containers/crun) 1.31 (2026-11-12, GPL-2.0) or [runc](https://github.com/opencontainers/runc) v1.5.3 (2026-11-12, Apache-2.0) | Standard images and registries, `--rm`, rootless user namespaces, cgroup v2 limits and no-network modes. The OCI runtime specification is a standard. | **Untested.** It fits if scenarios need many prebuilt toolchain images. But it brings a second image and lifecycle manager next to systemd, and it does not change the kernel boundary. ADR-012 left OCI runtimes as untested alternatives; nothing measured here requires one yet. |
| **gVisor** `runsc` ([gVisor](https://github.com/google/gvisor) release-20261109.0, 2026-11-11, Apache-2.0; [platforms](https://gvisor.dev/docs/architecture_guide/platforms/)) | An OCI runtime with a user-space kernel, which narrows the host kernel's exposure to untrusted code. It does not need hardware virtualization. | **Untested.** It addresses gap (a) on hosts without virtualization. Its costs: system-call compatibility, the overhead of file-heavy builds, and its own network stack in place of ADR-028's proxy design. It needs a fit run with this repository's own builds and scenarios. |
| **MicroVMs**: [Firecracker](https://github.com/firecracker-microvm/firecracker) v1.18.0 (2026-11-09, Apache-2.0, with its `jailer`), [Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor) v54.1 (2026-11-11, Apache-2.0 or BSD-3-Clause), QEMU's `microvm` machine (GPL-2.0, already used for ADR-012's disposable Debian guest) | Each job gets its own guest kernel and init, a disk image that is deleted afterwards, and virtio devices. That closes gaps (a) and (b), and a fixed-size disk image also closes (c). | **Untested.** This is the strongest boundary for untrusted commits, but every option needs KVM (`/dev/kvm`). Small cloud VMs often offer no nested virtualization, so this cannot be the only tier. The costs are a guest kernel and root image, guest-to-host transfer of evidence (for example vsock), and boot time; each needs measuring. |
| **Kata Containers** ([kata-containers](https://github.com/kata-containers/kata-containers) 4.3.1, 2026-10-21, Apache-2.0) | An OCI runtime that runs each container in a lightweight VM (QEMU, Cloud Hypervisor or Firecracker). | **Untested.** It adds microVM isolation to an OCI image workflow. Like the microVMs it needs KVM, and it adds the container stack on top. It is worth considering only if both images and microVMs are chosen. |
| **Incus** ([incus](https://github.com/lxc/incus) v7.6.0, 2026-10-30, Apache-2.0) | Manages system containers and VMs, with ephemeral instances, snapshots and resource limits behind a daemon and API. | **Untested; probably too much.** It is a second control plane that owns instance lifecycle and state, which ADR-012 keeps with systemd and the helper. |
| **NsJail** ([nsjail](https://github.com/google/nsjail) 3.6, 2026-03-18, Apache-2.0) | Namespaces, cgroups, rlimits and seccomp in one binary. | **Untested alternative**, as in ADR-012 and ADR-028. No measured gap motivates replacing the current composition. |
| **Hosted CI runners** (for example a forge's hosted runners) | Repository-native workflows, logs and artifacts. | **Reference only.** They are not open source, not under the host's confinement or ceiling, and they need forge credentials. A deployment may still use them; their results could enter Cardea as an ordinary operation's receipt. |

## Fit evidence

None has run yet. These are the fit tests a later slice must run on the supported Debian host, with this repository as the workload:

1. **Process tier (ADR-028 composition)**, with a disk-backed workspace capped by a fixed-size image:
   - check out the commit, then `uv sync --locked` through the egress proxy;
   - `make try`, `make teams` and the getting-started tests;
   - wall time, peak memory and workspace bytes;
   - hostile probes from ADR-012 plus: build-file attempts to reach the store, the helper socket, other jobs' workspaces, metadata addresses and unlisted hosts;
   - teardown after a kill at every stage.
2. **Init-inside tier**: the same scenarios plus one that needs systemd inside (Cardea's own confined-worker probes), under `systemd-nspawn --ephemeral` and under one microVM on a KVM host.
3. **Kernel-boundary tier**: gVisor on a host without KVM, measuring compatibility failures and build overhead against tier 1.

Do not select a tier, or call one unsuitable, before its fit test has run.

## Proposed decision

Compose what exists. `env.try` is an installed operation (ADR-023) whose executor is an ADR-028 confined worker. It adds a host-owner **environment tier** and **scenario catalog**, and one fixed-shape helper action for staging source. The tiers:

- **`process`** (the default, on every supported host): the ADR-028 systemd + bubblewrap composition, with a disk-backed workspace.
- **`init`** (proposed second tier, after its fit test): `systemd-nspawn --ephemeral`, or a microVM where KVM exists. It is for scenarios that need their own init or cgroups, or a host owner who wants a kernel boundary for untrusted commits.

A tier is host-owner configuration, never a caller's choice. A scenario that needs `init` is refused on a host without it (`env_tier_unavailable`), not silently run in a weaker tier.

### 1. The call

```json
{"repo": "cardea", "commit": "<40 hex>", "scenarios": ["try", "teams", "getting-started"], "rerun": false}
```

- **`repo`** is a repository alias from host configuration, the same aliases ADR-037 proposes for `code.session` and `vcs.*`. A caller never sends a URL.
- **`commit`** is a full object ID. Branches, tags and abbreviations are refused, because a name can move and the receipt must name exactly what ran.
- **`scenarios`** are names from the host-owner catalog, 1 to 8 of them.
  - Each catalog entry fixes: the argv run inside the instance (for example `["make", "try"]`), the tier it needs, a per-scenario time limit, and the artifact paths to keep.
  - The commands come from the catalog, while what `make try` does comes from the commit. That is why the commit is treated as untrusted.
- **`rerun: true`** skips the result window (item 4) and records a new receipt that links to the earlier one. It is for flaky scenarios, and it is bounded by the cap and per-host concurrency.

Grants: the operation name, as any installed operation, plus `objects.write` for its artifacts, and the repository's `repo:<alias>` grant if ADR-037's resource-qualified repository grants are adopted (its open question 4).

### 2. Lifecycle

1. **Stage** (helper, outside the instance):
   - fetch the commit into a fresh job directory, with a host-owner read-only deploy credential if the repository needs one;
   - verify that the checked-out object ID equals `commit`;
   - make the tree read-only.

   The credential never enters the instance.
2. **Build and run** (inside the instance):
   - a writable workspace copy, on a fixed-size disk image formatted per job, so the size is a hard cap;
   - dependency install through the profile's egress proxy, whose allow-list holds the package indexes and nothing else;
   - each scenario in order, with its own time limit, under one overall wall-time cap.
3. **Collect**: per-scenario exit status and duration; a scrubbed log tail of at most 4 KiB per scenario; and the catalog's artifact paths, each at most 256 KiB and 1 MiB in total. Anything over a limit is dropped and the drop is recorded.
4. **Tear down**:
   - kill the whole cgroup (`KillMode=control-group`, or the VM process);
   - unmount and delete the workspace image and job directory;
   - leave a helper tombstone so the job cannot be replayed.

   This runs on success, failure, cancellation, lease loss and timeout. A host restart cleans up stale jobs on the helper's next `cleanup`, as ADR-028 already does.

### 3. Confinement and egress

- **Process tier.** This is ADR-028 unchanged, with a new host-owner profile (for example `try`):
  - memory, CPU, task and wall-time caps;
  - the `allow_list` network policy, with only the package indexes the catalog's builds need;
  - no credential;
  - no access to the store, the helper socket or other jobs' directories;
  - uid 65534, `--disable-userns`, `--cap-drop ALL`.

  `public_web` is not allowed for this profile: a build needs named indexes, not the open web.
- **Workspace.** The fixed-size image is created and mounted by the root helper and bound into the instance, replacing the RAM tmpfs scratch. Its size is the disk cap; a full disk fails the scenario, not the host.
- **Init tier.** The same egress design. The instance sees only the proxy, and DNS goes nowhere else. The fit test decides between systemd-nspawn and a microVM. A microVM's disk is a per-job copy-on-write image, deleted at teardown.
- **What a scenario may not do:** start services reachable from outside the instance, keep anything after teardown, or send mail, records or tool calls into Cardea. It is a closed box that returns evidence.

### 4. Idempotency: the same commit and scenarios give the same receipt

- **Run key.** The SHA-256 of the RFC 8785 encoding of `{namespace, repo, commit, sorted scenarios, environment digest}`. The environment digest covers the profile digest (ADR-028), the tier, the catalog entries' digests and the toolchain image digest. Changing any of them is a different run.
- **Result window.** A host-owner `result_window_seconds` (proposed default 24 hours). A call whose run key has a settled run inside the window returns that run's result, marked `cached: true` and naming the original receipt ID. Nothing runs and no cap is spent.
- **Effect class.** `env.try` is classified `effect: "read"`, because nothing outside the throwaway instance changes. A crash mid-run is therefore retryable rather than `reconciliation_required`: the evidence is recomputed, and the old instance has been torn down. Artifacts are written only in the settling transaction, so a crash leaves no partial evidence.
- **Why this needs a small custom piece.** ADR-026 scopes deduplicate *effects*: a read operation cannot declare one, and a scope never expires. Declaring `env.try` an effect would turn every crash into `reconciliation_required` for something that changed nothing. The smallest addition is a run table keyed by the run key, written in the intent and settlement transactions:
  - columns: state (`running` or `settled`), receipt ID, settled time;
  - a call finds a settled row inside the window and returns its result; a `running` row gets `env_run_in_progress` (retry later), so identical concurrent calls never start a second instance;
  - an expired row lets a new run be recorded.

  Reusing ADR-026 instead, by allowing scopes on reads with an expiry, is the alternative; see the open questions.

### 5. Concurrency and caps per host

- **Per-host concurrency.** This is ADR-028's per-profile worker slots, with a proposed default of 1 for the `try` profile and a host-owner maximum. A call over the limit waits up to the profile's queue allowance, then fails with `executor_busy`. The kernel's per-host and per-profile limits are the only scheduler; there is no separate queue.
- **Hard caps per run.** Wall time (the lower of the catalog total, the profile and the task deadline), memory, CPU quota, task count, workspace bytes and artifact bytes. Each is a systemd property or a fixed image size, not a counter Cardea keeps.
- **Spend.** `env.try` uses no model budget. An agent run that calls it pays only for its own session (ADR-034 spend). Host resources are capped by the profile, not by group spend.

### 6. Evidence and how it flows back

- **Result**, bounded to the 64 KiB receipt document (ADR-029):

  ```json
  {"repo": "cardea", "commit": "...", "environment_sha256": "...", "cached": false, "passed": true,
   "scenarios": [{"name": "try", "outcome": "pass", "exit_code": 0, "duration_ms": 41210,
                  "log_tail": "...", "log_truncated": false,
                  "artifacts": [{"resource_id": "...", "version": 1, "sha256": "...", "bytes": 1834}]}],
   "dropped": {"artifacts": 0, "log_bytes": 0}}
  ```

  `outcome` is one of `pass`, `fail`, `timeout` or `error` (the instance failed before the scenario). Log tails are untrusted data, are scrubbed of anything that looks like a credential (ADR-026's summary rule), and are never instructions.
- **Artifacts** are Cardea versioned objects in the task's namespace, written by the kernel with the receipt. A reader reaches them through `objects.read` under its own ceiling.
- **To the calling agent:** the tool result, with the log tail inside ADR-031's untrusted-data block when it reaches a prompt.
- **To records (ADR-034).** The receipt is the citation.
  - A transition such as `submit` or `accept` can declare `requires_receipt: {operation: env.try, in: same_task}` (slice 3), so a developer cannot submit, and a reviewer cannot accept, without a fresh passing run of the named scenarios.
  - Whether a receipt *passed* is a guard over the cited receipt's result (for example every scenario's `outcome == "pass"`). This is proposed as a small extension of slice 3's receipt evidence, because today it checks only that a receipt exists; see the open questions.
- **To later runs (ADR-031).** A read-only context operation, `env.result {repo, commit}`, returns the latest receipt's summary for that commit within the window. A product-manager agent's run template can then start every attempt with the current evidence, under the same byte budgets as other context blocks.

### 7. Relation to ADR-037

ADR-037's `code.session` already runs a repository's own gates (lint, typecheck, test) inside the confined session that writes the change, and returns their results to the developer. `env.try` is separate, and is for whoever checks the change:
- it starts from a pushed commit, not a live workspace, so a reviewer or product-manager agent can run it without the developer's session;
- it runs product scenarios (`make try`, getting-started use cases, end-to-end checks, `make teams`), not only the unit gates;
- its evidence is deduplicated per commit and scenario set and cited by receipt.

Both are confined workers under ADR-028 with the same repository aliases; they share the staging and teardown design and differ in profile and catalog.

## Integration

- **Authority:** an installed operation under the live ceiling. A repository, a tier and a scenario are host-owner configuration, so a supervisor can only choose among what the host offers. Approval rules (ADR-036) apply as to any operation, though one is rarely needed because nothing outside the box changes.
- **State ownership:** the helper owns jobs, workspaces and tombstones; systemd owns the cgroups; the kernel owns intents, receipts, the run-key scope and the artifacts. Nothing in an instance survives teardown.
- **Failure handling:**
  - lease loss, cancellation or timeout kills the whole instance;
  - a crash before settlement is retried as a read;
  - an instance killed at a cap reports `timeout` or `error` for the running scenario, and the remaining scenarios are reported as not run.
- **Compatibility:** new descriptor, profile, catalog and helper action only. No v0.1 record changes. The helper's hash changes, so hosts reinstall it (ADR-028's probe enforces this).
- **Operational cost:** one build at a time per host by default. Disk use is the workspace image while a run lasts, plus artifacts; logs and artifacts follow normal object retention.

## Implementation status

### `env.try`, process tier (implemented 2026-11-16, portable tests only)

- **Package** `packages/cardea-env-ops`: the `env.try` descriptor and its worker entry point.
  - **Descriptor:** `effect: read`, confined profile `try`, worker `env-try`, a one-hour timeout, `repo:<alias>` on `/repo`, artifacts up to 1 MiB, and the run window below.
  - **Worker** (`cardea_env_ops/try_.py`): clones the alias's read-only `source` from the shared repository registry, with git hooks and global configuration off, and checks out exactly the 40-hex `commit`. A missing commit is `env_commit_unknown`. It then runs each named scenario's fixed argv in the checkout under the call's deadline. Each scenario's environment is built from nothing:
    - the pinned toolchain first on `PATH`;
    - a scratch `HOME`;
    - the profile's proxy;
    - the catalog's fixed variables, never credential-shaped names.
  - **Each scenario reports** `pass`, `fail`, `timeout`, `error` or `not_run`, with:
    - its exit code and duration;
    - a log tail of at most 4 KiB, scrubbed of credential shapes;
    - its artifact paths, at most 256 KiB each, 1 MiB and 8 files in all, returned as artifacts the kernel stores as objects.

    Anything over a limit is dropped and counted.
  - **The output** carries `passed` (every scenario passed), `cached`, the catalog and toolchain digests, and the scenarios.
  - **Refusals:** a scenario that needs the `init` tier is `env_tier_unavailable`, and a name outside the catalog is `env_scenario_unknown`.
- **Catalog.** Each repository-registry alias may carry `environment`, which holds:
  - `tiers`;
  - `toolchain` (`path` under `/opt/`, ending with its `sha256`);
  - `scenarios`, each with `argv`, `timeout_seconds`, `tier` and `artifacts`, the artifact paths relative to the checkout and refused if they leave it;
  - fixed `env`.

  Its digest joins the run key.
- **Run window** (decisions 4 and 5). A generic descriptor binding `run_window`, allowed on read operations only, implemented in `cardea.service.env_runs` and migration `m18_env_runs`:
  - **Tables.** `env_runs` holds one row per run key. `env_reruns` holds one row per forced rerun.
  - **Run key.** The SHA-256 of `{namespace, operation, repo, commit, sorted scenarios, environment}`. The environment digest covers the confined worker's profile digest and the alias's catalog digest, toolchain included.
  - **In the intent's transaction:**
    - a running row refuses an identical call (`409 env_run_in_progress`);
    - a settled row inside 24 hours answers with its output, `cached: true` and `cached_receipt_id`, recorded as a deduplicated receipt without starting a worker;
    - `rerun: true` skips the window, at most 3 times per namespace, repository and commit in 24 hours (`429 env_rerun_limit`), and the new receipt records `rerun_of`.
  - **At settlement.** Success keeps the row. A failure or an interrupted call deletes it, so the call can be made again. A running row past its call's deadline plus five minutes is ignored.
- **Toolchain image** (decision 6). `make try-toolchain` runs `packaging/try_toolchain.py`:
  - `build` runs `mmdebstrap` for a Debian 13 minbase root with `SOURCE_DATE_EPOCH` set to the ISO week's start, and unpacks it read-only under its SHA-256. It keeps the last two images and any pinned one.
  - `pin` writes the path and digest into every registry `environment`, atomically.
  - The kit's `cardea-try-toolchain.timer` refreshes the image weekly.
- **Kit** (`deploy/small-host/cardea`):
  - the `try` profile: `allow_list` through squid port `127.0.10.7` to `pypi.org` and `files.pythonhosted.org`, no credential, concurrency 1, 4 GiB of memory, 6 GiB of scratch and one hour of wall time;
  - the `env-try` worker, which binds read-only the kernel environment, the registry, the mirrors and the toolchain directory;
  - an example catalog in `repositories.json`.
- **Tests:**
  - `test_env_try.py` (19 tests) runs the real worker command locally against a fixture with a good and a bad commit. It covers:
    - pass and fail;
    - log scrubbing and artifacts, including dropped ones;
    - no planted credential reaching a scenario;
    - the window, including reordered scenarios, other inputs, a changed catalog and a forced rerun linked to the first;
    - the rerun limit per commit;
    - an identical call in progress, and a stale row;
    - failed runs that are not reused;
    - unknown scenarios and unavailable tiers;
    - refs, short IDs, argv and URLs refused before anything starts;
    - the repository grant;
    - registry path checks;
    - a passing receipt satisfying `result.passed == true` while a failing one does not.
  - `test_try_toolchain.py` covers the image script without running `mmdebstrap`, and the kit's profile, worker and timer.
  - The acceptance-script tests cover `env_try.py`.
- **Not done:**
  - **A disk-backed, fixed-size workspace image** (decision 2.2). The checkout and builds use the profile's `scratch_mb` tmpfs, so the disk cap is the scratch size and lives in memory. A helper action to create, mount and delete a per-job image is the next slice.
  - **Staging by the helper** with a deploy credential. The worker clones from the registry's read-only mirror, so a repository that needs a credential to fetch is not supported yet.
  - **The `init` tier.**
  - **`env.result`** as a context operation.
  - **Any Debian run.** `experiments/debian-acceptance/env_try.sh` is written and unrun.

## Out of scope

- Deploying anywhere real. There are no preview environments that outlive the call, and no promotion of artifacts to releases.
- Secrets inside scenarios, interactive shells, and debugging sessions in the instance.
- Scenarios that need GPUs, other operating systems, or more than one host.
- Build caches shared across runs. Each run starts clean; a cache is a later decision with its own poisoning analysis.
- Hosted CI integration. A deployment may wrap its CI as another operation.
- Performance benchmarking. Timings are reported but are not a reliable signal on shared, capped hosts.

## Acceptance tests (to write; none run)

1. **Pass and fail.** A commit whose `make try` passes and one that breaks it give `pass` and `fail` with exit codes, bounded log tails and artifacts as objects. Nothing remains on the host afterwards.
2. **Same receipt.** The same commit and scenarios inside the window return the first receipt (`cached: true`) without starting an instance. A new commit, a changed catalog entry or `rerun: true` runs again.
3. **Confinement.** A hostile commit's build tries to read the store, reach the helper socket, open another job's workspace, resolve DNS, reach a metadata address, reach an unlisted host and fill the disk. Every attempt fails and the host is unaffected (ADR-012 probe style).
4. **Caps.** Wall-time, memory, task and workspace limits each end the run with the documented outcome, and teardown completes.
5. **Teardown under failure.** Kill the service during staging, building and collection: the next `cleanup` removes every trace, and a retried call recomputes the evidence.
6. **Concurrency.** With concurrency 1, a second distinct call waits and then fails with `executor_busy`. An identical concurrent call gets `env_run_in_progress` and never starts a second instance.
7. **Records.** A transition that requires a passing `env.try` receipt is refused without one, refused with a failing one (once the extension exists), and allowed with a passing one.
8. **No command lines.** A call naming an unknown scenario or repository, or sending argv or a URL, is refused before anything starts.

## Verification

None. This record is proposed; no fit test or acceptance test has run, and nothing is implemented.

## Revisit

- The process tier's shared kernel becomes the dominant risk: select the init or kernel-boundary tier as the default where hosts allow it.
- Scenarios need prebuilt images at scale: evaluate the OCI path (Podman with crun or runc, or Kata where KVM exists).
- Builds need network beyond package indexes: reconsider per scenario, never by giving the profile `public_web`.
- A forge's CI becomes the team's source of truth: wrap it as an operation instead of duplicating it.

## Decisions (owner, 2026-11-16)

The owner answered every question as recommended.

1. **Default tier: the process tier.** `env.try` is available on every supported host, in the ADR-028 systemd + bubblewrap composition. The `init` tier stays a later option after its fit test, and a scenario that needs it is refused where it is missing (`env_tier_unavailable`).
2. **Repository grants: `repo:<alias>`.** `env.try` declares the resource grant `{grant: repo, argument: /repo}` and reads its repositories from the shared host repository registry (`CARDEA_REPOSITORIES`, ADR-037), as `code.session` and `vcs.*` do.
3. **The kernel enforces the guard.** ADR-034's receipt evidence gains a guard over the cited receipt's result ([ADR-034 amendment](034-agent-groups-and-shared-records.md#amendment-2026-11-16-guards-over-the-cited-receipts-result)). A transition such as `accept` declares `requires_receipt: {operation: env.try, in: same_task, result: "result.passed == true"}`, and is refused unless it cites a passing `env.try` receipt. `env.try` therefore reports a top-level `passed`.
4. **Window and reruns.** The result window defaults to 24 hours. `rerun: true` is limited to 3 forced reruns per commit per day, counted per namespace, repository and commit over the preceding 24 hours; a fourth is refused with `env_rerun_limit`.
5. **A run table, migration m18.** The run key, state and receipt live in a small dedicated table (item 4). ADR-026 is not amended.
6. **Toolchain images.** The host owner builds and pins the toolchain root image with a make target, refreshed weekly. The image digest is part of the environment digest, and so of the run key: a refreshed image is a different run.

## Open questions for the owner (answered 2026-11-16)

1. **Default tier.** Is the process tier acceptable as the default for agent-written commits, given the shared kernel? Or should `env.try` stay unavailable on hosts without an init or microVM tier?
2. **Repository grants.** Should `env.try` follow ADR-037's answer on `repo:<alias>` grants, or is the operation grant plus the host catalog enough for an operation that changes nothing outside its box?
3. **Pass/fail guards on records.** Should ADR-034's receipt evidence grow a guard over the cited receipt's result, so "accept only after a passing run" is enforced by the kernel rather than by the reviewer agent?
4. **Window and reruns.** Is a 24-hour default window right, and should `rerun` be limited per commit, to bound flaky-test churn?
5. **Run table or ADR-026 amendment.** Is a small run table (item 4) acceptable, or should ADR-026 scopes be amended to allow reads with an expiry window?
6. **Toolchain images.** Who builds and pins the toolchain root image (for example with `mmdebstrap` on Debian), and how often is it refreshed? Its digest is part of the run key.
