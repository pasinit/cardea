# ADR-028: Configurable Worker Profiles

- **Date / owner / milestones:** 2026-10-15; kernel/host execution and runtime maintainers; ADR-022 gap #6 (C7).
- **Status:** selected and implemented for portable verification. The root helper, kernel executor and confined session adapter are in place, and portable tests pass. Debian enforcement probes are written but **not run**; nothing here is verified on Debian yet. The fixed M2 profile, its tests and its probes are unchanged.
- **Requirement:**
  1. **Confined operations.** An installed operation (ADR-023) may declare `executor: {kind: "confined", profile, worker}`. The profile and worker are host-owner configuration, never chosen by a caller. A profile sets:
     - memory cap, CPU quota, task count, wall time and writable scratch size;
     - a network policy: `none`, `model_api_only` or `allow_list` of destination hostnames.

     The handler runs as a separate process under systemd-run + bubblewrap with per-job cgroup limits. Its arguments arrive on stdin as JSON and its result comes back on stdout as JSON. Intent, receipt, idempotency scope and settlement are exactly those of an in-process operation. Cancellation, timeout and lease loss stop the whole process tree and clean up. A worker killed at its memory cap fails the call and leaves the service running. For an effect that means `outcome_unknown`, unless the worker never started. For a read it means `failed`.
  2. **Confined agent sessions.** The `ClaudeAgentSession` process (ADR-027) can run under a profile. Its network is limited to the model API and the loopback tool endpoint. It has its own memory cap. It cannot read the Cardea store, or any credential beyond its single one.
  3. **Admission.** A profile or worker that is not installed is refused at admission (`worker_profile_not_installed`), and again at dispatch.

## Candidates and primary evidence

Checked 2026-10-15. Versions are the pinned Debian 13 packages from ADR-012 unless stated.

