# Cardea API package

For new work, follow [open source first](../../docs/standards-first.md) and the [decision register](../../docs/standards-review-2026-09-22.md). Reuse maintained libraries and standard contracts before custom infrastructure; keep generated contracts faithful to implemented APIs.

Python package for the Cardea kernel (`cardea.kernel`), authenticated service (`cardea.service`), runtime adapters (`cardea.runtime`) and Debian host adapter (`cardea.hosts`).

`src/cardea/kernel` separately implements version 0.1 contracts, lifecycle checks, private SQLite persistence, atomic event history, and an owner-level administration CLI. `src/cardea/kernel/demo.py` supplies a trusted persistence test harness; the separate `cardea.service` now supplies authenticated deterministic DBOS execution. Run `make kernel-test`, `make kernel-schema`, or `make kernel-demo` at the repository root. The v0.2 Unix-socket service, SPIFFE/OIDC authentication, live task authority ceiling and backup/restore are implemented; see [M1 quickstart](../../docs/m1-quickstart.md). M2 includes bounded confined host execution. [M3](../../experiments/m3/README.md) implements verified OpenRouter/native MCP incident execution, including live GLM-4.7 restart/new-input acceptance. See [the persistence demo](../../docs/persistence-demo.md) for commands.

See the [design](../../design.md). Follow the [coding standard](../../coding-standard.md) and [kernel contract](../../docs/kernel-contract.md) before implementation. The kernel will own durable execution and authenticated capability checks; route handlers remain thin transports.

Use root `make` targets for installation, local execution, linting, type checks, tests, and schema export. M5 completes the supported Debian package/systemd profile, bounded upgrades, fenced restore, diagnostics and explicit retention; see [installation and operations](../../packaging/README.md) and [acceptance](../../experiments/m5/M5-REPORT.md).

### Bounded Debian host operation

The optional `host_execution` configuration enables installed `host.sha256` and `host.copy` operations through `/v0.2/host-tasks`, `KernelClient.submit_host` and CLI `submit-host`. It requires explicit supervisor/delegation/platform grants, provisioned `host_budgets` and the fixed Debian helper. Discovery exposes permitted operation descriptors; `/v0.2/host-budget` reports the caller’s allocation, consumption and cleanup backlog. See [setup and limits](../../experiments/m2/INTEGRATION.md) and [M2 acceptance](../../experiments/m2/M2-REPORT.md). Existing M1 defaults remain host-disabled. Arbitrary commands are not accepted.

For an existing immutable object, use `/v0.2/host-object-tasks`, `HostObjectTaskRequest`, `KernelClient.submit_host_object` or CLI `submit-host-object`. This path requires exact-version `object_permissions` and `object_platform` provisioning in addition to namespace rights; possession of the object reference grants nothing. See [stored input setup](../../experiments/m2/INTEGRATION.md#hash-an-existing-stored-object) and [ADR-014](../../docs/decisions/014-object-scoped-host-inputs.md).

## Bounded delegation (M4)

With the same Debian `host_execution` profile, `POST /v0.2/delegation-tasks`, `KernelClient.submit_delegation` and CLI `submit-delegation` start an installed coordinator with 1–4 independent inline hash/copy child jobs. A separate logical worker agent is discovered and called through the native A2A SDK over the private socket. The original supervisor needs explicit `tasks.delegate` plus the requested operation/object rights; parent and supervisor allocations both apply. Inspect immutable child associations and usage through `/v0.2/tasks/{id}/children` or CLI `children`. See the [request example and limits](../../experiments/m4/README.md) and [acceptance report](../../experiments/m4/M4-REPORT.md). No model key is required for this profile.
