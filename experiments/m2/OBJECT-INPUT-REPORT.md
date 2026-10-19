# Exact-version host input acceptance — 2026-09-24

**Result: stored-object input to `host.sha256` is implemented and verified on Debian.** An authenticated supervisor can select one existing immutable object version, grant its exact read scope, and receive an independently verified digest from the confined worker. M2 remains in progress.

## Behavior

- New `/v0.2/host-object-tasks`, typed `HostObjectTaskRequest` / `KernelClient.submit_host_object`, and CLI `submit-host-object`. Existing inline requests and digests are unchanged.
- The source reference binds namespace, object ID, version and content digest. A newer version cannot change admitted input. The receipt records the reference; the worker receives only its validated bytes, capped at 16 KiB.
- Existing Casbin exact ACLs enforce source-namespace and exact-version supervisor allow/delegate plus platform allow. The task explicitly names its only source. No namespace-only fallback, wildcard, latest-version grant, child execution or caller path exists.
- Admission, dispatch, monitoring and publication recheck the live ceiling. Source-policy expiry attenuates the worker deadline. Revocation prevents new publication; already committed task outputs retain the existing task access policy.
- Existing SQLite ownership, request provenance, DBOS workflow, fixed helper and systemd/bubblewrap isolation are reused. No dependency or database migration was added. The privileged helper's bytes match ADR-013.

## Executed checks

| Suite | Result | Evidence |
| --- | --- | --- |
| Portable host tests, including 35 new stored-input cases | 58 passed | [XML](evidence/object-inputs/portable.xml) |
| Authenticated Debian integration, including 8 new stored-input cases | 15 passed | [XML](evidence/object-inputs/debian.xml), [log](evidence/object-inputs/debian.log) |
| Hostile fixtures against the unchanged production confinement profile | 11 passed | [JSON](evidence/object-inputs/production-fit.json) |
| Existing M1 real-provider service suite | 46 passed | [XML](evidence/object-inputs/m1-regression.xml) |
| Repository backend / frontend tests | 202 / 4 passed | [Log](evidence/object-inputs/repository-tests.log) |
| Repository lint / types and M2 experiment checks | Passed | [Lint](evidence/object-inputs/lint.log), [types](evidence/object-inputs/typecheck.log), [experiment checks](evidence/object-inputs/experiment-checks.log) |

Portable cases cover namespace/object/version/digest substitution; supervisor, delegation and platform withdrawal or expiry at admission, dispatch, post-intent and publication boundaries; cross-namespace requirements; policy replacement and atomic rejection; missing/corrupt/oversized bytes; unchanged inline digests; source-policy deadline attenuation; and interrupted intents without relaunch.

The eight new Debian cases create a source through the authenticated M1 API, grant its exact artifact reference and run the real SHA-256 worker. They reject alternate object/version references, revoke each of supervisor allow, supervisor delegate and platform allow before launch and before publication, and kill the daemon after stored-input dispatch. The recovered receipt is `outcome_unknown`, without verified output or automatic replay. The seven original host cases also pass.

## Environment and scope

One Debian 13 amd64 VM hosts the tested Cardea stack and all jobs; workers are isolated processes on that host. Kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, sudo `1.9.16p2-3+deb13u2`, Python 3.13.5, SPIRE 1.15.3 and DBOS 3.0.0. The QEMU environment runs through unprivileged Docker on an arm64 Mac. Its timings do not establish native throughput.

The [manifest](evidence/object-inputs/manifest.json) records exact sources, checked guest hashes, package inventory and final validation results. All 48 guest source files checked matched the local snapshot. No active Cardea test units remained, and the owned test VM was stopped after collection. Earlier ADR-012/013 reports and evidence remain unchanged. See [ADR-014](../../docs/decisions/014-object-scoped-host-inputs.md) for alternatives and the bounded reuse decision, and [setup](INTEGRATION.md#hash-an-existing-stored-object) for usage.

Only stored-version read input is added. General filesystem handles, resource discovery, output grants, additional installed operations, shared budgets, refunds/measured tool billing, spool retention, real models, production packaging and full-machine reboot recovery remain outside this acceptance. Missing host guarantees still fail closed.