| Need | Candidate | Fit |
|---|---|---|
| Process confinement and limits | **Extend the M2 systemd 257.13 + bubblewrap 0.12.0 composition** ([ADR-012](012-m2-host-confinement.md), LGPL-2.1-or-later). systemd owns the cgroup (`MemoryMax`, `MemorySwapMax=0`, `CPUQuota`, `TasksMax`, `RuntimeMaxSec`, `KillMode=control-group`, `OOMPolicy=kill`). bubblewrap owns the filesystem, PID and user namespaces. | **Selected, as the default.** Every limit the requirement names is already a systemd resource-control property. The M2 fit evidence covers the composition. Only the values change per profile, plus stdin/stdout files and network properties. No new launcher. |
| | [NsJail](https://github.com/google/nsjail) (Apache-2.0) | Untested alternative. It integrates namespaces, cgroups, rlimits and seccomp in one binary with protobuf configuration. Adopting it would replace a composition that already passes 11 hostile probes, and it would need its own fit run. Not rejected; no measured gap motivates it. |
| | [gVisor](https://gvisor.dev/) `runsc` (Apache-2.0) | Untested alternative. A user-space kernel gives stronger isolation for untrusted code such as a browser rendering hostile pages. It runs as an OCI runtime with its own netstack, so it would replace both bubblewrap and the network design. Revisit if browser workers handle untrusted content beyond read tools, or if a kernel escape becomes the dominant risk. |
| Network: deny | systemd `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, seccomp `~@network-io` | Selected for `none`. Unchanged from M2. |
| Network: allow some destinations | systemd [`IPAddressAllow=`/`IPAddressDeny=`](https://github.com/systemd/systemd/blob/v257/man/systemd.resource-control.xml) (cgroup eBPF) with `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | Selected as the **enforcement** layer, but only for IP addresses. It filters per unit cgroup, so it applies inside bubblewrap's namespaces too. It cannot express hostnames, and it has no port filter. |
| | nftables with `socket cgroupv2` matching, populated via systemd `NFTSet=` | Not selected now. It adds ports and per-unit sets, but still matches IPs, not names, and needs a host-owner ruleset. It is the revisit path for narrowing the loopback exposure below. |
| | **Egress forward proxy** (Squid or tinyproxy, both packaged in Debian, GPL-2.0-or-later) enforcing a hostname allow-list on `CONNECT` | **Selected for hostname correctness.** The worker can reach only the proxy's loopback address (systemd IP filter). It has no resolver: no `/etc/resolv.conf` is mounted, and every other IP is denied. The proxy resolves the name and connects, so the allow-list is enforced on names, and CDN or DNS changes cannot widen it. Squid's `dstdomain` ACL and tinyproxy's `FilterDefaultDeny` both do this. The proxy is host-owner configuration, not Cardea code. Exact package versions are recorded at acceptance. |
| Credential delivery to a session | systemd [`LoadCredential=`](https://github.com/systemd/systemd/blob/v257/man/systemd.exec.xml) | Selected. systemd reads a root-only file and exposes a copy only to the unit's user. bubblewrap binds that single file at `/run/cardea/credential`. The service process never holds the session credential. |

### How hostname allow-lists stay correct

systemd's IP filters work on addresses. A worker under `allow_list` or `model_api_only` may connect only to its profile's proxy (a dedicated `127.x.y.z` other than `127.0.0.1`). `model_api_only` also allows `127.0.0.1`, for the tool endpoint. DNS is unavailable: there is no resolver configuration, and port 53 on any address is outside the allowed IPs. So name resolution happens only at the proxy, which admits `CONNECT host:443` only for listed hostnames. The profile's `hosts` list is the single source for the proxy ACL. A host owner who changes one without the other is detectable by the Debian allow-list probe.

Residual: domain fronting. A client can `CONNECT` an allowed CDN hostname and then send a different `Host` header inside TLS. The connection still terminates at the allowed host's addresses. This is acceptable for read tools, and is noted for effect workers.

## Decision

1. **Profiles and workers are root-owned host configuration** in `/etc/cardea-host-workers.json`, which must be owned by root and not group- or world-writable. The root helper validates it strictly:
   - limits within fixed bounds;
   - proxies only on loopback, never on `127.0.0.1`;
   - credentials only under `/etc/cardea/credentials/`;
   - worker `argv[0]` and read-only binds only under `/usr/`, `/opt/`, `/etc/ssl/` or `/etc/fonts/`;
   - no overrides of reserved variables or proxy variables.

   A worker lists the profiles it may run under. The helper's new `profiles` action reports the limits, network policy and a per-profile digest. It never reports credential paths.
2. **Helper boundary.** One new fixed-shape action: `{action: "start_confined", job, profile, worker, deadline, input}`. The caller names a profile and a worker. It never sends a command line, mount, limit or network rule; any extra key is `invalid_request`. Unknown or unpermitted names are refused before the no-relaunch fence (`profile_not_installed`). `status`, `stop` and `cleanup` are unchanged and apply to confined jobs. The existing `start` action and fixed workers are untouched. The probe still compares the installed helper's SHA-256 with this source. This change alters the hash, so Debian hosts must reinstall the helper, and the probe refuses a stale one.
3. **The command** is the M2 composition with these values replaced:
   - `MemoryMax`, `TasksMax`, `CPUQuota`, `RuntimeMaxSec`. The wall time is the lower of the profile's and the deadline's.
   - The scratch tmpfs size and `LimitFSIZE`.
   - `StandardInput=file:` and `StandardOutput=file:` in the job directory.
   - `MemoryDenyWriteExecute=no` only for profiles that declare `jit`, which Chromium and Node need.
   - For networked policies: `IPAddressDeny=any` plus `IPAddressAllow` for the proxy (and `127.0.0.1` for `model_api_only`), and proxy environment variables. `PrivateNetwork` and the `@network-io` seccomp denial are dropped.
   - Optionally, one `LoadCredential`.

   bubblewrap keeps its minimal read-only root, `--clearenv`, `--disable-userns`, `--cap-drop ALL` and uid 65534.
4. **Kernel executor** (`service/confined.py`). The descriptor's `executor` replaces `handler`; a descriptor declares exactly one. `Kernel.handler()` returns a coroutine with the in-process handler signature, so the operation task and the agent tool path are unchanged.
   - The job ID is derived from the receipt ID. Recovery can find it, and the helper's tombstones refuse a replay.
   - The intent records `{profile, worker, profile_sha256}`.
   - The executor starts the job, then polls status and a watch: a cancel request, or a lease that is lost or expired.

   | Outcome | Result |
   |---|---|
   | Helper refused the start | `OperationRejected` → `failed`, even for effects |
   | `{"output": …}` | normal output verification |
   | `{"rejected": …}` | `OperationRejected` |
   | systemd `oom-kill` | `worker_memory_limit` |
   | systemd `timeout` | `worker_timeout` |
   | other failure or unknown start | `worker_failed` / `worker_start_unknown` |
   | cancel or lease loss | stop + cleanup, then `WorkerStopped` |

   Any started-worker failure of an effect becomes `outcome_unknown`. For a read it is `failed` with the code above, or `cancelled` on a cancel request. Cancellation and `asyncio` timeouts stop and clean up in a `finally`. Kernel recovery reaps the worker of any interrupted confined operation.
5. **Entry points inside the sandbox.**
   - `cardea.hosts.operation_worker <entry-point>` runs an installed `cardea.operations` handler from a read-only venv.
   - `cardea.hosts.session_worker` runs `ClaudeAgentSession` with the bound credential (`CARDEA_CREDENTIAL_KIND` = `api_key` or `oauth_token`).
6. **Confined sessions.** `agent_session` accepts `confined_profile` and `confined_worker` in place of a credential file. The session worker job is derived from the task and attempt, so attempt recovery stops it. The session request crosses the helper boundary, but no credential does. Admission refuses agent runs when the session profile is missing.

## Integration

- **Authority.** Unchanged. A profile grants no authority. The operation must still be granted under the live ceiling, and the profile is checked at admission and at dispatch.
- **State.** No new tables. The executor record lives in the intent's arguments. Job IDs are derived, not stored.
- **Compatibility.**
  - In-process descriptors keep their digest; the `executor` key is omitted while unset.
  - The M2 `start` action, fixed workers, limits and tests are unchanged.
  - The helper's request cap rises to 256 KiB, its status output cap to 256 KiB and its wall-time measurement cap to one hour. Fixed M2 workers cannot reach these, because their `LimitFSIZE=64K` and 8 s limits still apply.
- **Trust.** Worker code is installed by the host owner, as in-process handlers are. Confinement limits damage from bugs, memory blow-ups and hostile content, not from a malicious installer.

## Verification

Portable (macOS, Python 3.13): 58 new tests.
- `tests/hosts/test_confined_launcher.py` drives the helper's request handling with a fake `run`:
  - limits, stdin/stdout, proxy and credential properties reach systemd-run and bubblewrap;
  - `none` keeps M2's network denial;
  - profile wall time caps the deadline;
  - unknown or unpermitted names, and requests carrying argv or limits, are refused before the fence;
  - unsafe or invalid host-owner configuration is refused;
  - the `profiles` action never reveals credential paths;
  - the fixed M2 profile is unchanged;
  - the in-sandbox operation worker speaks JSON on stdin and stdout.
- `tests/service/test_confined.py` uses a fake `WorkerHost`:
  - limits and JSON passed through, with the executor pinned in the intent;
  - a memory-cap kill gives `outcome_unknown` for an effect and `failed` for a read, and the service keeps serving;
  - a never-started worker gives a definite failure;
  - cancel gives stop, cleanup and `cancelled`, or `outcome_unknown` for an effect;
  - timeout and lease loss give stop and cleanup;
  - crash recovery reaps the worker;
  - a missing profile is refused at admission and at dispatch;
  - a memory-cap kill inside an agent run is reported to the agent;
  - confined sessions: parsing, memory cap, cancellation and no credential in the request.

`make lint`, `make typecheck` and `make test` pass (451 API tests, 4 Debian probes skipped).

Debian-only: `tests/hosts/test_confined_debian.py`, skipped unless `CARDEA_CONFINED_DEBIAN=1`. It covers memory-cap kill, `none` denying all connections and DNS, the allow-list reaching only the proxy and only listed hosts, and no visibility of the store, helper configuration or credentials. **Not run.**

### Unverified until Debian acceptance

- `IPAddressAllow`/`IPAddressDeny` and `RestrictAddressFamilies` on a systemd-run transient unit whose process enters bubblewrap's user namespace.
- `LoadCredential=` on a transient unit, and bubblewrap (running as `cardea-worker`) binding `/run/credentials/<unit>/cardea`.
- `Result=oom-kill` reported for a `MemoryMax` kill under `OOMPolicy=kill`.
- `StandardInput=file:` from the root-owned job directory.
- Claude Code honouring `HTTPS_PROXY` and running with `MemoryDenyWriteExecute=no`. Note that the Agent SDK merges the parent environment into the CLI's, which `--clearenv` now bounds.
- Headless Chromium under bubblewrap. It will need `--no-sandbox` (nested user namespaces are disabled), `--disable-dev-shm-usage` and its fonts bound read-only. TasksMax and memory sizing for it are unmeasured.
- Proxy package versions and their ACL behaviour; native performance.

## Limits and open items

- **Loopback exposure.** `model_api_only` allows every TCP port on `127.0.0.1`, so a session can reach other loopback listeners. Other attempts' tool endpoints need their random bearer token, but unrelated host services bound to `127.0.0.1` are reachable. Narrowing to the endpoint port needs nftables `socket cgroupv2` rules (revisit).
- **Cleanup tracking.** If cleanup of a finished job fails, its spool entry stays until a later reap. There is no periodic sweep for confined jobs yet, and the 256-entry spool cap is shared with M2 jobs.
- **Profile changes need a restart.** The kernel caches the installed-profile listing at first use.
- **No budget accounting.** Confined operations are not charged to M2 host budgets; ADR-023 operations have none yet.

## Revisit

Revisit if:
- browser or other workers process untrusted content with effect authority (evaluate gVisor);
- loopback exposure matters (nftables `NFTSet=`/cgroup rules);
- Debian probes show any property above not enforced inside bubblewrap;
- profiles need hot reload.
