# ADR-013 — Installed host operations

Date: 2026-09-24. Owner: kernel/host integration. Milestone: M2.
Status: bounded implementation complete; acceptance evidence recorded in the linked integration report. M2 remained in progress at this decision. Subsequently completed for the fixed Debian profile; see the [M2 acceptance report](../../experiments/m2/M2-REPORT.md) and [ADR-015](015-m2-accounting-retention-and-acceptance.md).

## Requirement and selection

An authenticated supervisor submits a bounded root task invoking the installed `host.sha256` operation. Its input is the RFC 8785 encoding of declared JSON; the isolated worker returns its SHA-256 and byte count. No caller supplies executable code, argv, paths, unit properties or a verifier. Current task, supervisor, delegation and platform authority must permit dispatch and publication. Unknown interrupted dispatch cannot silently run again.

Reuse the systemd 257.13 + bubblewrap 0.12 composition verified in ADR-012 (LGPL-2.1-or-later), Casbin 1.43 exact namespace/operation policies from ADR-010 (Apache-2.0), DBOS 3.0 durable workflows/steps (MIT), SQLAlchemy/Alembic/SQLite transactions, and existing v0.1 receipts. These versions and licenses are recorded in ADR-010/012 and the lockfile. [DBOS step semantics](https://docs.dbos.dev/python/reference/decorators) checkpoint outcomes but cannot atomically commit an OS process and SQLite; Cardea must persist intent and reconcile that gap. No new scheduler, policy engine, namespace implementation or hash algorithm is justified.

Privilege candidates: raw systemd D-Bus/polkit grants allow service properties wider than one installed operation; a fixed sudo command can authorize one root-owned helper without granting arbitrary systemd access. Select sudo's maintained Debian implementation (ISC-style license; exact package recorded in Debian validation), with a root-owned Python standard-library helper and fixed JSON command schema. [Sudoers](https://www.sudo.ws/docs/man/sudoers.man/). A network launcher service is unnecessary for one local host and remains unevaluated. The custom scope is operation validation, staging, fixed systemd configuration and durable kernel reconciliation; neither sudo nor systemd knows Cardea's supervisor provenance.

## Integration and bounds

The helper receives only a random task identifier, bounded input bytes and a start/status/stop action through stdin. The kernel account alone can invoke its exact command; workers have a separate locked account. The service remains unprivileged. Its authenticated handlers, current Casbin decisions and private SQLite store retain authority; possession of helper privileges is not a task grant. The root-owned spool has a hard entry cap and a fixed per-job input/output bound; administrative retention is explicit.

Expose a separate `/v0.2/host-tasks` request, preserving existing M1 request defaults/digests and workflow history. Host operation grants are explicit, not added to M1 defaults. The first profile accepts root tasks only, one execution per task, no event wait and no arbitrary retries/plugins. Child budget sharing remains M4. A unique durable intent reserves the full fixed execution/output budget before dispatch. SQLite writer serialization orders final authorization plus launcher invocation against policy updates; workers have an independent systemd wall limit. Check current authority again during monitoring and before publication. Revocation/cancellation triggers whole-cgroup stop; a daemon outage cannot disable the host deadline.

A crash after intent is recorded triggers stop/reconciliation, never a second start. Unknown outcomes stay explicit in existing receipt semantics. Successful exit is insufficient: the kernel recomputes the expected digest from the bound input before publishing output and verification evidence atomically.

## Acceptance and remaining gates

Require portable denial, duplicate/conflicting request, cancellation, revocation, input/output verification and restart tests; run the actual helper as an unprivileged kernel account in the Debian VM. Repeat host confinement tests for the production profile. Record exact evidence before marking verified. General resource scopes, shared budgets, arbitrary operations, packaging upgrades, stronger confidentiality and full reboot recovery remain separate gates.

## Implemented behavior and evidence

See the [component design](../components/installed-host-operations.md), [setup](../../experiments/m2/INTEGRATION.md), and [integration report](../../experiments/m2/INTEGRATION-REPORT.md). No new dependency, scheduler or database table was introduced. Existing M1 request defaults and workflow identity remain unchanged; host tasks use a separate DBOS workflow and explicit operation grant. The privileged helper is Python standard-library configuration around the selected host facilities.

Intent records bind the installed launcher SHA-256, profile, policy revision and full fixed reservation. Runtime monitoring checks live authority; dispatch carries the earliest required policy expiry, and the worker checks the absolute expiry before processing data. The service refuses to disable the host adapter while unfinished host tasks require reconciliation. Confirmed cancellation settles the operation as cancelled; interrupted dispatch remains unknown and cannot replay. Output and verification commit with success in SQLite.

The capped spool retains job identities and results; automatic cleanup is not implemented. Charged tool time is the entire reservation, not a measured elapsed-time invoice. General resource scopes, arbitrary plugins, child/shared budgets, quota refunds, native performance, production packaging and full-OS reboot recovery remain unverified or unimplemented. These are explicit remaining gates, not claims satisfied by the single-operation acceptance.
