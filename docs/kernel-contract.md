# Kernel Contract

The [open-source-first review](standards-review-2026-09-22.md) governs implementation selection. Implemented version 0.1 semantics below remain accurate; proposed operations are requirements, not frozen wire contracts or authorization to build bespoke infrastructure. [ADR-001](decisions/001-m0-contracts.md) closes M0’s R01–R03 selection gate; [ADR-010](decisions/010-m1-authenticated-kernel.md) subsequently completed the authenticated endpoint and migrations.

Status: version 0.1 records, lifecycle checks, and local SQLite persistence implemented on 2026-09-21. A trusted deterministic persistence demonstration runs across processes. The separate service implements bounded authenticated execution (ADR-010), confined `host.sha256`/`host.copy` operations with exact-version stored input (ADR-013–015), a bounded OpenRouter/MCP agent task (ADR-016), A2A delegation (ADR-017) and Debian packaging (ADR-018/019); general tool/application execution remains incomplete.

## Implemented Contract Layer

`apps/api/src/cardea/kernel` contains Pydantic records, deterministic transition functions, and a trusted transactional store independent of the legacy application. Run `make kernel-test` for contract/storage tests, `make kernel-schema` for JSON Schema, and `make kernel-demo` for a two-process persistence check. The separate `cardea.service` package now implements the bounded authenticated v0.2 daemon described in [ADR-010](decisions/010-m1-authenticated-kernel.md). The trusted v0.1 store remains unchanged; this is not yet a confined autonomous agent OS.

Records serialize `schema_version: "0.1"`, reject unknown versions and extra fields, normalize aware timestamps to UTC, and use strict nonnegative resource counters. Omitted versions currently default to 0.1; clients should include them explicitly. JSON documents are limited to 65536 encoded bytes and reject nonfinite numbers. JSON Schema describes shapes; Python validation additionally enforces cross-field invariants such as time ordering and state/evidence consistency.

Task transitions check expected revision and time, require a live bounded lease to enter `running`, require checkpoints to yield, and require matching verifier records to enter `succeeded`. Terminal tasks cannot reopen. Operation receipts bind the entire request to a digest, retain uncertain effects, and require reconciliation references to resolve them. Recorded usage cannot decrease across transitions.

The pure functions are checks for trusted in-process callers. Identity/grant records do not authenticate or authorize anyone, and a verifier record alone does not prove an artifact was inspected. The SQLite store adds transactional revision comparisons and state/event commits, while the installed demo verifier reads actual artifact bytes. A future authenticated broker must establish the remaining authority and execution guarantees. Frozen models do not deeply freeze nested JSON containers; transition and retry functions revalidate and return isolated snapshots.

The version 0.1 request digest is SHA-256 of the full normalized request encoded as sorted-key, compact ASCII JSON with finite numbers. Identity, task, operation ID/name, resource, arguments, and deadline are bound. Object key order is ignored, array order is preserved, and `1` differs from `1.0`. This is not RFC 8785 canonicalization. [ADR-001](decisions/001-m0-contracts.md) retains these semantics for v0.1 and selects JCS only for an explicit future schema/digest version; existing persisted hashes must retain their original semantics. Matching a retry only returns the existing receipt; it neither checks current grants nor dispatches the operation.

Host profile/version selection, initial runtime adapter selection, and the authenticated wire/session contract are recorded in [runtime decisions](runtime-decisions.md). This design is distinct from implementing those adapters or an authenticated service.

## Implemented Persistence Layer

`SQLiteKernelStore` provides a trusted transaction API and a local owner-level administration CLI. Storage schema version 1 is independent of record schema version 0.1. SQLite WAL/FULL transactions persist task snapshots, unique lease IDs, operation intents/receipts, bounded object versions/content, and an append-only event journal together. Unknown database identities/schema versions are rejected. Small objects are capped at one MiB and their bytes commit with metadata and events; separate large-file publication is deferred.

