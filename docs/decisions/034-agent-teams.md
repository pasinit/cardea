# ADR-034: Agent Teams

- **Date / owner / milestones:** 2026-11-08; runtime and API maintainers. Builds on [ADR-024](024-time-triggers.md) (time triggers), [ADR-026](026-effect-idempotency-and-reconciliation.md) (effect scopes), [ADR-027](027-agent-runs.md) (agent runs), [ADR-029](029-supervisor-schedules-expectations-and-run-evidence.md) (supervisor-managed triggers, expectations), [ADR-031](031-agent-run-context-operations.md) (context before the session) and [ADR-033](033-pluggable-agent-harnesses.md) (harnesses). Depends on ADR-035 (inbound event triggers) and ADR-036 (approval waits), both designed in parallel. Relates to roadmap M8 (repeatable work and daily supervision).
- **Status:** proposed. This record selects a design. Nothing in it is implemented, and none of the fit tests below has been run; each is listed as unrun.
- **Affected components:** [tasks and scheduler](../components/tasks-scheduler.md), [delegation and messaging](../components/delegation-messaging.md), [events and inputs](../components/events-inputs.md), [identity and capabilities](../components/identity-capabilities.md), [resource budgets](../components/resource-budgets.md), [agent runtime](../components/agent-runtime.md).

## Requirement

Cardea runs one-off agent runs under a supervisor, scheduled by triggers, with grants, context operations and A2A child delegation. It has no notion of a *standing team*: several long-lived agent roles that cooperate on shared work over days or weeks. A team's structure must be pure configuration. Changing who does what must be an edit to a document, not new code.

Reference scenario, used only to test generality: a software team with a principal engineer (reviews designs and changes, records decisions, can block), a product manager (exercises the system, files work items), a team leader (prioritises and assigns) and N developers (claim assigned items, implement them, open pull requests). An operations desk (on-call responders, an incident coordinator, a reviewer of post-incident reports) and a research group (a lead, several investigators, a reviewer) must be expressible with the same primitives and no new kernel concept. Nothing below names a software-team concept.

Observable behaviour:

1. **T1 Team manifest.** One declarative document defines roles (instructions, grants, harness and model, bounds, budgets, triggers, expectations), per-role instance counts, and relations between roles (`assigns_to`, `reviews`, `escalates_to`). Applying it provisions the instances' principals, grants and triggers. Applying a changed document is safe for in-flight work and for removed roles. Every role's grants stay inside the team supervisor's *live* ceiling, re-checked at every run.
2. **T2 Shared work items.** A durable, team-scoped work item with a lifecycle, a full history, an assignee, and links to external artifacts (pull requests, documents, external issues). Agents act on items through tools. What a role may do to an item comes from its grants and relations. Cardea owns the state; an external tracker may mirror it.
3. **T3 Claims.** Starting work on an item is an atomic claim with a lease and a fencing epoch. Two instances never hold the same item, including across crashes and restarts. An expired claim returns the item to the pool, and a stale holder can no longer act on it through Cardea.
4. **T4 Messaging.** Durable messages between the supervisor, roles and instances, addressed to a role, an instance or the whole team. A message is delivered as bounded context on the recipient's next run and may also wake the recipient through an event trigger. Messages survive restarts, are delivered to a successful run once, and are auditable with receipts.
5. **T5 Bounded context.** Every run of a role receives a kernel-assembled, size-bounded snapshot: its own recent runs, the team's state relevant to it, recent decisions, recently completed work and its inbox. Each block declares how fresh it is.
6. **T12 Expectations and budgets.** Per-run expectations (ADR-029 forms), per-role windowed expectations, per-role spend limits and a team-level spend ceiling, enforced at admission.

Trust boundary: the team supervisor is an authenticated human or service principal. Role instances are agent principals that act only through runs Cardea starts. Model output, messages, work-item text and external tracker data are untrusted data; none of them can grant authority, change the manifest, or address a principal outside the team.

Recovery: a crash at any point leaves every work item with at most one claim holder; messages are never lost and never consumed by a run that did not succeed; manifest changes are applied by a restartable reconciler that converges to the latest applied generation.

## Candidates and primary evidence

Checked 2026-11-08 against primary sources (specifications, API references and release metadata). Pins follow the project rule: the newest release at least two weeks old. **No candidate has had an executable fit test yet**; the "fit" column is from the specification or API reference and says so.

