# ADR-023: Installed Operation Registry

- **Date / owner / milestones:** 2026-10-15; runtime and broker maintainers; ADR-022 gap #1 (G3).
- **Status:** selected and implemented for a bounded first increment: in-process operations declared by host-owner configuration. Portable tests pass; no live provider operation is installed yet. The existing host, incident and delegation profiles are unchanged. Confined-worker operations from configuration are deferred to the worker-profile decision (ADR-022 gap #6). They are added by [ADR-028](028-configurable-worker-profiles.md).
- **Requirement:** a host owner can install, version and remove operations without kernel code changes. Each operation declares its schemas and effect class. A task can invoke only installed operations that its live authority ceiling grants. Every invocation keeps the existing intent → receipt → verification contract, and an interrupted effect is never replayed.

## Problem

Operations are kernel code today. `service/host_operations.py` names two operations and computes their expected output with an if/elif chain. The `Right` literal in `service/models.py` enumerates every grantable operation, so provisioning cannot even name a new one. `service/profiles.py` derives one task profile per hard-coded host operation. Recurring assistant routines (ADR-022) need email, search and recall operations. Company workloads need their own operations. Neither should change the kernel.

## Candidates and primary evidence

Checked 2026-10-15 against the pinned dependencies in `apps/api/pyproject.toml`.

| Need | Candidate | Fit |
|---|---|---|
| Descriptor vocabulary | [MCP `Tool`](https://modelcontextprotocol.io/specification) (`name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`), via the already-pinned `mcp==2.2.0` types | Selected for naming and schema fields. Item 4 serves these operations to agents over MCP, so one vocabulary avoids a translation layer. The `ToolAnnotations` hints (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) are recorded but **confer nothing**: the MCP specification says clients must treat them as untrusted, and this ADR keeps them descriptive. |
| Schema language | JSON Schema Draft 2020-12 via the pinned `jsonschema==4.26.0` | Selected. Already used to validate MCP tool input in M3. |
| Handler discovery | [Python entry points](https://packaging.python.org/en/latest/specifications/entry-points/) (`importlib.metadata`, group `cardea.operations`) | Selected. Handlers come only from packages installed in the service's own root-owned environment; configuration names an entry point, never a module path or command. |
| Plugin framework | [pluggy](https://github.com/pytest-dev/pluggy) (MIT) | Not needed. One handler per operation needs no hook calling or ordering; entry points are the underlying mechanism pluggy also uses for discovery. |
| Interface description | OpenAPI 3.1 operations | Describes HTTP endpoints, not effect classes or receipts. Revisit if operations are later exposed as HTTP APIs. |

## Decision

1. **Descriptor.** An installed operation is a JSON file in the configured `operations_dir`, owned by the service account and mode `0600`, like the service configuration. It holds:
   - the MCP tool fields: `name`, `title`, `description`, `input_schema`, `output_schema`, `annotations`;
   - Cardea's enforced fields:
     - `version`;
     - `effect`: `read` or `effect`;
     - `handler`: an entry-point name in `cardea.operations`;
     - `timeout_seconds`;
     - `max_output_bytes`;
     - `requires`: extra kernel rights such as `objects.read`.
2. **Identity.** The descriptor's RFC 8785 digest is written into every operation intent next to the policy revision, the same way `launcher_sha256` pins host workers. Changing a descriptor changes the digest of new intents, never existing receipts.
3. **Rights.** A grantable right is a kernel right (the existing literal) or the name of an installed operation. Provisioning, admission and every ceiling check validate installed names against the live registry. Casbin policy rows already store operation names as strings, so no policy migration is needed. A removed operation can no longer be granted or dispatched; its history remains.
4. **Task.** A new `OperationTaskRequest` names one installed operation plus its arguments, which are validated against `input_schema` at admission. It is admitted through `POST /v0.2/operation-tasks`. Its profile (`operation-v1`) uses a new DBOS workflow with one non-retried step.
5. **Execution.** One intent per task (`operation_id="execute"`), committed under a dispatch lease before the handler runs, as on the host path.
   - The handler is called with validated arguments and a context (task, receipt ID, deadline), bounded by `timeout_seconds`.
   - Success requires output that validates against `output_schema` and fits `max_output_bytes`. The output and the verification evidence are published in the settling transaction.
   - A handler may raise `OperationRejected` to state that no effect happened. That settles as FAILED.
   - Any other exception or a timeout:
     - `effect` operations become `outcome_unknown` and, once the lease is fenced, the task fails with `reconciliation_required`;
     - `read` operations fail without uncertainty.
   - A task found already RUNNING on entry, meaning the process died after the intent, is fenced through the kernel's existing recovery for both effect classes. Its operation becomes `outcome_unknown` and is never replayed. Retrying interrupted reads is left to ADR-022 gaps #2 and #4.
   - Retry and reconciliation semantics for effects (idempotency scopes, destination checks) are ADR-022 gap #2 and get their own record.
6. **Discovery.** `GET /v0.2/operations` lists installed operations the caller is authorized for, alongside the fixed ones, with schemas and effect class.

## Integration

- **Authority.** No change to the ceiling rule: `policy.ceiling(task, name)` is evaluated for the operation and each `requires` right at admission, dispatch and settlement.
- **State.** Existing `tasks`, `operations`, `task_bindings` and `objects` tables. No new tables in this increment.
- **Compatibility.** Existing routes, profiles, runtime IDs, receipts and v0.1 digests are untouched. The `Right` literal stays for kernel rights. Wire types that carried it now accept an installed name as an `OperationName` string validated against the registry.
- **Trust.** Handlers run inside the service process with its privileges. The host owner is responsible for what is installed. This is why this increment admits in-process handlers only from installed packages. Untrusted or heavy code waits for configurable confined workers.

## Verification

The increment ships with portable tests:
- registry loading and refusal of malformed, duplicate or unsafely-owned descriptors;
- an unknown operation refused at provisioning and at admission;
- argument schema refusal;
- an installed operation denied without a grant, and when the supervisor ceiling lacks it;
- revocation after admission, and uninstallation after admission, both stopping dispatch;
- a timeout on an `effect` operation leading to `outcome_unknown`;
- success with output and evidence published;
- output schema violation leading to `verification_failed`;
- `OperationRejected` leading to FAILED;
- a handler crash on an `effect` operation leading to `outcome_unknown` plus `reconciliation_required`, with no replay on recovery;
- a duplicate request returning the same task;
- discovery filtered by authority.

Result (2026-10-15, macOS, Python 3.13, DBOS 3.0.0): 28 new tests in `apps/api/tests/service/test_operations.py` pass, and the full `make test`, `make lint` and `make typecheck` pass. Not covered: an end-to-end DBOS workflow run of an installed operation inside the real daemon. The workflow is registered and the `make try` daemon starts with it, but no test submits an operation task through it yet. Debian acceptance is not needed for in-process operations.

## Revisit

Revisit if:
- MCP changes its tool descriptor fields;
- in-process handlers need isolation (move them to confined workers);
- operation versions need side-by-side installation (today one version per name).