Task writes compare revisions; object writes compare expected versions. Running-worker mutations require the current unexpired lease. Checkpoints currently require settled operations and existing referenced object versions. Event-wait registration checks already-committed matching events in its write transaction; explicit wakeup checks the persisted cursor. There is no background event scheduler yet.

An expired-worker recovery transaction cancels accepted-but-undispatched operations and marks dispatched operations uncertain. Uncertain effects fail the task for reconciliation; resolving a receipt does not reopen that task. A safe recovery with no uncertain effects may make the exceptional `running -> queued` transition, preserving the checkpoint and removing the expired lease. The pure transition function still forbids that transition outside this recovery path. No external processes are killed or external effects replayed by the store.

See [the persistence demo](persistence-demo.md) for commands, current limitations, and crash-test coverage. The store must remain inside a trusted service; its producer/grant labels and namespace references are not access enforcement.

## Core Objects

| Object | Meaning |
|---|---|
| Agent identity | Authenticated principal independent of a model name or supplied actor string. |
| Capability grant | Issuer, subject, operation/resource scope, expiry, limits, delegation allowance, and revocation state. |
| Task | Durable logical process with objective, parent, runtime, grants, budget, state, checkpoint, and result criteria. |
| Operation | One typed request with stable ID, authenticated subject, task, schema version, parameters, and deadline. |
| Receipt | Dispatch/result record with argument digest, authority decision, effect status, usage, and verification evidence. |
| Object/artifact | Namespaced versioned data with owner, access policy, content reference/hash, and provenance. |
| Event | Durable ordered journal item with scope, cursor, correlation, type, and bounded payload/reference. |
| Checkpoint | Explicit resumable task state, input versions, event cursor, and outstanding operation references. |
| Budget | Reserved and consumed model/tool resources, wall deadline, output bounds, and concurrency ceiling. |

## Supervisor Authority Ceiling

Required invariant. The trusted v0.1 store does not authenticate callers; the M1 service enforces this ceiling for its installed operations with current transactional Casbin decisions. ADR-013 extends enforcement to one fixed Debian host operation; [ADR-014](decisions/014-object-scoped-host-inputs.md) adds exact-version source read/delegation/platform grants at admission, dispatch, monitoring and publication. [ADR-015](decisions/015-m2-accounting-retention-and-acceptance.md) adds a separately granted copy operation and live host allocations. [ADR-016](decisions/016-m3-openrouter-and-native-mcp.md) added explicit model invocation/disclosure, installed tool and additive-input grants to the bounded M3 profile, which [ADR-032](decisions/032-remove-incident-agent-tasks.md) removed; agent runs (ADR-027) need `models.invoke` plus a grant for every exposed or context operation. ADR-017 adds bounded native A2A delegation between the installed coordinator and worker agents. General or remote agent coordination is outside this profile.

[ADR-019](decisions/019-m5-operational-recovery.md) adds a snapshot-recovery fence to the authenticated service: restore disables old principals, clears policy, fails unfinished historical tasks conservatively and seals old execution namespaces. Current-store reboot preserves normal wait semantics. Fresh execution after snapshot restore requires the `new_namespace_prefix` exposed by `/v0.2/system`, fresh scoped budgets and the same live ceiling below. Historical reads still require current supervisor/ancestor/platform rights.

Every task has one accountable supervisor principal, established through authenticated submission and persisted with grant provenance. For any operation/resource/context at time `t`:

```text
effective(task, t) = explicit_task_grants(t)
                   ∩ supervisor_current_permissions(t)
                   ∩ supervisor_current_delegation_authority(t)
                   ∩ ancestor_effective_permissions(t)
                   ∩ platform_policy(t)
```

For a root task, the ancestor term imposes no additional constraint. This is a conjunction of authorization decisions, including conditions and expiry, not merely a comparison of role names or permission strings. A supervisor may delegate only authority they can currently exercise and are allowed to delegate. Default is denial when the supervisor, provenance, or current policy cannot be established.