### Team definition (T1)

| Candidate | Version, licence | Fit from the source | Gaps |
|---|---|---|---|
| [Kubernetes resource model and API conventions](https://github.com/kubernetes/community/blob/main/contributors/devel/sig-architecture/api-conventions.md): `apiVersion`/`kind`/`metadata`/`spec`/`status`, `metadata.generation` and `status.observedGeneration`, declarative apply with server-side dry run, and [StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) ordinal identity and highest-ordinal-first scale-down | Conventions document at `main`; Apache-2.0 | Fits the shape and the change semantics: a desired-state document, a generation counter, a reconciler that reports what it has converged to, a dry run that returns the plan, stable per-instance identity (`<role>-<n>`), and an ordered, graceful scale-down. | It is a convention, not a runtime Cardea can embed; running a Kubernetes API server for this is out of scope (single Debian host, ADR-018). Reuse the shape and semantics, not the server. |
| Cardea's existing `Provisioning` document (`cardea provision`, ADR-024 triggers inside it) | in-tree | Already provisions principals, permissions and triggers, and replaces sets atomically. | Host-owner only, and replace-all. ADR-029 already rejected host-only ownership for client-managed work because user edits race with replace-all provisioning. It has no role, relation, instance-count or drain semantics. |
| [CrewAI](https://github.com/crewAIInc/crewAI) `agents.yaml` / `tasks.yaml` and hierarchical process | 1.15.30 (2026-10-24), MIT | Declarative roles (`role`, `goal`, `backstory`, `llm`, `tools`, `max_iter`, `allow_delegation`) and a manager-led process: close to the role vocabulary. | An in-process Python framework that owns the agent loop, delegation and memory, which ADR-027/033 assign to harnesses and the kernel. No instance counts, no authority model, no durable lifecycle. Useful as vocabulary only. |
| A2A [Agent Card](https://a2a-protocol.org/v1.0.0/specification/) (already pinned, `a2a-sdk==1.1.5`) | A2A 1.0, Apache-2.0 | Describes one agent's skills and endpoint for discovery. | Describes an agent, not a team: no relations, counts, grants or budgets. Can be *generated* per role for discovery later. |

### Work items (T2) and claims (T3)

| Candidate | Version, licence | Fit from the source | Gaps |
|---|---|---|---|
| [GitHub Issues REST API](https://docs.github.com/en/rest/issues) | API version `2022-11-28`; proprietary hosted service | Rich item model: state with `state_reason`, assignees, labels, comments, timeline events, sub-issues, links from pull requests. Webhooks for changes. | No conditional write: `PATCH` and the assignees endpoint have no compare-and-set or `If-Match` precondition, so two instances can both "claim" (documented conditional requests cover reads). No lease or expiry. Assignees are platform users, not Cardea principals, so authorization would be the platform's, outside the live ceiling. A hosted dependency for core coordination. Not tested. |
| [Gitea issues API](https://github.com/go-gitea/gitea) | v28.2.0 (2026-10-18), MIT; self-hostable | Same item shape (state, assignees, labels, comments, timeline, issue dependencies), self-hosted on the same host class. | The same missing atomic claim and lease; a second service and database to operate beside Cardea's SQLite (ADR-030 small host). Not tested. |
| Cardea versioned objects (`objects.write` with expected version) | in-tree | Conditional writes give compare-and-set; versions give history; namespaces give authority filtering. | No indexed query by state or assignee, no lease expiry, and authorization is per namespace or object version, not per transition (only roles with `assign` may assign). Storing items as objects would put transition rules in every agent's prompt. |
| Cardea task leases (kernel store) and ADR-027 per-attempt lease checks | in-tree | Exactly the needed semantics: a unique holder, an expiry, a monotonic identity checked at every mutation and before every tool call, and recovery on expiry. | A task lease covers one task's execution. An item claim must outlive one run (implement, then address review in a later run), so the lease must belong to an instance and be renewed by its runs. |
| DBOS queues / workflow IDs (pinned `dbos==3.0.0`) | MIT | At-most-once enqueue by workflow ID. | Dedupes execution, not ownership of a business item across runs; no claim/expiry concept. Kept for dispatch only. |

### Messaging (T4)

| Candidate | Version, licence | Fit from the source | Gaps |
|---|---|---|---|
| A2A 1.0 `Message` (`messageId`, `role`, `parts` of text/data/file, `contextId`, `taskId`) and `Task` (already pinned) | A2A 1.0, SDK 1.1.5, Apache-2.0 | A standard message body and correlation identifiers. | Point-to-point to a running agent endpoint (`message/send`, push notifications). No store-and-forward to a recipient that is not running, no addressing of "any instance of a role", no delivery-as-context. ADR-017 already rejected a second writable A2A task store. |
| [DBOS](https://docs.dbos.dev/) `send`/`recv` durable notifications | 3.0.0, MIT | Durable, topic-addressed, exactly-once into a workflow. | Addressed to one workflow ID and consumed inside that running workflow. A role instance is not a workflow; its runs are separate short-lived tasks. Not tested. |
| [CloudEvents](https://github.com/cloudevents/spec) | spec v1.0.2 (2022-02-06); Python SDK 2.2.0; Apache-2.0 | Standard event envelope (`id`, `source`, `type`, `subject`, `time`, `data`), which ADR-035 is expected to use for inbound events. | An envelope only: no durability, addressing or delivery semantics. |
| Transactional inbox/outbox (a mailbox table written in the same transaction as the sender's receipt) | pattern | Durable, atomic with the sender's effect, replayable, auditable. | It is a pattern, not a library; it is the custom part. |

### Context (T5) and budgets (T12)

| Candidate | Fit |
|---|---|
| ADR-031 context operations | The delivery mechanism fits (kernel-invoked, receipted, in the prompt every time). But ADR-031 requires `effect: read`, and inbox delivery must record which run consumed which message. |
| Framework memories (CrewAI short/long-term memory, external memory services via MCP) | Agent-chosen recall; no guarantee that a run sees its inbox or claims. External memory remains available through ADR-031 context operations and is not replaced. |
| ADR-015 reservation ledger (reserve maximum, settle actual, keep unknown) | Fits spend ceilings directly; ADR-033 already meters cost per harness. |
| Windowed rate limiters | Rejected in ADR-015 for the same reason: they cannot join the admission transaction or preserve uncertain consumption. |

## Decision

Compose. Reuse the Kubernetes resource model's shape and change semantics for the manifest, A2A's message model for message bodies, CloudEvents for the events teams emit, Cardea's lease and fencing model for claims, the ADR-015 reservation ledger for spend, and the existing trigger, agent-run, invocation-core and context machinery for execution. The custom parts are the team tables, the reconciler, the work-item state machine and the mailbox, each justified by a gap above.

### 1. Team manifest (T1)

```yaml
apiVersion: cardea/v1alpha1
kind: Team
metadata: {name: platform-team, namespace: team-a}
spec:
  supervisor: sup-1                 # immutable; must equal the caller
  charter: "Shared instructions every role receives."
  spend: {usd: 50, window: day, timezone: UTC}
  context: {max_bytes: 24576}       # default per-run team context budget
  roles:
    - name: lead
      replicas: 1
      instructions: "Prioritise and assign ready work."
      harness: default
      model: model-a
      operations: [tracker.search]  # installed operations exposed as tools
      team_rights: [items.create, items.assign, items.close, messages.send]
      grants: [objects.read, models.invoke, "harness:default", tracker.search]
      bounds: {max_turns: 30, max_cost_usd: 0.5, attempts: 2, timeout_seconds: 900}
      spend: {usd: 10, window: day}
      triggers:
        - schedule: {cron: "0 9 * * 1-5", timezone: UTC, misfire: skip}
        - event: {on: [message.created, item.review_requested]}   # ADR-035
      expect:
        run: [{operation: team.items.assign, min_calls: 0, max_calls: 10}]
        window: [{event: item.assigned, min: 1, window: day}]
    - name: worker
      replicas: 3
      instructions: "Implement your claimed item; submit it for review."
      team_rights: [items.claim, items.submit, items.comment, messages.send]
      claim_lease_seconds: 14400
      # ...
  relations:
    - {from: lead, kind: assigns_to, to: [worker]}
    - {from: reviewer, kind: reviews, to: [worker], required: true}
    - {from: worker, kind: escalates_to, to: lead}
```

1. **Format and ownership.** The shape is the Kubernetes resource model (`apiVersion`/`kind`/`metadata`/`spec`, with `status` written by Cardea). It is validated by a Pydantic model with strict, field-named errors, like every Cardea request, and is accepted as JSON (YAML is a client convenience). It is supervisor-managed like ADR-029 triggers: `PUT /v0.2/teams/{name}` applies it, `GET` returns spec and status, `DELETE` removes the team. The caller must be `spec.supervisor` and hold the new right `teams.manage` (supervisor and platform) in the namespace. `?dry_run=true` returns the plan (item 4) and changes nothing. Host-owner provisioning of teams is out of scope for the first slice (open question 1).
2. **Instances are principals.** Each instance is an agent principal `<team>.<role>.<n>` (`n` from 0), bound to the kernel's runtime, with no external identity: it cannot authenticate to the API and acts only through runs Cardea admits. This is the one new provisioning power: `teams.manage` lets a supervisor create agent principals of exactly this form in its own namespace and nothing else. Principals are never deleted; a removed instance is disabled and its history kept (ADR-024 removal semantics).
3. **Authority stays under the live ceiling.** A role's `grants` become delegated permissions from the team supervisor to each instance (`delegate` mode required on the supervisor's side). Admission of every run computes the existing conjunction: role grants ∩ supervisor's current permissions ∩ delegation authority ∩ platform policy. The manifest is checked against the ceiling at apply (refused with the offending role and grant), and again at every run, so revocation of the supervisor's rights stops every role at its next admission or tool call. `team_rights` are compiled into the role's grants as `team:<right>` (`items.create`, `items.assign`, `items.claim`, `items.submit`, `items.review`, `items.close`, `items.comment`, `messages.send`, `decisions.record`), checked in the same way. **Relations only narrow**: `assigns_to` restricts the roles an assigner may target; `reviews` restricts whose items a reviewer may approve or block, and `required: true` makes approval a precondition of `done`; `escalates_to` names the default recipient of escalations and of unmet-expectation notices. A relation never grants a right the role lacks.
4. **Compilation.** The reconciler compiles each instance to one ADR-029 supervisor-owned `agent-run` trigger per manifest trigger (`team.<team>.<role>.<n>.<k>`), whose template is a full `AgentRunRequest`: `agent_id` = the instance, instructions = charter + role instructions, the role's operations plus the team tools it has rights for, harness, model, bounds, `expect.run`, and the team context sources (item 5). Event triggers compile to ADR-035 subscriptions with the same template. Everything is therefore covered by the existing definition digest, slot identity and idempotency context.
5. **One run per instance at a time.** An instance is a serial worker. A slot or event that finds the instance's previous run unfinished is coalesced: recorded as a `coalesced` trigger event, not admitted. Different instances run concurrently, bounded by the existing `concurrent_sessions` queue.
6. **Applying a change.** `PUT` stores the new spec, increments `metadata.generation` and returns the plan. A restartable reconciler (one DBOS workflow per team, keyed by team and generation) converges and sets `status.observedGeneration` when done. Rules:
   - **In-flight work is never rewritten.** An admitted run keeps its bound request, as for any trigger (ADR-029). New generation values apply from the next admission.
   - **Grant narrowing is immediate**, because grants are evaluated live; widening applies to new runs only.
   - **Scale-down and role removal drain.** The highest ordinals go first (StatefulSet semantics). A draining instance gets no new slots, claims or role-addressed messages. Its current run may finish until `drain_timeout_seconds` (default 1800), after which it is cancelled through the normal cancellation path (uncertain effects stay `outcome_unknown`). Then its claims are released (item 3, epoch bump), its undelivered instance-addressed messages are re-addressed to its role or, if the role is gone, to the role's `escalates_to` target or the supervisor, its triggers are removed and its principal is disabled. Every step is a team event.
   - **Renames are removals plus additions.** Role identity is its name; there is no implicit migration of claims from an old role to a new one.
   - **Invalid generations are refused whole.** A manifest that fails validation or the ceiling check changes nothing.
   - **Deleting a team** drains every instance, then closes open items as `cancelled` with reason `team_removed`; history, messages and decisions are retained.

### 2. Work items (T2)

1. **Model.** `work_items`: `item_id`, `team`, `title`, `body` (≤ 16 KiB), `labels` (≤ 16), `priority` (integer), `state`, `assignee` (a role or an instance, nullable), `holder` and `claim_epoch` and `lease_expires_at` (item 3), `links` (≤ 32 of `{rel, url, external_id, observed_state, observed_at}`, with `rel` one of `pull_request`, `issue`, `document`, `commit`, `artifact` or `other`), `parent_item_id`, `version`, `created_by`, timestamps. `work_item_events` is append-only and records every change with actor principal, run task ID, receipt ID, old and new values. History is that table; it is never rewritten.
2. **Lifecycle.** A small fixed state machine that the kernel enforces, so claims and review mean the same in every team:

   | From | To | Operation | Right and relation |
   |---|---|---|---|
   | — | `open` | `team.items.create` | `items.create` |
   | `open`, `ready` | `ready` (assignee set) | `team.items.assign` | `items.assign`; target role within `assigns_to` |
   | `ready` | `claimed` | `team.items.claim` | `items.claim`; caller is, or is an instance of, the assignee |
   | `claimed` | `ready` | `team.items.release` or lease expiry | holder |
   | `claimed` | `review` | `team.items.submit` | holder, current epoch |
   | `review` | `claimed` (changes requested) | `team.items.review` `block` | `items.review`; `reviews` the holder's role |
   | `review` | `done` | `team.items.review` `approve` | as above; if no `required` reviewer, `team.items.close` by holder |
   | any non-terminal | `cancelled` | `team.items.close` | `items.close` |

   `done` and `cancelled` are terminal; reopening creates a linked new item, as for tasks. Team-specific stages ("triaged", "mitigated") are labels, not states. A blocked review keeps the claim and its epoch, so the same instance continues.
3. **Operations exposed to agents.** Built-in in-process installed operations served on the per-attempt MCP endpoint and receipted through the shared invocation core (ADR-023/026): `team.items.create`, `get`, `list` (filter by state, assignee, label; paged), `update` (title, body, labels, priority, links), `assign`, `claim`, `release`, `submit`, `review`, `close`, `comment`; `team.messages.send`; `team.decisions.record`; and the read-only `team.state`. Effectful ones declare an ADR-026 idempotency scope (for example `create` scoped by `/client_key`, `claim` by `/item_id`), so a retried session cannot create duplicates. Each operation is exposed to a role only when the role holds its `team:` right.
4. **Storage ownership.** Cardea owns all team state in its SQLite store, in tables added by one Alembic migration. Items are authorized by team namespace plus the transition table; reads are filtered by `tasks.inspect`-style rights in the namespace, so the supervisor and clients can inspect them (`GET /v0.2/teams/{name}/items`).
5. **External trackers are mirrors, not owners.** GitHub or Gitea lack an atomic claim, so they cannot hold claims or state. An adapter may:
   - **mirror out**: installed effect operations (ADR-026 scope on `item_id`) create and update an external issue per item, recorded as a link;
   - **import in**: an ADR-035 inbound event (for example an issue labelled for the team) creates an item through `team.items.create` under the adapter's principal;
   - **observe**: an ADR-035 event updates a link's `observed_state` (for example a pull request merged). Observation never transitions an item by itself; a role or a configured rule does (open question 3).
   The adapter contract is a documented set of operation names and arguments, not a kernel plug-in.

### 3. Claims (T3)

1. **Atomic claim.** `team.items.claim` runs in one `BEGIN IMMEDIATE` transaction: re-read the item, check `state = ready`, the assignee condition, the caller's right and that the instance is active, then set `holder`, increment `claim_epoch`, set `lease_expires_at = now + claim_lease_seconds` (role setting, 300–86400, default 14400) and append the event. SQLite's single writer serializes competing claims, so exactly one wins; the loser gets `item_already_claimed` (409) with the current holder.
2. **A claim belongs to an instance, not a run.** It survives across runs, because work on one item usually spans several. It is renewed (lease reset) at each attempt start of a run by the holder, and by `team.items.claim` on an item already held by the caller, which is idempotent.
3. **Fencing.** The holder's runs see `(item_id, claim_epoch)` in their context. Every item operation that requires the holder checks the epoch, and the tool endpoint refuses any `team.items.*` call against an item whose epoch no longer matches with `claim_lost`. Effects on external systems taken for an item should carry `idempotency_context = <item_id>.<claim_epoch>` (ADR-026), so a stale holder's retried effect cannot duplicate the new holder's. Cardea cannot fence effects outside its invocation boundary; that limit is stated, as in ADR-026.
4. **Expiry returns work.** The ADR-024 scheduler tick (one per minute, no new clock) releases expired claims: state `ready`, holder cleared, epoch incremented, event `claim_expired`, and a message to the assigner role's `escalates_to` target if the manifest asks for one. A crash of the holder's run therefore returns the item at most one lease plus one tick later.
5. **Fairness and limits.** An instance may hold at most `max_claims` items (role setting, default 1). There is no automatic reassignment beyond returning to `ready`; who claims next is the next instance to call `claim`.

### 4. Messaging (T4)

1. **Model.** `team_messages`: `message_id` (UUID), `team`, `sender` (principal, run task ID, receipt ID), `to` (`{instance}`, `{role}` or `{team}`), `kind` (`note`, `request`, `escalation`, `decision`, `system`), optional `item_id`, `body` as an A2A 1.0 `Message` `parts` array (text and data parts only, ≤ 16 KiB), `created_at`. Bodies are untrusted data. `team_message_deliveries`: one row per (message, recipient instance) with `state` (`pending`, `leased`, `consumed`, `dead`), `task_id`, `attempt`, `deliveries`.
2. **Sending.** `team.messages.send` (agents, receipted, ADR-026 scope on `/client_key`) and `POST /v0.2/teams/{name}/messages` (the supervisor). The message and its delivery rows commit in the sender's receipt transaction, so a message exists exactly when its send succeeded. `to: {instance}` creates one delivery; `to: {role}` creates one delivery assigned lazily to the first active instance of the role that starts a run (work-queue semantics); `to: {team}` creates one delivery per active instance. Addresses must be team members; there is no cross-team addressing in this record.
3. **Delivery as context.** At attempt start, in the same transaction as the attempt checkpoint (ADR-027 item 2), the kernel leases pending deliveries for the instance, oldest first, up to the inbox budget, recording `task_id` and attempt. They appear in the prompt as an inbox block (item 5). A retried attempt of the same run sees the same deliveries. When the run succeeds, its deliveries become `consumed`; when it fails or is cancelled, they return to `pending` with `deliveries` incremented. After `max_deliveries` (default 3) a delivery becomes `dead` and a `system` message goes to the escalation target. A successful run is the only consumer, so a message is delivered to exactly one successful run per recipient.
4. **Wake-up.** Every message commit emits a CloudEvents-shaped team event (`type: cardea.team.message.created`, `subject: <recipient>`). A role trigger `event: {on: [message.created]}` turns it into a run through ADR-035; coalescing (decision 1.5) avoids one run per message.
5. **Audit and replay.** Messages and deliveries are never deleted by the team logic (retention follows the store's general policy). `GET /v0.2/teams/{name}/messages` pages them with their delivery history; the run view (ADR-029) lists the messages each run consumed.

### 5. Bounded context (T5)

1. **Kernel-assembled blocks.** Before each attempt, after the ADR-031 context operations, the kernel adds team blocks in ADR-031's format and with its untrusted-data preface: `<context source="team.SECTION" as_of="ISO-8601" cursor="N" status="ok">JSON</context>`. These are kernel reads recorded as `ctx-NN` receipts like context operations; only the inbox lease (decision 4.3) writes.
2. **Sections and default budgets** (bytes of the JSON, per run; each configurable per role, and the total capped by `spec.context.max_bytes`, at most 64 KiB):

   | Section | Content | Default |
   |---|---|---|
   | `team.claims` | the instance's claimed items with full body, links, epoch and lease expiry | 8 KiB |
   | `team.inbox` | leased deliveries, oldest first | 6 KiB |
   | `team.recent_runs` | the instance's last 5 runs: outcome, items touched, trimmed result text | 4 KiB |
   | `team.state` | counts by state; items assigned to the role or awaiting the role's review; titles only | 3 KiB |
   | `team.decisions` | the last 10 decisions (`team.decisions.record`: title, summary, item links) | 2 KiB |
   | `team.completed` | items done or cancelled in the last 7 days, titles and links | 1 KiB |

   A section over budget is trimmed by dropping the oldest entries and states `truncated="true"` and how many entries it omitted. Claims and inbox are filled first and are never dropped entirely.
3. **Staleness.** Every block is a snapshot taken in one read transaction at attempt start and carries `as_of` and the team event cursor. During the run the agent can call the read-only `team.state` (and `team.items.get`) to refresh. Messages that arrive mid-run are delivered to the next run, unless the role's trigger wakes one sooner. External memory services remain ADR-031 context operations and are not replaced.

### 6. Expectations and budgets (T12)

1. **Per run.** `expect.run` is ADR-029's `expect`, unchanged.
2. **Per role, windowed.** `expect.window` entries count team events or receipted operations by the role's instances over a calendar window (`day` or `week` in the team's timezone): `{event | operation, min, max, window}`. The scheduler tick evaluates closed windows and records `expectation_unmet` or `expectation_exceeded` team events plus a `system` message to the role's `escalates_to` target (or the supervisor). They never fail runs retroactively.
3. **Spend.** Role `spend` and team `spend` are ADR-015-style reservation accounts, keyed by team, role and window. Admission of a run reserves its `max_cost_usd` against both and is refused with `team_budget_exhausted` when either would be exceeded. Settlement replaces the reservation with the harness-reported or metered cost (ADR-033); unknown cost keeps the full reservation. The sum of role limits may exceed the team ceiling; the team ceiling always binds. This is a metered bound, not a provider invoice guarantee (kernel contract, storage and resource boundaries).

### 7. Dependencies on parallel records

- **ADR-035 (inbound events and event triggers):** role `event` triggers, team events as trigger sources, inbound tracker events for import and observation. Until it lands, roles use time triggers only and messages wait for the next scheduled run.
- **ADR-036 (approval waits):** a role or a transition may require a human approval (for example `review approve` with `approval: required`, or any operation the platform marks as approval-gated). This record only places the hook: the transition waits on an ADR-036 approval and does not invent its own.

## Integration

- **Authority.** New kernel rights: `teams.manage` (supervisor and platform) and `team:<right>` grants. One new provisioning power, limited to runtime-bound agent principals in the supervisor's own namespace. No change to the live-ceiling formula: instance runs are ordinary root agent runs whose supervisor is the team supervisor.
- **State.** One Alembic migration adds `teams`, `team_members`, `work_items`, `work_item_events`, `team_messages`, `team_message_deliveries`, `team_decisions` and `team_spend`. Existing tables are unchanged; triggers gain a `managed_by = team` value.
- **Failure handling.** All transitions are single transactions. The reconciler is idempotent per (team, generation). Expired claims and closed expectation windows are swept by the existing tick. Deliveries are released when their run fails.
- **Compatibility.** Without teams nothing changes. Team runs are standard agent runs, visible in every existing view.
- **Operational cost.** No new service, scheduler or dependency. One DBOS workflow per manifest change; per-minute sweep work proportional to open claims.

## Acceptance tests (to write; none run)

1. **Restructure by manifest only.** Apply a team with lead, reviewer and two workers; run a full item through `open → ready → claimed → review → blocked → review → done`. Then add a third worker, remove the reviewer role and rename nothing else, with no code change: the plan lists exactly those changes, the removed role's in-flight run finishes, its state is drained, `required` review falls away for new transitions, and `observedGeneration` converges.
2. **Same primitives, different team.** An operations-desk manifest (coordinator, two responders, report reviewer) passes the same suite with only a different document.
3. **No double claim.** Twenty concurrent `claim` calls from two instances: exactly one succeeds. Kill the daemon after the claim commits and before the run's first tool call; restart: one holder. Let the lease expire: the item returns to `ready` with a new epoch; the old holder's next `team.items.*` call is refused `claim_lost`; the new holder proceeds.
4. **Message once, across restart.** Send a message; kill the daemon before delivery; restart; the next run receives it in `team.inbox`; that run fails, the following run receives it again (`deliveries = 2`) and succeeds; no later run receives it. A role-addressed message is consumed by exactly one instance; a team-addressed one by each.
5. **Ceiling.** Revoke one grant from the supervisor: the next run of every role using it is refused, and a running one is stopped at its next tool call. A manifest granting a role a right the supervisor cannot delegate is refused at apply.
6. **Relations narrow.** A role with `items.assign` but no `assigns_to` to the target role is refused; a reviewer outside `reviews` cannot approve.
7. **Budgets.** With a team ceiling of two runs' `max_cost_usd`, the third concurrent admission is refused `team_budget_exhausted`; settlement releases unused reservation; unknown cost keeps it.
8. **Context bounds.** Fill every section past its budget: the prompt respects every limit, claims and inbox are present, truncation is declared, and markup in item bodies or messages cannot close a block.
9. **Scale-down drain.** Scale workers 3 → 1 while instance 2 holds a claim and has a running run: no new slots, the run finishes or is cancelled at the timeout, the claim returns to `ready`, instance-addressed messages move to the role.

Portable tests in `apps/api/tests/service/`; one Debian run of tests 3 and 4 with a real daemon restart before calling the record verified.

## Implementation plan (PR-sized slices, dependency order)

1. **Schema and manifest model.** Migration with the team tables; Pydantic `Team` model and validation; no behaviour. Tests: validation errors, round-trip.
2. **Apply, plan and instances.** `PUT/GET/DELETE /v0.2/teams/{name}`, `teams.manage`, instance principals, delegated grants, ceiling check at apply, dry-run plan, reconciler for creation only. Tests 5 (apply half).
3. **Compile roles to triggers.** Time triggers per instance via ADR-029 templates, one-run-per-instance coalescing. Test: scheduled runs per instance with the right agent and grants.
4. **Work items without claims.** Item tables in use, `create/get/list/update/assign/comment/close`, transition table, relations, supervisor read API. Tests 6.
5. **Claims and fencing.** `claim/release/submit/review`, epoch checks at the tool endpoint, lease renewal at attempt start, expiry sweep in the tick. Test 3.
6. **Messaging.** Send (agent and supervisor), deliveries, inbox lease at attempt start, consume/release on run end, dead letters, messages API. Test 4.
7. **Team context blocks.** Sections, budgets, truncation, `team.state` tool, decisions log. Test 8.
8. **Change application.** Scale-down and role removal drain, re-addressing, generation/observedGeneration, team deletion. Tests 1, 9.
9. **Spend and windowed expectations.** Team and role reservation accounts, settlement from harness cost, window evaluation and notices. Test 7.
10. **Event triggers** (after ADR-035): role `event` triggers, team CloudEvents, coalescing.
11. **Approval hooks** (after ADR-036): approval-gated transitions.
12. **Tracker mirror adapter** (after ADR-035): documented operation set and one reference adapter for an external issue tracker. Test 2 runs from slice 8 onward.

## Verification

None yet. This record is a design selection. Each slice adds its tests; status moves to implemented when slices 1–9 pass portably and to verified after the Debian restart run.

## Limits

- Fencing ends at Cardea's invocation boundary. A stale holder's harness process that is already running outside a tool call cannot be stopped by an epoch change alone; it is stopped by its run's lease and the `claim_lost` refusal at its next call.
- Role-addressed messages pick an instance by who runs first, not by load or skill.
- No cross-team messaging or shared items; a cross-team handoff remains ADR-021's separately admitted task.
- Spend ceilings are metered and conservative, not provider bills.

## Revisit

Revisit if:
- a team needs states the fixed lifecycle cannot express with labels;
- an external tracker gains conditional writes (compare-and-set on assignee) and a deployment wants it authoritative;
- DBOS or A2A gains durable store-and-forward addressed to a logical recipient;
- teams need to span hosts, which breaks the single-writer claim argument;
- runs need more team context than 64 KiB, or retrieval instead of recency.

## Open questions for the owner

1. Should host-owner provisioning also install teams (`managed_by = host`), or are teams supervisor-managed only?
2. Is a fixed kernel lifecycle plus labels acceptable, or must teams declare their own states?
3. May an observed external event (for example a linked pull request merged) transition an item automatically, or only a role?
4. Should `teams.manage` be allowed to create agent principals at all, or must the host owner pre-provision instance principals?
5. Default claim lease (4 h) and default team context budget (24 KiB): acceptable starting points?
6. Should a failed run that consumed a message count as delivered for audit purposes beyond the `deliveries` counter (for example notify the sender)?
