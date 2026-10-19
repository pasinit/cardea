# M2 — Authority and Debian execution acceptance

Date: 2026-09-24. **M2 complete for the fixed Debian execution profile.** All required reuse decisions and behavioral acceptance gates passed.

## Delivered profile

One persistent Debian machine hosts the authenticated kernel, DBOS, SPIRE, SQLite storage, fixed privileged helper and isolated worker processes. Debian is the system platform; no VM is created per job. The acceptance VM substitutes for that whole machine during development on macOS.

The installed registry exposes `host.sha256` and `host.copy`, each with inline JSON or one explicitly granted immutable object-version input. Output is independently verified and published only into the task's authorized output namespace/name. No caller command, path, mount, plugin or verifier is accepted. Source/resource grants, supervisor allow/delegate, platform policy, identity enablement and current allocations gate admission and execution. Workers receive bounded bytes; they cannot traverse the kernel store or credentials.

## Exit requirement matrix

| Requirement | Implementation and acceptance |
| --- | --- |
| Scoped grants, expiry, revocation, authority-filtered discovery | Casbin exact namespace/action and immutable-version ACLs; separate operation grants; discovery reports installed descriptors only under current authority/capacity. Portable boundary/substitution tests and Debian source-revocation cases. |
| Task authority never exceeds current supervisor/ancestor authority | Existing M1 provenance/ancestor enforcement and real-provider regressions; M2 live supervisor disablement, read/delegate/platform withdrawal and expiry. Host child bindings are explicitly rejected until M4. |
| Fixed installed operation registry and thin host adapter | Two fixed workers, two input modes, typed API/client/CLI, independent byte/digest verification; Debian copy/hash success and denied arbitrary command/path/operation tests. |
| Cross-scope input/output denial | Exact source namespace/object/version/digest grants; task-owned destination names; namespace/object/version/digest substitutions denied. Worker sees only staged input and private scratch. Broad ambient host paths are outside the MVP boundary. |
| Filesystem, symlink, network and credential isolation | Production command builder passes hostile probes for read-only mounts, secret/storage reachability, symlink escape, network and Unix socket denial, namespace/privilege restrictions and scratch bounds. |
| Process-tree cancellation, deadlines and resource limits | Systemd cgroup controls and bubblewrap namespaces; real cancellation/disablement after dispatch, detached-descendant termination, CPU throttling, memory/process/output bounds and wall deadline probes. |
| Unsupported hosts fail explicitly | Pinned kernel/package/facility probe and installed-helper digest binding; unsupported platform and malformed/expired helper requests fail, with no unconfined fallback. |
| Host/admission R09 accounting | SQLite writer-serialized supervisor allocations and task-keyed reservations committed with intent. Concurrency/exhaustion tests, observed execution/output settlement, unused-capacity release, unknown full charges, and no reset on reprovisioning. |
| Durable effects and cleanup | Crash tests before/after launch and before settlement preserve unknown outcomes without relaunch. Terminal cleanup confirms stop, commits a helper tombstone, then deletes staging. Failed/lost cleanup acknowledgments retry; active units and symlink roots are refused. |
| Migration and recovery compatibility | Alembic `m2_001` preserves v0.1 receipts and request digests, imports historical host charges conservatively, and rolls back interrupted migration. Backup/restore includes the budget tables; M1 snapshots require explicit migration. |

## Validation results

| Suite | Result |
| --- | --- |
| Repository backend / frontend | 221 / 4 passed |
| Portable host and service storage/contract tests | 83 passed |
| Existing M1 real SPIRE/OIDC service regressions | 46 passed |
| Authenticated Debian integration | 20 passed in 514.52 seconds |
| Hostile production-profile probes | 11 passed |
| Repository lint/type checks and M2 experiment checks | Passed |

Evidence: [manifest](evidence/completion/manifest.json), [Debian results](evidence/completion/debian.xml), [hostile probes](evidence/completion/production-fit.json), [portable tests](evidence/completion/portable.xml), [M1 regressions](evidence/completion/m1-regression.xml), and [guest inventory](evidence/completion/guest-inventory.txt). The portable suite is a subset of the repository backend tests, not an additional 83 distinct tests. Earlier ADR-012/013/014 evidence remains unchanged.

The run used Debian 13.7 amd64, kernel `6.12.107+deb13-cloud-amd64`, systemd `257.13-1~deb13u1`, bubblewrap `0.12.0-1~deb13u1`, Python 3.13.5 and SPIRE 1.15.3. The manifest records the cloud-image SHA-512 and 58 frozen source/config/test SHA-256 values; all 51 uploaded source files in that manifest were checked against the guest after acceptance. There were zero active Cardea units after the tests. The owned disposable VM was then stopped and removed. The pytest warning concerns Authlib’s deprecated compatibility module; it did not skip or fail acceptance.

## Reuse decisions and scope disposition

[ADR-010](../../docs/decisions/010-m1-authenticated-kernel.md) supplies the authenticated policy/store/workflow integrations; [ADR-012](../../docs/decisions/012-m2-host-confinement.md) selects the Debian confinement composition; [ADR-013](../../docs/decisions/013-installed-host-operations.md) establishes intent and host integration; [ADR-014](../../docs/decisions/014-object-scoped-host-inputs.md) establishes exact-version input scopes; [ADR-015](../../docs/decisions/015-m2-accounting-retention-and-acceptance.md) closes the installed registry, accounting and retention decisions. Reused mechanisms include Casbin, DBOS, SQLite/SQLAlchemy/Alembic, sudo, systemd, bubblewrap and Python's standard library. No new runtime dependency was added.

The earlier broad “resource scopes / accounting / retention” placeholders are satisfied for the MVP's declared inputs and task-owned output, cumulative local host allocations and recoverable staging retirement. They do not require unrestricted filesystem paths or arbitrary plugins: the [MVP boundary](../../mvp.md#boundary) explicitly selects a fixed registry and excludes broad host filesystem access. The acceptance matrix above defines observable closure rather than claiming every possible tool or policy is supported.

M3 still owns real model/provider accounting and native MCP tool interaction. M4 owns host child execution, transitive shared child budgets and A2A delegation. M5 owns production packaging/upgrades, full-machine reboot/disaster recovery and long-term history retention. The kernel account and host administrator remain trusted. Tombstones and durable audit/object metadata are retained; deleting those to reclaim space is not supported. Administrative restore returns the policy/account snapshot to backup time and requires reconciliation of later activity before operational reuse. Emulated timings are not native throughput or a kernel-vulnerability audit.

See [current setup](INTEGRATION.md) for allocation provisioning, migration, helper installation and usage.