Reject attempted grants outside this ceiling. Check the live ceiling on discovery, reads, writes, execution, model disclosure, result/receipt access, and authority operations, including retries and resumed work. Persisting a grant does not freeze the supervisor's old permissions. New supervisor privileges do not automatically expand a task's explicit grants. Human approval or another approver's broader access cannot raise the task above its bound supervisor.

Revocation, expiry, or supervisor disablement applies transitively to descendants. Order authorization and dispatch against revocation so queued work cannot use stale authority; invalidate affected cached decisions. Stop ongoing access and cancel/reconcile affected in-flight work through the host/broker. Previously completed effects cannot be undone by changing permissions, and uncertain outcomes remain visible; neither permits subsequent unauthorized work.

Child tasks remain within both their parent and supervisor ceilings. Multiple grants cannot be combined to exceed that bound. Task reassignment cannot silently substitute a more privileged supervisor: any future reassignment must be explicitly authorized, revalidate grants and provenance, and avoid carrying old authority into the new binding. Trusted daemon/model/host service privileges are implementation powers, not permissions available to tasks.

The v0.1 `owner_id` and grant issuer fields alone do not establish this chain. M1 adds immutable `task_bindings` and authenticated v0.2 admission with transactional Casbin checks under ADR-010. Ancestor/supervisor revocation and policy freshness are tested across dispatch and recovery. M4 accepts only allocated inline host children from its bound coordinator runtime. Other endpoints cannot create unallocated descendants under that coordinator. Child grants omit submission/resume; nested plans and delegated model tasks remain unsupported.

## Proposed Agent Operations

The next product scope also requires [company process and data integration](components/company-integration.md): permission-filtered external resource discovery/reads, versioned source references, accepted change events and process-bound task/evidence links. These are proposed extensions, not implemented endpoints or frozen types. [ADR-021](decisions/021-company-process-data-integration.md) requires standards-first fit and versioned contract decisions before changes. Company schemas and process rules stay in the integration/application layer; the kernel enforces the generic authority, persistence and execution contracts. Cross-team correlation never confers authority or bypasses ancestor ceilings.

Names are logical operations; transport and path spelling are not a promise that endpoints exist.

| Family | Initial operations | Semantics |
|---|---|---|
| Discovery | `system.describe`, `operations.list` | Return versioned schemas, effects, required scopes, and supported execution profiles filtered by authority. |
| Tasks | `tasks.submit`, `tasks.inspect`, `tasks.cancel`, `tasks.checkpoint` | Durable execution and cooperative restart boundaries; caller cannot assert successful completion without checks. |
| Objects | `objects.list`, `objects.read`, `objects.write` | Scoped handles, bounded content, versions, conditional writes, and publication receipts. |
| Execution | `operations.invoke`, `operations.inspect` | Dispatch registered tools in an enforceable profile; return stable operation IDs and structured receipts. |
| Events | `events.read`, `events.wait` | Durable cursors and scoped subscriptions; no busy polling or dependence on an open browser. |
| Authority | `capabilities.inspect`, `capabilities.request` | Discover/request authority; grant/revoke are restricted supervisor operations. |
| Results | `tasks.complete` | Submit artifact references for independent acceptance checks. |

The proposed generic `tasks.spawn` and `messages.send/receive` names are not implemented endpoints. M4 exposes `POST /v0.2/delegation-tasks`, native A2A messages/tasks at the agent boundary, and authenticated `/tasks/{id}/children` inspection. Existing DBOS wakeups, immutable message/slot associations and conditional object publication supply the bounded profile without a new mailbox API.

The 2026-09-22 [standards-first decision](standards-first.md) revises the earlier M0 session proposal. [ADR-001](decisions/001-m0-contracts.md) selects HTTP/OpenAPI over a private Unix-domain socket with SPIFFE/OIDC authentication adapters; provider/session integration must pass M1 acceptance before exposure. Socket permissions alone cannot distinguish agents sharing an account. Trusted execution bindings must connect authenticated runtime identity to the admitted task. Supervisor and agent clients share authorization handlers; remote network exposure is outside the first MVP. See [runtime decisions](runtime-decisions.md).

