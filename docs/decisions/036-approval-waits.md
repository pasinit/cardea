# ADR-036: Approval Waits and Per-Operation Autonomy

- **Date / owner / milestones:** 2026-11-08; kernel and runtime maintainers. Affects the operation registry (ADR-023), the invocation core and effect scopes (ADR-026), agent runs (ADR-027, ADR-031), triggers (ADR-024, ADR-029) and policy provisioning (ADR-010). Related proposals written in parallel: ADR-034 (agent groups, shared records, state machines and mailboxes) and ADR-035 (event triggers). This record does not design either.
- **Status:** selected (design), 2026-11-08, by the lead reviewer; the project owner may veto. Implementation is pending: nothing below is implemented, and no fit test in this record has run. The CEL implementation is chosen by the fit test in slice 1. The open questions are settled under [Decisions settled in review](#decisions-settled-in-review-2026-11-08).
- **Requirement:** some effects must not run until an authorised principal has approved that exact effect. Examples: an agent proposes an order, a payment, a deployment or a merge, and a principal holding approval authority approves or rejects it. A reviewer role can block a change. Small effects inside tighter limits run without approval.
  - Whether approval is needed is configuration per operation and per grant, never code in the kernel.
  - The wait is durable. It survives daemon restarts and the loss of any lease, and it has a deadline with a defined outcome.
  - What runs is exactly what was approved: same operation, same descriptor version, same arguments.
  - Deciding is idempotent. The requesting agent can never approve its own proposal.
  - Approval never raises a task above its supervisor's live ceiling. The ceiling is checked again when the decision is made and when the effect runs.
  - The receipt of the executed effect names the approver, the decision, its time and the approved argument hash.
  - The agent learns the outcome without guessing.
  - An approver can find out that a request is pending through an event the deployment routes. Cardea does not build a notification service.

## Reconciling approval with the product direction

[product.md](../../product.md) says supervision surfaces use the same authorization path as agents, and that already-authorized work does not need ceremonial approval clicks. [design.md](../../design.md) calls approval "a policy-selected escalation, not a mandatory preview/approve ritual". The [kernel contract](../kernel-contract.md) says approval cannot raise a task above its bound supervisor. This record keeps all three:

1. **Approval is an extra conjunct, never a source of authority.** For an operation that needs approval, the effective decision becomes

   ```text
   may_run(task, op, args, t) = effective(task, op, t)                  # the ADR-010 live ceiling, unchanged
                              ∧ approved(intent(task, op, args), t)       # only where a rule requires it
   ```

   An operation outside the task's grants, or outside the supervisor's live permissions, is denied as today. It produces no approval request, because no approver can supply the missing authority. Approval narrows; it never widens.
2. **The default is autonomous.** An operation with no approval rule, or whose rule's condition is false for these arguments, runs on authority alone, as every installed operation does today. Approval is required only where an installed rule says so, and the rule records why (for example "payments above 500").
3. **The decision is an authority decision on the normal path.** Deciding is an authenticated v0.2 call, checked against current Casbin policy in the same SQLite transaction that records it, exactly like `tasks.cancel` and `tasks.resume`. A web UI, a chat bot, the CLI and a reviewer agent all call the same endpoint with their own identities. No client has an override.

**When authority alone is enough:** the operation is granted and inside the live ceiling, and no applicable rule requires approval for these arguments. **When approval is also required:** some applicable rule (an operation default, a host rule or a supervisor's per-grant rule, defined below) evaluates to "approve" for the frozen arguments.

## Candidates and primary evidence

Checked 2026-11-08. Versions are the pinned dependency where Cardea already uses the project, otherwise the newest release seen on that date. Reference models are read for semantics, not proposed as dependencies.

| Candidate | What it offers | Fit for this requirement |
|---|---|---|
| **DBOS durable messaging and sleep** ([`DBOS.send`/`DBOS.recv`, `DBOS.sleep`](https://docs.dbos.dev/python/reference/contexts), pinned `dbos==3.0.0`, MIT, [source at 3.0.0](https://github.com/dbos-inc/dbos-transact-py/tree/3.0.0)) | `send` takes an `idempotency_key`; `recv(topic, timeout_seconds)` records its timeout durably, so a recovered workflow resumes the remaining wait. Cardea already uses `recv` for event waits and `send_in_transaction`/`enqueue_in_transaction` for control signals and admission (ADR-008). | **Reused for delivery, not for holding the wait.** In the 3.0.0 source, a synchronous `recv` blocks its workflow's thread on an in-process event until a message or the timeout arrives, and a queued workflow keeps its queue's concurrency slot while it is pending. Holding one blocked workflow per pending approval for hours or days would pin threads and, on the agent-run queue (sized by `concurrent_sessions`), starve sessions. The decision must also be checked against Casbin in Cardea's own transaction, so the store is the source of truth. Selected uses: transactional enqueue of the continuation under a deterministic workflow ID (exactly-once start), and the existing DBOS minute schedule (ADR-024) as the expiry clock. A fit test of many concurrent `recv` waits is not run and is listed under revisit. |
| **Temporal signals and updates** ([message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing); server v1.32.2 seen) | The reference model for a durable human wait. A signal is fire-and-forget; an *update* is a request–response message with a *validator* that can reject it before it enters history; `workflow.wait_condition` with a timeout gives expiry. | **Reference model; not adopted.** ADR-004/007 evaluated Temporal as a runtime and kept DBOS. Switching engines for one wait primitive is not justified. Adopted semantics: the decision call is update-like (validated synchronously, rejected without side effects if invalid, idempotent by an ID), and the wait has a timer racing the decision. |
| **BPMN 2.0 user tasks** ([OMG BPMN 2.0.2](https://www.omg.org/spec/BPMN/2.0.2/)) as run by Camunda 8 ([user tasks](https://docs.camunda.io/docs/components/modeler/bpmn/user-tasks/), [timer boundary events](https://docs.camunda.io/docs/components/modeler/bpmn/timer-events/); release 8.10.x seen) | A user task has candidate users/groups, a due date and a follow-up date; an interrupting timer boundary event models expiry; the process continues with the task's output. | **Reference model; not adopted.** ADR-021 keeps the process-engine question open for multi-step company processes, and ADR-029 rejected a process DSL for single-step agent runs. A BPMN engine would become a second owner of execution and would not see Cardea's ceiling or receipts. Adopted semantics: candidate approvers are a permission, not a list of names; expiry is a timer that interrupts the wait and takes a declared path. A future process application on ADR-021 can map a user task onto an approval request. |
| **Zanzibar-style relationship checks** ([Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/), [OpenFGA](https://openfga.dev/docs/concepts) v1.22.1 seen, Apache-2.0) | Relationship tuples ("user U is approver of project P"), usersets and conditions. | **Not needed now.** "May principal P approve operation X in namespace N" is an exact ACL that the selected Casbin model already expresses as one more `mode`. ADR-010 kept Casbin and said to revisit relationships only for a concrete unmet requirement. Approver sets derived from org structure (for example "the requester's manager") would be that requirement; ADR-034 roles may raise it. |
| **Casbin ABAC matchers** ([ABAC](https://casbin.org/docs/abac); pinned `casbin==1.43.0`, Apache-2.0) | Matchers can evaluate request attributes such as `r.obj.amount > 100`. | **Not used for thresholds.** ADR-010 deliberately keeps an exact-match model with no expression evaluation in the authorization matcher. Mixing argument thresholds into it would turn every policy decision into a per-argument evaluation. Thresholds belong to approval rules, evaluated once against the frozen arguments. |
| **CEL, the Common Expression Language** ([spec](https://github.com/google/cel-spec)); used for conditions by [Kubernetes ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/) and Google Cloud IAM Conditions. Python implementations: [`cel-python`](https://github.com/cloud-custodian/cel-python) 0.5.0 (2026-01-31, Apache-2.0, beta, pure Python with `lark`, `google-re2`, `pendulum` dependencies) and [`common-expression-language`](https://github.com/hardbyte/python-common-expression-language) 0.10.0 (2026-09-15, Apache-2.0, bindings to a Rust implementation) | A standard, side-effect-free, terminating expression language over JSON-like values, designed for exactly "does this request need extra checks". | **Selected for rule conditions; implementation chosen by the slice-1 fit test.** If both pass, prefer the one with fewer native dependencies. The test must show: evaluation over validated JSON arguments; no access to time, I/O or host state; a bounded cost per expression; errors (missing field, type mismatch) surfaced as errors so Cardea can fail closed; install on the supported Debian profile. Do not mark either implementation unsuitable before that test. If neither passes, the fallback is the smallest custom form: RFC 6901 pointers (already used by ADR-026) with `lt/le/gt/ge/eq/in` comparisons. It is acceptable only as an exception recorded in this ADR. |
| **MCP elicitation** ([MCP 2025-06-18, elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation)) | A server asks the connected client's user for structured input mid-call. | **Rejected.** The "user" is whoever drives the MCP client, here the headless harness, not an authorised principal. The request lives in one open call, which is neither durable nor survivable across attempts. |
| **A2A `input-required` / `auth-required` task states** ([A2A specification](https://a2a-protocol.org/latest/specification/)) | Standard task states telling a peer agent that a task is blocked on input or authorization. | **Mapping only, later.** Useful for presenting a waiting delegated child (ADR-017) to its parent agent. It carries no approver identity or argument binding, so it cannot be the record. |
| **Cardea's own lease, fencing and receipt model** (ADR-010, ADR-026, ADR-027) | Leases fence workers; receipts bind a request digest; recovery cancels accepted-but-undispatched operations and marks dispatched ones uncertain; effect scopes dedupe across attempts. | **Reused unchanged for execution.** A wait holds no lease, so lease loss cannot touch it. The pending intent is *not* an `accepted` receipt, because recovery would cancel it and v0.1 has no "awaiting approval" operation state. Adding one would be a versioned contract change for no gain. The approved effect runs later under a fresh lease through the shared invocation core, so fencing, scopes, receipts and reconciliation apply exactly as for any other call. |
| **Hosted approval gates** (for example CI/CD "required reviewers" on deployment environments) | Required reviewers, "prevent self-review", a wait timer, automatic expiry of waiting jobs. | **Reference behaviour only** (not open source, not a dependency). It confirms the expected rules: the requester cannot approve, a waiting run expires, and the gate is configured per target rather than per click. |

## Decision

### 1. Approval rules and autonomy levels

An approval rule says when an operation needs approval. Rules come from three places, and the strictest outcome wins.

| Source | Who sets it | Where | Can it loosen? |
|---|---|---|---|
| Operation default | Whoever installs the operation (host owner) | Descriptor `cardea.approval` (ADR-023) | It is the baseline. |
| Host rule | Host owner, per namespace and operation | Host configuration, provisioned like policy (ADR-010) and versioned by digest | Yes, it replaces the operation default for that namespace. It is the platform term of the ceiling. |
| Per-grant rule | The submitting supervisor | `approvals` in an agent-run request or trigger template, next to `grants` | No. It can only add requirements. |

A rule has:

```json
{
  "rule_id": "large-payments",
  "level": "conditional",
  "when": "args.amount > 500 || args.currency != 'EUR'",
  "reason": "Payments above 500 or in a foreign currency need finance approval",
  "expires_after_seconds": 86400,
  "on_expiry": "resume",
  "distinct_from_supervisor": false
}
```

- `level` is one of:
  - `autonomous`: this rule never requires approval (the default when no rule exists);
  - `conditional`: approval is required when `when` is true for the validated arguments;
  - `always`: every call needs approval.
- `when` is a condition over `args` (the validated arguments), `operation` and `namespace` only. It cannot see time, other tasks or external state, so the same frozen intent always evaluates the same way. **An evaluation error requires approval** (fail closed).
- `expires_after_seconds` bounds the wait (60 s to 30 days). `on_expiry` is `resume` (the run continues and is told) or `fail` (the task fails with `approval_expired`). When a rule omits it, the default depends on the task: `resume` for agent runs, so the agent is told and can report, and `fail` for single-operation tasks, which have no agent to tell.
- `distinct_from_supervisor: true` adds four-eyes separation: the task's own supervisor cannot approve. It is opt-in. By default the supervisor may approve.
- When several rules require approval, the request takes the earliest expiry, `fail` over `resume`, and the union of separation constraints. It lists every requiring `rule_id`.
- A per-grant rule is part of the agent-run request and of a trigger's definition digest (ADR-024), so changing it creates a new definition version.

These levels are generic. Nothing in the kernel names payments, merges or currencies; those live in the rules a deployment installs.

### 2. Who may approve

- The kernel gains one Casbin `mode`, `approve`, next to `allow` and `delegate`. The row `(P, namespace, operation, approve)` means "P may decide approval requests for this operation in this namespace". Provisioning permissions gain `approve: bool` and `allow: bool` (default true), so an approve-only permission can be expressed: for example, a controller may approve payments without being able to make them. A matching `platform` row is required too, as for every other right.
- At decision time the approver must:
  - be an enabled principal holding the `approve` row now;
  - not be the task's agent or runtime principal, nor the agent or runtime of any ancestor task;
  - not be the task's supervisor when the request says `distinct_from_supervisor`.
- Agents may be approvers when they hold the `approve` row; this is how an ADR-034 reviewer role can block a change. The requester exclusion still applies: the agent is neither the requester nor an ancestor's agent or runtime.
- An agent approver is checked at decision time like any other approver, live. When it decides from inside a task (for example an ADR-034 reviewer run), the `approve` right must also be inside that task's own live ceiling. The task must be granted it, its supervisor must currently hold it with `delegate`, and the platform and every ancestor must allow it. An agent cannot approve with authority its own supervisor does not have.
- The approver's own broader access is irrelevant to the ceiling. The task's live ceiling for the operation and its `requires` rights (`policy.ceiling`) is checked at proposal, at decision and at execution. If it fails at decision or execution, the request becomes `void` and nothing runs.
- One approval is enough in this record. A rejection by any authorised approver is final. Quorum (n of m approvers) is a later slice.

### 3. The frozen intent

When an approval-required call arrives, its arguments are first validated against the operation's input schema and the 64 KiB argument limit (ADR-029). Cardea then writes an **approval request** row (table `approval_requests`, new migration) holding:

- `request_id`, `task_id`, `namespace`, the supervisor, and the requesting agent and runtime;
- `operation` and the installed descriptor `version`;
- `arguments` (the validated document) and `argument_sha256`, the SHA-256 of their RFC 8785 encoding. This is the same hash the run view already shows (ADR-029), so an approver UI and the run view agree;
- `idempotency_context` and, when the operation declares a scope, the ADR-026 scope key;
- `intent_digest`, the SHA-256 of the RFC 8785 encoding of `{task_id, namespace, operation, version, argument_sha256, idempotency_context}`;
- the requiring `rule_id`s with the rule-set digest, the reason text, `created_at`, `expires_at` (never later than the task deadline) and `on_expiry`;
- `state`: `pending` → `approved` → `executed` | `void`, or `pending` → `rejected` | `expired` | `withdrawn` | `void`. Every state except `pending` and `approved` is terminal.

The approver must echo `intent_digest` with the decision. A mismatch is refused, so an approver cannot approve something other than what is stored. At execution the stored arguments are dispatched as they are; the agent does not resend them. If the operation was reinstalled with a different version in the meantime, the request becomes `void` with `operation_changed`.

An identical proposal (same `intent_digest`) while one is pending returns the existing request rather than creating another. In this record a task has **at most one open request**. A different approval-required call while one is open is refused with `approval_pending` and creates nothing.

### 4. The durable wait

- **Single-operation tasks** (the operation runtime): the task moves RUNNING → WAITING with a checkpoint, in the same transaction that writes the request. The task holds no lease while it waits.
- **Agent runs:** the tool call returns at once with a structured result `{"status": "approval_pending", "request_id", "expires_at"}`, telling the agent the decision will come in a later turn and not to retry. The call creates no receipt and does not use the tool-call budget (like `tool_busy` in ADR-027). The session may continue with other work. When the attempt ends with a request open, the task moves to WAITING instead of finishing, whether the session succeeded or failed. A failed session still uses a retry.
- **The wait condition** is the existing v0.1 `EventWait` on event type `approval.<request_id>` at the checkpoint cursor. The store's existing rule, that registering a wait checks for events already committed after the cursor, means a decision that races the wait boundary is not lost. The v0.1 model already has an `AuthorityWait` kind, but the v0.1 store cannot tell when it is ready. Using it is a deliberate store change, deferred (see revisit). The v0.2 task view shows `waiting_for: {"approval": request_id}` from the service table.
- **Lease loss while proposing:** if an agent-run attempt's lease is lost after the request is written, kernel recovery fences the attempt as today. The next attempt finds the open request and parks the task (QUEUED → RUNNING → WAITING with a fresh checkpoint) without starting a session or using an attempt.
- **Restarts:** the request, the waiting task and the checkpoint are all in SQLite. Nothing waits in memory, so a restart changes nothing.
- **Expiry:** the existing DBOS minute schedule (ADR-024, one clock) also expires overdue requests. Each expiry is a conditional update `WHERE state='pending' AND expires_at <= now`, so it is idempotent and at most about 60 s late. A request that expired during downtime is expired by the first tick after restart. Expiry appends `approval.<request_id>` and `approval.expired`. Then:
  - with `on_expiry: resume`, the task wakes (WAITING → QUEUED) and its continuation is enqueued in the same transaction;
  - with `fail`, the task fails with `approval_expired`.
- **Withdrawal:** `tasks.cancel` on a waiting task marks its request `withdrawn`. Withdrawal by the agent is not offered in this record.
- **Bounds:** an agent run has `max_approval_waits` (default 4, at most 16). A further approval-required call is refused with `approval_limit`, so a looping agent cannot create unbounded waits. An agent-run request that may wait must cover the wait in its deadline. It gains `approval_wait_seconds` (at most 30 days), which is added to the task deadline at admission, and `expires_at` is capped by that deadline.

### 5. Deciding

```text
GET  /v0.2/approvals?namespace=&state=pending&after=&limit=   requests the caller may decide or supervises
GET  /v0.2/approvals/{request_id}                             the whole frozen intent (fits one response)
POST /v0.2/approvals/{request_id}/decision
     {"decision_id": "...", "decision": "approve" | "reject",
      "intent_digest": "...", "reason": "..."}
```

- **Reading** requires the `approve` row for the operation in the namespace, or `tasks.inspect` on the task as its supervisor.
- **Deciding** happens in one transaction:
  1. authenticate and check the approver rules in item 2 and the task's live ceiling;
  2. check that `state='pending'`, that `expires_at` has not passed and that `intent_digest` matches;
  3. record the decision (`approval_decisions`: `decision_id`, approver, decision, bounded reason, time);
  4. append `approval.<request_id>` and `approval.decided`;
  5. wake the task and `enqueue_in_transaction` its continuation with workflow ID `<task>.approval.<request_id>`.

  This is the same pattern `tasks.cancel` already uses with `control_requests` and `send_in_transaction`. A crash before commit leaves nothing; a crash after commit leaves a committed decision with an enqueued continuation that DBOS recovers.
- **Idempotency:** `decision_id` is the idempotency key. Replaying the same `decision_id` with the same body returns the recorded decision. The same `decision_id` with a different body is `409 decision_conflict`. Any other decision on a request that is no longer pending is `409 approval_closed` and carries the current state, so a client that lost a response can learn the outcome. Two approvers racing: the first commit wins, the second gets `approval_closed`.
- **Refusals** (`403 approver_denied`, `403 self_approval`, `409 approval_expired`, `409 intent_mismatch`) write nothing.
- **Reason text** is bounded to one printable line of 200 characters, like ADR-026 failure summaries. It is shown to the agent, marked as untrusted data.

### 6. Execution and the receipt

The continuation runs under a fresh lease of the task. Before anything else it checks:

- the request is `approved`;
- the descriptor version is unchanged;
- the task's full live ceiling holds for the operation and its `requires` rights;
- the approver is still enabled and still holds `approve`.

If any check fails, the request becomes `void` with a reason (`approver_authority_lost`, `authority_lost`, `operation_changed`), no effect runs and the outcome is reported as below. Otherwise the stored intent goes through the shared invocation core (ADR-026) as operation `apr-NN`. NN is numbered per task like `ctx-NN`. Effect scopes, deduplication, timeouts, `outcome_unknown` and reconciliation all apply unchanged. A crash mid-effect fences the task with `reconciliation_required` and never replays the effect. The request becomes `executed` with the receipt ID.

Following the ADR-026 precedent for caller context, the receipt records the approval under `arguments.context.approval`:

```json
{"request_id": "...", "decision_id": "...", "approver_id": "...", "decision": "approve",
 "decided_at": "...", "argument_sha256": "...", "intent_digest": "...",
 "rule_ids": ["large-payments"], "rules_digest": "..."}
```

The run view (ADR-029) and `GET /agent-receipts` show it with the call. Rejections, expiries and voids create no receipt, because nothing was dispatched. They remain in `approval_requests`/`approval_decisions` and in the events.

### 7. How the requester learns the outcome

**The run resumes with the result; it is not a new run.**

- **Agent runs:** the continuation is a new attempt of the same task, with the next attempt number so leases, logs and `ctx-NN` numbering stay unique. It does not use a retry from `max_attempts`, which still counts only failed sessions. After running `apr-NN` (or not) and the context operations (ADR-031), the session prompt carries the retry journal (ADR-027 item 4) plus one block per closed request. The block uses ADR-031's escaping and untrusted-data preface:

  ```text
  <approval request="REQ" operation="NAME" decision="approved|rejected|expired|void|withdrawn"
            by="PRINCIPAL" at="TIME" reason="...">RESULT-JSON-or-empty</approval>
  ```

  Expectations (ADR-029) are evaluated only when a session ends with no open request. An executed `apr-NN` counts as a distinct applied effect of its operation.
- **Single-operation tasks:** approve leads to execution and the normal verified result. Reject fails the task with `approval_rejected` and void with `approval_void`. Expiry always fails it with `approval_expired`, because there is no agent to resume, so `on_expiry: resume` has no effect on these tasks.
- **Any client** can follow `GET /v0.2/approvals/{id}`, the task view or `events.wait` on the namespace.

Re-asking the agent to call the tool again after approval was rejected as a design. It would depend on the model reproducing byte-identical arguments, and a reworded call would either fail the digest check or need a second approval.

### 8. Notifications

Every state change appends a kernel event in the same transaction: `approval.requested`, `approval.decided`, `approval.expired`, `approval.void`, `approval.withdrawn`, plus the per-request `approval.<request_id>` that wakes the task. Payloads carry `request_id`, `task_id`, `operation`, `namespace`, `intent_digest`, `expires_at` and `rule_ids`. They never carry arguments: those are read through `GET /v0.2/approvals/{id}` under the approver's own authority. Delivery is at least once with durable cursors, as for every journal event.

A deployment routes these events to people or agents with what it already has:

- a client long-polling `events.wait`;
- an ADR-035 event trigger on `approval.requested` that runs an installed operation (for example a chat or mail operation with an ADR-026 idempotency scope keyed on `request_id`, so each request notifies once);
- an ADR-034 mailbox message addressed to a reviewer agent pool.

Cardea builds no notification service, channel routing or reminders. A reminder is a further trigger.

### 9. Smallest custom scope

New code is limited to:

- the rule loader and evaluator (wrapping the selected CEL implementation);
- two tables and their migration;
- the `approve` policy mode;
- three endpoints;
- the expiry step on the existing schedule;
- the tool-endpoint branch that turns an approval-required call into a request;
- the continuation step that executes `apr-NN` through the existing invocation core.

The wait reuses the v0.1 `EventWait`. Delivery reuses DBOS transactional enqueue and the existing minute schedule. Authorization reuses Casbin, and effects reuse ADR-026.

## Decisions settled in review (2026-11-08)

The lead reviewer decided these open questions on 2026-11-08. The project owner may veto any of them.

1. **Supervisor approval.** The task's supervisor may approve by default. Four-eyes separation stays an opt-in rule (`distinct_from_supervisor`).
2. **Expiry default.** `resume` for agent runs, `fail` for single-operation tasks, when a rule does not set `on_expiry`.
3. **Open requests.** At most one open request per task for now.
4. **Agent approvers.** Allowed when the agent holds `approve` and is neither the requester nor an ancestor's agent or runtime. Its own authority is checked against the live ceiling at decision time like any approver's (item 2).
5. **CEL implementation.** The slice-1 fit test decides between `cel-python` and `common-expression-language`; if both pass, take the one with fewer native dependencies. The JSON-pointer fallback is acceptable only as a recorded exception.
6. **Deadline.** An approval wait extends the task deadline through `approval_wait_seconds`, at most 30 days.
7. **Reason text.** Shown to the agent, bounded to 200 characters and marked untrusted.
8. **Bounds.** A maximum expiry of 30 days and a default of 4 approval waits per run (`max_approval_waits`, at most 16).

## Amendment (2026-11-09): resource-qualified approve

Decided by the lead on 2026-11-09 for [ADR-034](034-agent-groups-and-shared-records.md); the owner may veto. Not implemented.

- **Why.** ADR-034 routes every record state transition through one built-in operation, `records.transition`. An `approve` row on `(namespace, records.transition)` would let its holder approve every transition of every collection. Approvers must instead be scoped to one transition of one collection: approving `orders/approve` must not also approve `work/approve`.
- **What changes.** The `approve` mode (item 2) may also be granted on a resource-qualified grant of the form `<kind>:<resource>`, written like ADR-033's `harness:<id>`. The row `(P, namespace, record:orders/approve, approve)` means "P may decide approval requests for the `approve` transition of collection `orders` in this namespace". It needs a matching `platform` row, as every right does.
- **Which grant a request needs.** An approval request records the *approval resource* that its requiring rule names. For a rule that ADR-034 compiles from a transition's `approval`, that resource is `record:<collection>/<transition>`. For every other rule it is the operation name, exactly as today.
  - The decision-time check (item 2) looks up the `approve` row on that resource.
  - The ceiling check (`policy.ceiling`) also applies to that resource: the requesting task must hold the resource-qualified grant itself.
- **What does not change.** The requester exclusion, `distinct_from_supervisor`, the frozen intent and its digest, the durable wait, expiry, idempotent decisions and execution are all unchanged. A resource-qualified `approve` row grants no other right, and it can never raise a task above its supervisor's live ceiling.
- **Tests (to write).** A principal holding `approve` on `record:orders/approve` can decide that transition's request and is refused with `403 approver_denied` for `record:work/approve`. An operation-level `approve` row on `records.transition` does not satisfy a resource-qualified request.

## Integration

- **Authority:** the ceiling formula is unchanged. Approval adds a conjunct for selected effects. The live ceiling is checked at proposal, decision and execution, and the approver's `approve` right at decision and execution. Supervisor revocation or disablement while a task waits voids its request at the next decision, expiry or execution check. Snapshot restore (ADR-019) seals old namespaces: pending requests in a sealed namespace are voided by the restore fence.
- **State ownership:** SQLite owns requests, decisions, task state and events. DBOS owns the continuation workflow's start and recovery, and the clock. Neither owns the decision.
- **Compatibility:** no v0.1 record or digest changes. New v0.2 endpoints, request fields (`approvals`, `approval_wait_seconds`, `max_approval_waits`), a descriptor block (`cardea.approval`) and provisioning flags. Requests without them behave exactly as today. Existing descriptors without `approval` are `autonomous`.
- **Triggers:** a slot whose run waits does not block later slots. Each slot is its own task, and ADR-026 scopes still dedupe effects per slot.
- **Delegation:** a delegated child (ADR-017) waits like any task. Its approvers are decided by its own namespace policy. Presenting the wait to the parent as A2A `auth-required` is a later mapping.
- **Operational cost:** one row per request and per decision, a few events, and one indexed query per minute tick. No resident thread per wait.

## Acceptance tests

Portable tests, plus one Debian acceptance run with a real OIDC human approver in slice 6.

1. **Approve.** An above-threshold call waits; the approver approves with the right `intent_digest`. The effect runs once as `apr-01`; the receipt carries the approval block; the run resumes with the result; an `exactly once` expectation is met.
2. **Below threshold.** The same operation with a small amount runs at once with no request (authority alone).
3. **Reject.** No receipt and no handler call. An agent run resumes with `decision="rejected"` and the reason; a single-operation task fails with `approval_rejected`.
4. **Expire.** No decision before `expires_at`. The first tick after it expires the request, with no effect. `resume` continues the run; `fail` fails the task. A later decision gets `409 approval_expired`.
5. **Crash while waiting.** SIGKILL the daemon with a task waiting. After restart the task is still WAITING and the request pending; a decision then completes it. A request that expired during downtime expires on the first tick.
6. **Crash around the decision.** Fault injection before and after the decision commit gives either no decision, or a decision with exactly one continuation (deterministic workflow ID). Never a decision without a continuation.
7. **Crash during the approved effect.** It becomes `outcome_unknown` and `reconciliation_required`, never replayed.
8. **Lease loss after proposing.** The attempt is fenced; the request survives; the task parks without a new session and without using an attempt.
9. **Approver revoked before execution.** Approve, then remove the approver's `approve` row before the continuation runs. The request becomes `void (approver_authority_lost)`; no effect; the agent is told.
10. **Supervisor revoked while waiting.** The decision is refused or the request voided; no effect.
11. **Arguments changed → rejected:**
    - a decision with a stale or forged `intent_digest` gets `409 intent_mismatch`;
    - an operation reinstalled at a new version voids the request (`operation_changed`);
    - an agent re-proposing with different arguments while a request is open gets `approval_pending`; with identical arguments it gets the same request back.
12. **Idempotent decision.** Replaying the same `decision_id` returns the same result; the same ID with a different decision gets `409 decision_conflict`; two racing approvers produce one decision.
13. **Who may approve.** The requesting agent and runtime are refused (`self_approval`); the supervisor can approve by default and is refused when `distinct_from_supervisor`; a reviewer agent can approve only while its own task's live ceiling includes `approve`, and is refused once its supervisor loses it; a principal without `approve` is refused; an approve-only principal can approve but cannot invoke the operation.
14. **Autonomy configuration:**
    - a condition error requires approval;
    - a per-grant rule adds a requirement but cannot remove a host or operation-default one;
    - an ungranted operation is denied with no request;
    - a changed per-grant rule changes the trigger digest.
15. **Notifications.** `approval.requested` and `approval.decided` are committed with their state changes, their payloads exclude arguments, and `GET /approvals/{id}` enforces its read rule.
16. **Bounds.** `max_approval_waits` is enforced; `expires_at` never exceeds the task deadline; effect-scope deduplication at execution returns the earlier effect without a second call.

## Implementation plan (PR-sized slices)

1. **Rules and fit test.** CEL fit test for both shortlisted implementations, recorded in this ADR. Then the rule schema (descriptor `cardea.approval`, host rules, per-grant `approvals`), the evaluator with fail-closed errors, and unit tests. No runtime behaviour change.
2. **Requests and decisions for single-operation tasks.** Migration (`approval_requests`, `approval_decisions`), the `approve` policy mode and provisioning flags, propose → WAITING, the three endpoints, the decision transaction with enqueue, execution of `apr-NN` with the receipt block, and events. Tests 1–3, 9–13 and 15 for operation tasks.
3. **Expiry and crash safety.** The expiry step on the existing minute schedule, `on_expiry`, withdrawal on cancel, and fault-injection points. Tests 4–7.
4. **Agent runs.** The `approval_pending` tool result, parking at attempt end and on lease loss, the continuation attempt, the prompt block, `max_approval_waits`, `approval_wait_seconds`, and expectations after the last wait. Test 8 plus agent-run variants of 1–4 and 16.
5. **Supervisor rules and evidence.** Per-grant rules in agent-run requests and trigger templates (digest), plus approvals in the run view and `agent-receipts`.
6. **Documentation and host acceptance.** A getting-started use case, kernel-contract and component updates, and a Debian acceptance run with a real OIDC approver and a SPIFFE agent. Status moves to verified only after this.

Later, each behind its own decision: quorum approvals, agent-initiated withdrawal, several open requests per task, the A2A `auth-required` mapping, and moving the wait onto `AuthorityWait` in a versioned store change.

## Verification

None yet. This record is a design. Every test above is unrun.

## Revisit

Revisit if:

- approver sets must follow relationships (managers, project membership, ADR-034 roles) that exact Casbin rows cannot express; evaluate OpenFGA then;
- a fit test shows DBOS can hold many concurrent durable `recv` waits cheaply, without pinned threads or queue slots, which would allow the wait itself to live in a workflow;
- neither CEL implementation passes the slice-1 fit test, which triggers the pointer-comparison fallback as a recorded exception;
- company processes adopt a process engine under ADR-021, whose user tasks should then map onto approval requests rather than duplicate them;
- agents need several concurrent approvals in one run, or batched approval of a plan.
