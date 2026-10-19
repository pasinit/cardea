# Installed host operation acceptance — 2026-09-24

**Result: the bounded `host.sha256` path is implemented and verified on Debian.** An authenticated supervisor can submit declared JSON, run the fixed worker under systemd/bubblewrap, and retrieve a verified digest with durable receipts. M2 remains in progress for broader operations, resource scopes and accounting.

## What changed

- Separate `/v0.2/host-tasks`, typed `KernelClient.submit_host` and CLI `submit-host`; existing M1 request defaults and digests remain unchanged.
- Opt-in Debian adapter and exact-command sudo helper. The daemon runs as `cardea-kernel`; workers run under a separate account and private namespaces. The helper accepts fixed actions and bounded data, not commands, paths or unit properties.
- Live supervisor/delegation/platform checks at admission, dispatch, monitoring and publication. Dispatch carries a policy-expiry deadline, checked again by the worker before processing input.
- Durable intent with input binding, launcher digest, profile, policy revision and a full fixed reservation. Interrupted dispatch becomes `outcome_unknown` and is never automatically launched again. Disabling the adapter with unfinished host tasks is rejected.
- Confirmed cancellation settles the operation and task as cancelled. Independently verified output, evidence, receipt and successful task state commit together; a successful exit with a false digest fails.

## Executed validation

| Suite | Result | Evidence |
| --- | --- | --- |
| Portable host authority, API, duplicate, verifier, cancellation and crash cases | 23 passed | [XML](evidence/integration/portable.xml) |
| Real SPIRE → unprivileged daemon → sudo helper → Debian worker | 7 passed | [XML](evidence/integration/debian.xml), [log](evidence/integration/debian.log) |
| Hostile fixtures against the production command builder | 11 passed | [JSON](evidence/integration/production-fit.json) |
| Existing M1 real-provider service regressions | 38 passed | [XML](evidence/integration/m1-regression.xml) |
| Repository backend/frontend tests | 167 + 4 passed | Recorded in manifest |
| Repository lint/type checks and experiment checks | Passed | Recorded in manifest |

The seven Debian scenarios cover successful verified output and duplicate/conflicting requests; daemon SIGKILL after durable intent, after launcher start and before settlement; revocation between intent and launch; cancellation before launch; and the fixed sudo-command boundary, invalid path/operation requests and expired dispatch. The portable suite separately tests false-success output, live policy denial at each boundary, post-intent revocation, policy-expiry attenuation and disabled-adapter recovery protection.

The production-profile probes reuse the eleven hostile tests from ADR-012. Root-only instrumentation replaces the fixed worker with each attack fixture and disables successful-unit retention so the original harness can observe exit; isolation and resource settings come from the production builder. An additional check confirms the worker account cannot invoke the privileged helper. This instrumentation is not installed in the helper or exposed through the API.

## Environment and provenance

Debian 13.7 amd64, kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, sudo `1.9.16p2-3+deb13u2`, Python 3.13.5, SPIRE 1.15.3 and DBOS 3.0.0. This is a real Debian kernel inside software-emulated QEMU on an arm64 Mac, hosted by unprivileged Docker. It is not native performance evidence.

The [manifest](evidence/integration/manifest.json) records the cloud image, guest packages, exact implementation/test/config hashes and validation counts. The final guest sources were checked against the frozen local sources. No active test units remained after collection; the owned VM was stopped. Private VM/provider credentials remain outside the repository.

See [ADR-013](../../docs/decisions/013-installed-host-operations.md) for the reuse decision and [setup instructions](INTEGRATION.md) to reproduce the run. The initial systemd-only failure and original host-fit reports remain unchanged as historical evidence.

## Remaining limits

Only `host.sha256` is installed. Inputs are inline declared JSON; arbitrary commands/plugins, other filesystem resources and child host tasks are rejected. Tool accounting charges the entire fixed 10-second reservation conservatively; it is not measured wall-time billing, a refund mechanism or shared-budget accounting. The host separately caps runtime at 8 seconds plus a 1-second stop timeout.

The root spool caps retained jobs at 256 and fails closed when full; automatic retention is pending. The kernel account is trusted infrastructure and must not be shared with untrusted native clients. Runtime `/usr` is read-only and must contain no credentials. Scratch is `nosuid,nodev`, not `noexec`; some host-wide proc metadata remains visible. These tests do not establish immunity to kernel exploits, native throughput, full-OS reboot recovery or production packaging/upgrades. Missing or changed required host facilities reject dispatch rather than silently weakening the profile.