## Task Lifecycle

- `queued`: accepted and durably recorded; waiting for admission.
- `running`: admitted under a bounded dispatch lease; may execute model/tool steps.
- `waiting`: checkpointed with an event cursor, timer, dependency, or authority request.
- `suspended`: explicit supervisor pause at a durable boundary; not native memory suspension.
- `succeeded`: independent result checks passed and outputs are committed.
- `failed`: terminal error with a structured reason and evidence.
- `cancelled`: dispatch stopped and in-flight work terminated or classified for reconciliation.

Allowed transitions: queued to running/cancelled/failed; running to waiting/suspended/succeeded/failed/cancelled; waiting to queued/suspended/failed/cancelled; suspended to queued/failed/cancelled. Terminal states never silently reopen. Resubmission creates a linked new task. Only checkpoint-safe pauses are supported initially.

On restart, an expired running lease enters a recovery routine. Reconcile outstanding operation intents first, then requeue from the last committed checkpoint or fail with an explicit unresolved outcome. Never blindly rerun the last external effect. Cooperative cancellation may have a bounded grace period; unknown effects remain visible even if the task is cancelled.

## Operation Lifecycle and Retries

Persist admission decisions and operation intent before dispatch. Execution state is separate from task state and verification: `accepted`, `running`, `succeeded`, `failed`, `cancelled`, or `outcome_unknown`. Denied calls create an authorization result without dispatch.

An identical retry under the same principal, task, operation ID, and argument digest returns the existing status/receipt after access checks. Changed arguments under the same ID are rejected. New operation IDs are not deduplication keys for an earlier effect.

Every operation declares one of: read-only/retryable, intrinsically idempotent, externally deduplicated, or reconciliation-required. Local transactions and conditional object versions protect kernel state. Tool execution and external I/O require their own effect protocol; they cannot share SQLite's transaction boundary.

If a worker completes an effect before its receipt commits, recovery must inspect observable state or ask a supported external deduplication mechanism. If neither proves the outcome, retain `outcome_unknown` and request intervention. Do not promise general exactly-once execution.

## Waiting and Event Delivery

Commit the checkpoint and wait registration against a journal cursor transactionally. Check for matching events after that cursor as part of registration/recovery so publication around the wait boundary is not lost. Deliver at least once; consumers identify events and commit progress with durable cursors. Filter both subscriptions and payloads by current authority. Timers and cancellation are durable wakeup reasons.

Installed input adapters publish events under their own scoped identities and stable source IDs. For the MVP directory source, rescan input versions on restart and reconcile them against the persisted ingestion cursor before resuming waits. An in-memory filesystem notification alone cannot preserve changes that arrive during downtime. General external sources require an explicit replay or reconciliation mechanism.

## Storage and Resource Boundaries

Agents use object handles within a namespace, not arbitrary host paths. Host adapters materialize authorized inputs into isolated working directories and publish validated outputs. Conditional updates require an expected version. Artifact bytes and journal metadata require an explicit crash-recovery protocol including orphan cleanup.

A task budget limits model invocation count/tokens, steps, wall time, tool time, output bytes, and concurrency. Host CPU/memory/process limits are part of the execution profile. Reserve before dispatch, reconcile actual usage, and conservatively handle unknown consumption. Child tasks share the parent ceiling. No claim of a strict monetary bound without provider-specific enforcement and pricing semantics.

## Result Verification

A trusted installed application verifier checks declared postconditions against actual artifacts or observed state. It runs with bounded authority/resources. The agent cannot select an always-successful verifier to validate itself. Model judgments may provide supplementary evidence but do not replace observable checks in the MVP.

## Conformance

The same portable contract suite applies to every runtime and host adapter. Host enforcement tests additionally prove filesystem/network confinement, descendant termination, and resource limits on the declared environment. A missing feature rejects the profile. A development fallback must identify itself as unenforced and cannot satisfy the MVP gate.
