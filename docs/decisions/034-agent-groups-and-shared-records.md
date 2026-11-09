# ADR-034: Agent Groups, Shared Records, State Machines and Mailboxes

- **Date / owner / milestones:** 2026-11-08; runtime and API maintainers. Builds on [ADR-023](023-installed-operation-registry.md) (installed operations), [ADR-024](024-time-triggers.md) (time triggers), [ADR-026](026-effect-idempotency-and-reconciliation.md) (effect scopes), [ADR-027](027-agent-runs.md) (agent runs), [ADR-029](029-supervisor-schedules-expectations-and-run-evidence.md) (supervisor-managed triggers, expectations), [ADR-031](031-agent-run-context-operations.md) (context operations), [ADR-033](033-pluggable-agent-harnesses.md) (harnesses), [ADR-035](035-event-triggers.md) (event triggers, selected, slice 1 implemented) and [ADR-036](036-approval-waits.md) (approval waits, selected, implementation pending). Relates to roadmap M8.
- **Status:** selected (design); slices 1 (collections and records) and 4 (agent groups) implemented portably on 2026-11-09, see [Implementation status](#implementation-status) and [Verification](#verification); slices 2–3 and 5–11 not implemented; nothing verified on Debian. The lead approved this design on 2026-11-09, and the owner may veto. It was reworked on 2026-11-08 at the lead's and owner's direction. The first version made "team" a kernel concept, with roles, role relations and a fixed work-item lifecycle; that baked one workflow into the kernel and is withdrawn. This version has only generic primitives. Review decisions are recorded under [Carried decisions](#carried-decisions) and [Decisions settled in review (2026-11-09)](#decisions-settled-in-review-2026-11-09). No fit test below has run.
- **Affected components:** [tasks and scheduler](../components/tasks-scheduler.md), [delegation and messaging](../components/delegation-messaging.md), [events and inputs](../components/events-inputs.md), [identity and capabilities](../components/identity-capabilities.md), [resource budgets](../components/resource-budgets.md), [agent runtime](../components/agent-runtime.md).

## Requirement

Cardea should run long-lived sets of agents that cooperate on shared work: a software team, an operations desk, a research group, a discretionary trading desk. **The kernel must know none of these.** A "team" is a composition of three things:
- generic kernel primitives;
- configuration documents;
- an *operations package*: installed operations (ADR-023) that the deployment owns, for example a version-control operation or a broker operation.

Restructuring a team, or creating a different kind of team, must need no Cardea change.

The kernel needs five primitives:

1. **P1 Agent groups.** A declarative manifest that defines:
   - a set of agent principals, with replicas;
   - their grants, delegated under one supervisor's live ceiling;
   - their run templates and triggers (time triggers from ADR-024, events from ADR-035);
   - their budgets and expectations.

   Applying a changed manifest is safe for in-flight work and for removed agents.
2. **P2 Shared records with claims.** Named collections of versioned records, with any JSON Schema and any labels, and compare-and-set updates. An atomic claim carries a lease and a fencing epoch. Expired claims are swept and the record returns to the pool. A holder that is idle but alive keeps its claim.
3. **P3 Configurable state machines.** Each collection may declare its own states and allowed transitions. Each transition names the permission it needs, an optional guard, optional claim effects, optional receipt evidence and an optional ADR-036 approval gate.
4. **P4 Mailboxes.** Durable messages between principals, addressed to one instance, to any member of a pool, or to every member of a group.
   - A message is committed together with the sender's receipt.
   - It is delivered as context and consumed only when the receiving run succeeds; repeated failures send it to a dead letter.
   - Each message emits events that ADR-035 triggers can use.
5. **P5 Group context and spend.**
   - Bounded context blocks built from P2–P4, delivered as ADR-031 context sources.
   - Spend accounts for a group and for its pools.

Who may do what comes only from grants (ADR-010), guards on records and transitions, and ADR-036 approval rules. Nothing comes from named roles or relations.

**Trust boundary.**
- The supervisor is an authenticated principal.
- Group members are runtime-bound agent principals with no external identity. They act only through runs that Cardea admits.
- Record contents, message bodies, model output and inbound events are untrusted data. None of them can grant authority, change a manifest or a collection, or address a principal outside the namespace.

**Recovery.**
- A crash never leaves a record with two holders.
- A message is never lost, and is never consumed by a run that did not succeed.
- Manifest and collection changes converge through a restartable reconciler.

## Candidates and primary evidence

Checked 2026-11-08 against specifications, API references and release metadata. **No executable fit test has run**; each "fit" below is from the source and says so.

### P1 Group manifest

| Candidate | Version, licence | Fit | Gaps |
|---|---|---|---|
| [Kubernetes API conventions](https://github.com/kubernetes/community/blob/main/contributors/devel/sig-architecture/api-conventions.md); [StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) | conventions at `main`; Apache-2.0 | `apiVersion`/`kind`/`metadata`/`spec`/`status`, `generation` and `observedGeneration`, server-side dry run, stable ordinal identity and highest-ordinal-first scale-down: the right shape and change semantics. | A convention, not something to embed; no API server on a single Debian host (ADR-018/030). Reuse the shape and semantics only. |
| Cardea `Provisioning` document | in tree | Already provisions principals, permissions and triggers. | Host-owner only and replace-all; ADR-029 rejected that for client-managed work. No replicas or drain. |
| [CrewAI](https://github.com/crewAIInc/crewAI) `agents.yaml` | 1.15.30 (2026-10-24), MIT | Declarative agent definitions. | An in-process framework that owns the loop and memory, which ADR-027/033 give to harnesses and the kernel; no authority, replicas or durability. Vocabulary only. |

### P2 Records and claims

| Candidate | Version, licence | Fit | Gaps |
|---|---|---|---|
| [GitHub Issues REST API](https://docs.github.com/en/rest/issues) | API `2022-11-28`; hosted, proprietary | Rich item model, assignees, labels, timeline, webhooks. | No conditional write on assignment (documented conditional requests cover reads), so no atomic claim; no lease; authorization outside the live ceiling. Not tested. |
| [Gitea issues API](https://github.com/go-gitea/gitea) | v28.2.0 (2026-10-18), MIT | Same model, self-hostable. | Same missing claim and lease; a second service and database. Not tested. |
| Cardea versioned objects (`objects.write` with expected version) | in tree | Compare-and-set, versions as history, namespace authority. | No query by field or state, no lease, no per-transition authorization. **Reused** for the version and compare-and-set semantics; records add the index, claims and transitions. |
| Cardea task leases and ADR-027 per-attempt lease checks | in tree | Unique holder, expiry, a monotonic identity checked before every mutation and tool call. | A lease covers one task. A claim must outlive a run. **Reused** as the model, with the holder being a principal. |

### P3 State machines

| Candidate | Version, licence | Fit | Gaps |
|---|---|---|---|
| [W3C SCXML 1.0](https://www.w3.org/TR/scxml/) (Recommendation, 2015) | W3C | A standard, declarative statechart format. | Hierarchy, parallel states and an executable-content language far beyond "which transitions are allowed for this record"; no maintained Python implementation found in this check. The flat subset is what is needed. |
| [`transitions`](https://github.com/pytransitions/transitions) | 0.9.3 (2025-07-02), MIT | A maintained Python state-machine library with states, transitions and conditions. | In-memory machines bound to Python objects; conditions are Python callables. The rules here must be data, checked inside a SQLite transaction with authority. The flat table below is smaller than adapting it. Not tested. |
| BPMN engines, Serverless Workflow (ADR-021, ADR-029) | various | Process orchestration. | Already rejected for single-step work in ADR-029. They would also add a second execution owner. A record lifecycle is not a process. |
| [CEL](https://github.com/google/cel-spec), as selected by ADR-036 | `common-expression-language` 0.10.0, selected by the ADR-036 slice-1 fit test | Side-effect-free, terminating expressions over JSON. ADR-036 already uses them for approval conditions. | **Reused** for guards, so Cardea has one expression language. |

### P4 Mailboxes

| Candidate | Version, licence | Fit | Gaps |
|---|---|---|---|
| A2A 1.0 `Message` (`messageId`, `role`, `parts`, `contextId`, `taskId`), SDK 1.1.5 pinned | Apache-2.0 | Standard message body and correlation fields. | Point-to-point delivery to a running agent endpoint. No store-and-forward when the recipient is not running, no pool addressing, no delivery as context. ADR-017 rejected a second A2A task store. **Reused for the body.** |
| DBOS `send`/`recv` (3.0.0) | MIT | Durable, topic-addressed delivery into a workflow. | Addressed to one running workflow. A principal's runs are separate, short-lived tasks. Not tested. |
| CloudEvents 1.0.2 (as ADR-035) | Apache-2.0 | Event envelope. | No durability or addressing. **Reused for emitted events.** |
| Transactional inbox/outbox | pattern | Atomic with the sender's effect; replayable. | A pattern; it is the custom part. |

### P5 Context and spend

| Candidate | Fit |
|---|---|
| ADR-031 context operations | **Reused as the delivery path.** Two amendments are needed: more entries per run with a byte budget (its own revisit trigger), and one built-in source that records delivery leases (decision 5.2). |
| ADR-015 reservation ledger | **Reused** for group spend; ADR-033 already meters cost. |
| Framework memories, external memory services | The agent decides when to recall them, so they are not guaranteed context. External memory stays available as ADR-031 context operations. |

## Decision

Compose them. Reuse Kubernetes' shape and change semantics, A2A message bodies, CloudEvents, CEL as selected by ADR-036, Cardea's object versions and lease model, the ADR-015 ledger, and the existing trigger, agent-run, invocation-core and context machinery. Five custom pieces remain, each justified by a gap above:
- the group reconciler;
- the record and claim tables;
- a flat transition table;
- the mailbox;
- the built-in context sources.

**The kernel contains no workflow vocabulary.** It has no roles, reviews, assignments, orders or states of its own.

### 1. Agent groups (P1)

```yaml
apiVersion: cardea/v1alpha1
kind: AgentGroup
metadata: {name: g1, namespace: ns-a}
spec:
  supervisor: sup-1                   # immutable; must equal the caller
  collections: [items]                # separate Collection documents this group uses (decision 2.1)
  spend: {usd: 50, window: day, timezone: UTC}
  pools:
    - name: worker                    # principals g1.worker.0 .. g1.worker.2
      replicas: 3
      grants: [objects.read, objects.write, models.invoke, "harness:default", "record:items/claim", ...]
      run:                            # an ADR-027 AgentRunRequest template
        instructions: "..."
        operations: [records.get, records.transition, vcs.open_change]
        context: [{operation: records.claimed}, {operation: mailbox.inbox}]
        harness: default
        model: model-a
        bounds: {max_turns: 30, max_cost_usd: 0.5, attempts: 2, timeout_seconds: 900}
        expect: [...]                 # ADR-029, unchanged
      triggers:
        - {schedule: {cron: "0 9 * * 1-5", timezone: UTC, misfire: skip}}
        - {event: {types: [cardea.mailbox.delivered, cardea.record.transitioned]}}
      spend: {usd: 10, window: day}
      expect_window: [{operation: records.transition, min: 1, window: day}]
      drain_timeout_seconds: 1800
```

1. **Ownership.** The manifest is managed by a supervisor, like ADR-029 triggers:
   - `PUT /v0.2/groups/{name}` applies it, `GET` returns spec and status, and `DELETE` removes the group.
   - `?dry_run=true` returns the plan and changes nothing.
   - The caller must be `spec.supervisor` and hold the new right `groups.manage` in the namespace.
   - Host-owner provisioning of groups is later work (carried decision 1).
2. **Members are principals.** Each replica is an agent principal `<group>.<pool>.<n>`.
   - It is bound to the kernel's runtime and has no external identity.
   - `groups.manage` may create principals of exactly that form in the supervisor's own namespace, and nothing else (carried decision 4).
   - Principals are disabled, never deleted.
3. **Authority.** A pool's `grants` are delegated permissions from the supervisor; the supervisor's side needs `delegate`.
   - Every admission computes the unchanged conjunction: grants ∩ the supervisor's current permissions ∩ delegation authority ∩ platform policy.
   - The manifest is checked at apply, and refused with the pool and grant that fail. It is checked again at every run and every tool call.
   - There are no relations. "Only pool X may do Y" means "only pool X is granted Y".
4. **Compilation.** Each member gets one ADR-029 supervisor-owned trigger per manifest trigger. The template is `run`, with `agent_id` set to the member. Event triggers compile to ADR-035 triggers whose source is the kernel's own event journal (decision 7).
   - Templates are complete requests, so they are covered by the existing definition digest, slot identity and idempotency context.
   - **One run per member at a time.** A slot or event that arrives while the member's previous run is unfinished is coalesced: it is recorded and not admitted.
5. **Applying a change.** `PUT` stores the spec and increments `generation`. A DBOS reconciler, keyed by (group, generation), converges and then sets `observedGeneration`.
   - **Running work keeps its bound request.** Narrowing a grant applies at once, because grants are live.
   - **Scale-down and pool removal drain**, highest ordinal first. A draining member gets no new slots, claims or pool-addressed mail.
   - Its current run may finish until `drain_timeout_seconds`. After that it is cancelled through the normal path, and uncertain effects stay `outcome_unknown`.
   - Then its claims are released (decision 2.4), its undelivered instance mail is re-addressed to its pool (or, if the pool is gone, dead-lettered to the supervisor), its triggers are removed and its principal is disabled.
   - A rename is a removal plus an addition. An invalid generation changes nothing.
   - Deleting a group drains every member.

### 2. Collections, records and claims (P2)

```yaml
apiVersion: cardea/v1alpha1
kind: Collection
metadata: {name: items, namespace: ns-a}
spec:
  supervisor: sup-1
  schema: {type: object, properties: {title: {type: string}}, required: [title]}  # JSON Schema 2020-12
  max_record_bytes: 16384
  claims: {lease_seconds: 14400, max_per_holder: 1, max_followups: 6, standalone: false}
  states: [...]           # optional; decision 3
  transitions: [...]
```

1. **Collections** are supervisor-managed with `PUT /v0.2/collections/{name}`, under the same `groups.manage` right, `generation` and dry-run rules. A collection belongs to one namespace and one supervisor.
   - **It is its own document, not part of a group.** Several groups under that supervisor may share it.
   - A group lists the collections it uses in `spec.collections`. Apply checks that each one exists in the namespace and has the same supervisor.
   - Removing a collection that a group still references is refused.
2. **Records.** A record holds:
   - `record_id` and `data`, validated against the schema;
   - `labels` (at most 16) and `links` (at most 32, each `{rel, url, external_id, observed_state, observed_at}` with free-form `rel`);
   - `state`, null when the collection has no state machine;
   - `holder`, `claim_epoch` and `lease_expires_at`;
   - `version`, `created_by` and timestamps.

   `record_events` is append-only and stores actor, task, receipt, old and new values. It is the record's history.
   - **Updates are compare-and-set.** Every write carries `expected_version` and is refused with `version_conflict` if the version has moved. This is the same semantics as `objects.write`.
3. **Built-in operations.** They are in-process installed operations, served on the per-attempt MCP endpoint and receipted through the invocation core (ADR-023/026). Each needs a resource-qualified grant `record:<collection>/<action>`, written in the same form as ADR-033's `harness:<id>`.

   | Operation | Effect | Grant |
   |---|---|---|
   | `records.create` | creates a record in the collection's initial state; ADR-026 scope on `/collection` and `/client_key` (the collection is part of the scope so one key cannot dedupe across collections) | `record:C/create` |
   | `records.get`, `records.query` (filter by state, label, holder, field equality; paged) | read | `record:C/read` |
   | `records.update` (data, labels, links; compare-and-set) | effect | `record:C/update` |
   | `records.transition` (decision 3) | effect | `record:C/<transition>` |
   | `records.claim`, `records.release` (only when `standalone: true`) | effect | `record:C/claim` |
   | `records.claimed` | read: the caller's claims | `record:C/read` |

   The operation names themselves are not grants. A run lists `records.*` in `operations`, and its record grants authorize each call, checked under the live ceiling inside the transaction that reads or writes the record (implemented in slice 1).

   A collection may add a CEL **write guard** per action, over `record`, `args` and `caller` (`id`, `group`, `pool`). One example is a per-record check that a member may update only records it holds: `record.holder == caller.id`. The guard can only narrow what the grant allows.
   - **CEL limits.** Guards use the same CEL evaluator and the same static limits as ADR-036 slice 1:
     - no comprehension macros;
     - at most 512 characters;
     - a fixed function list;
     - compiled and checked when the collection is applied.
   - **Fail closed.** A guard that is false, or that errors at evaluation, refuses the call or transition.
4. **Claims.** A claim sets `holder` to the calling principal.
   - It is atomic. One `BEGIN IMMEDIATE` transaction re-reads the record, checks it is unclaimed, the guard and the holder's limit, then sets the holder, increments `claim_epoch`, sets the lease and appends the event. SQLite's single writer means exactly one of several concurrent claims succeeds. The others get `record_claimed` (409).
   - **It belongs to a principal, not to a run,** so it survives across runs.
   - **Renewal requires evidence the holder is alive.** At each attempt start of a holder's run, the lease becomes at least the attempt deadline plus 120 s. When a holder's run succeeds, the lease becomes now plus the full lease. An idempotent re-claim also renews. Failed or crashed runs renew nothing.
   - **Fencing.** A holder's runs see `(record_id, claim_epoch)` in `records.claimed`. Holder-only operations check the epoch, and a stale holder is refused with `claim_lost`. Effects taken for a record should use `idempotency_context = <collection>.<record_id>.<epoch>` (ADR-026). Fencing stops at Cardea's invocation boundary.
   - **Follow-up wake for idle holders.** A holder only runs on its triggers. The ADR-024 tick therefore admits one coalesced follow-up run when two conditions hold: less than half the lease is left, and the holder has no queued or running run.
     - The follow-up uses the holder's first trigger template, with request ID `<holder>.claim.<record_id>.<epoch>.<n>`, and pays from its budgets.
     - At most `max_followups` follow-ups are admitted per epoch unless the holder records a new event on the record. After that the claim lapses, and the supervisor gets a mailbox message.
     - **Apply-time check:** `lease_seconds ≥ 2 × attempts × timeout_seconds + 240` for every pool granted `claim` on the collection. A manifest that fails it is refused with `claim_lease_too_short`.
   - **Expiry.** The tick releases expired claims: holder cleared, epoch incremented, a `claim_expired` event, and the state set to the acquiring transition's `on_claim_expiry` target (decision 3). A holder that stops renewing loses the record at most one lease plus one tick after its last renewal.
5. **External trackers are mirrors.** GitHub and Gitea lack an atomic claim, so they cannot own records. An operations package may provide three things:
   - **mirror out:** installed effect operations, with an ADR-026 scope on the record, write an external issue per record and store it as a link;
   - **import in:** an ADR-035 trigger runs an agent that calls `records.create`;
   - **observe:** an operation updates a link's `observed_state`.

   Observing never transitions a record by itself (carried decision 3).

### 3. State machines (P3)

A collection may declare a flat state machine. The table is configuration; the kernel only enforces it.

```yaml
states: [s1, s2, s3]
initial: s1
terminal: [s3]
transitions:
  - name: t1                    # grant record:C/t1; the call is records.transition {record_id, transition: t1, args}
    from: [s1]
    to: s2
    guard: "args.x > 0 && record.data.owner != caller.id"   # CEL, optional
    claim: acquire              # acquire | release | holder | none (default none)
    requires_receipt: {operation: pkg.do_thing, in: same_task}   # optional evidence
    approval: {level: always, reason: "...", expires_after_seconds: 86400, on_expiry: resume}   # ADR-036, optional
    on_claim_expiry: s1         # where an expired claim returns the record (with claim: acquire)
```

1. **Semantics.** `records.transition` runs in one transaction. The kernel checks:
   - the caller's live grant `record:C/<transition>`;
   - that `state ∈ from`;
   - the guard, under the CEL limits of decision 2.3. A failing guard (false or an evaluation error) **refuses the transition** with `guard_refused`;
   - the claim rule. `acquire` claims the record as in decision 2.4. `holder` requires the current holder and epoch. `release` requires them and then clears the claim.
   - the evidence, when `requires_receipt` is set: the call must cite a receipt ID of a successful (or reconciled-applied) call of that operation, made by the caller's task, under an idempotency context that names this record.

   It then writes the state and the history row and emits the event. There are no implicit transitions.
2. **Approval gates are ADR-036 rules, not a second mechanism.** A transition's `approval` compiles into an ADR-036 per-grant rule on `records.transition`. Its condition is `args.collection == 'C' && args.transition == 't1'`, combined with the transition's own condition if it has one. The call then follows ADR-036 exactly:
   - the arguments are frozen;
   - it returns `approval_pending`;
   - the wait is durable;
   - an approver holding the `approve` mode on `record:C/t1` decides;
   - the frozen transition is executed once and re-checked against the live ceiling.

   Self-approval is refused by ADR-036's requester exclusion. An approver for `record:C/t1` needs the `approve` mode on that resource-qualified grant. ADR-036 owns approvals, so this is defined there, in [ADR-036, Amendment (2026-11-09): resource-qualified approve](036-approval-waits.md#amendment-2026-11-09-resource-qualified-approve).
3. **Changing the table.** A new collection generation may add states and transitions freely. Removing a state that records still occupy is refused, unless the manifest gives `migrate: {from: old, to: new}`. The reconciler then applies that move as recorded `migrated` events. Records keep their history.
4. **No state machine** means plain records with compare-and-set updates and, if `standalone: true`, standalone claims.

### 4. Mailboxes (P4)

1. **Model.** `mailbox_messages` holds:
   - `message_id` (UUID) and `namespace`;
   - `sender` (principal, task, receipt);
   - `to`, one of `{instance: <principal>}`, `{pool: <group>.<pool>}` (any one member) or `{group: <group>}` (every member);
   - optional `record` (collection and ID);
   - `body`, an A2A 1.0 `Message` `parts` array (text and data parts, at most 16 KiB);
   - `created_at`.

   `mailbox_deliveries` has one row per recipient, with `state` (`pending`, `leased`, `consumed` or `dead`), `task_id`, `attempt` and `deliveries`.
2. **Sending.** Agents send with `mailbox.send` (grant `mail:<address>`, ADR-026 scope on `/client_key`). The supervisor sends with `POST /v0.2/mailbox`. The message and its delivery rows commit in the sender's receipt transaction, so a message exists exactly when its send succeeded.
   - **Least privilege.** An agent may send only to addresses it holds a `mail:<address>` grant for: an instance, a pool or a group, each in the namespace. There is no implicit mail between any two principals of a namespace.
   - A grant for a pool or group address does not cover its individual instances.
3. **Delivery as context.** At attempt start, the built-in context source `mailbox.inbox` leases pending deliveries, oldest first and up to its byte budget. The lease is recorded in the attempt checkpoint transaction, and a pool delivery is bound to the first member that leases it.
   - A retried attempt sees the same deliveries.
   - When the run succeeds they become `consumed`. When it fails or is cancelled they return to `pending`, with `deliveries` incremented.
   - At `max_deliveries` (default 3) a delivery becomes `dead`, and the sender and the supervisor are notified. Redeliveries notify no one (carried decision 6).
4. **Events.** Every send and every delivery-state change appends a kernel journal event with a CloudEvents shape: `cardea.mailbox.sent`, `cardea.mailbox.delivered`, `cardea.mailbox.dead`, with `subject` set to the recipient. Group triggers use them to wake the recipient (decision 6).
5. **Relation to A2A.** A2A remains the wire for delegation to a running agent (ADR-017), with a reply expected. A mailbox is store-and-forward between principals that may not be running. It reuses A2A's message body, so a future adapter could accept A2A `message/send` into a mailbox address. That adapter is not designed here.
6. **Audit.** Messages and deliveries are append-only to agents. The run view lists the messages each run consumed, and `GET /v0.2/mailbox` pages messages with their delivery history.

### 5. Group context and spend (P5)

1. **Built-in context sources** are installed read operations, listed in a run template's `context` like any ADR-031 entry and rendered as ADR-031 blocks with its untrusted-data preface and escaping.

   | Source | Content | Default budget |
   |---|---|---|
   | `records.claimed` | the caller's claims, with full data, epoch and lease | 8 KiB |
   | `mailbox.inbox` | leased deliveries (decision 4.3) | 6 KiB |
   | `runs.recent` | the caller's last N runs: outcome, records touched, trimmed text | 4 KiB |
   | `records.summary` | per collection: counts by state, and records in states the caller may transition from, titles only | 3 KiB |
   | `records.query` | any query, for example the latest entries of a decisions collection | 2 KiB |

   Each block carries `as_of` (one read snapshot at attempt start) and the journal cursor. A block over budget drops its oldest entries and says how many with `truncated`. Agents refresh during a run with the same operations as tools.
2. **ADR-031 amendments.**
   - A context entry gains an optional `max_bytes`, and a run gains `context_max_bytes` (default 24 KiB, at most 64 KiB).
   - The entry limit rises from 4 to 8.
   - `mailbox.inbox` is the only context source that writes. It writes only kernel bookkeeping, the delivery lease, and the run's terminal transition settles that lease. A failed fetch therefore still has nothing external to reconcile.
3. **Spend.** Group and pool `spend` are ADR-015-style reservation accounts per window.
   - Admission reserves `max_cost_usd` against both, and refuses with `budget_exhausted` when either would be exceeded.
   - Settlement replaces the reservation with the reported or metered cost (ADR-033). Unknown cost keeps the full reservation.
   - This is a metered bound, not a provider invoice.
4. **Expectations.** Per-run expectations are ADR-029's `expect`, unchanged. Windowed `expect_window` entries count receipted operations, or `records.transition` calls with a named transition, across a pool over a day or week. At window close they send `expectation_unmet` or `expectation_exceeded` mail to the supervisor. They never fail a run after the fact.

### 6. Kernel events as trigger sources

Record transitions (`cardea.record.transitioned`, `subject` = `<collection>/<record_id>`, data `{from, to, transition}`), claim expiry and the mailbox events are appended to the kernel journal in the same transaction as the change. A group trigger with `event:` compiles to an ADR-035 trigger whose source is the kernel journal (`source = cardea://<namespace>`) instead of an HTTP sender. No signature is needed, because the kernel is the producer. It uses the same matching, the same dedupe on (trigger, source, id), the same admission, and the same `trigger_event` data block. This is a small extension of ADR-035: an internal source beside the HTTP endpoint. **The source is namespace-local.** A trigger matches only journal events of its own namespace, and cross-namespace triggering is a revisit item.

## Integration

- **Authority.** New rights: `groups.manage`, and the resource-qualified grants `record:<collection>/<action>` and `mail:<address>`. There is one new provisioning power: the runtime-bound principals described in decision 1.2. The live-ceiling formula is unchanged, and a group member's run is an ordinary root agent run of its supervisor.
- **State.** One Alembic migration after the current head (`m6_004`) adds these tables:
  - `agent_groups` and `group_members`;
  - `collections`, `records` and `record_events`;
  - `mailbox_messages` and `mailbox_deliveries`;
  - `spend_accounts`.

  Triggers gain `managed_by = group`.

  *Slice 1 (2026-11-09):* because groups and mailboxes are built in parallel, each slice adds its own migration. Migration `m10_records` adds `collections`, `records` and `record_events`, including the claim columns (`holder`, `claim_epoch`, `lease_expires_at`, `followups`), the `state` column and the claim, transition and migration event kinds, so slices 2 and 3 need no further change to these tables.
- **Compatibility.** Without groups or collections nothing changes. ADR-031's amendments are additive.
- **Operational cost.** No new service or scheduler. Each apply runs one DBOS workflow. The per-minute tick gains a sweep over claims and windows.
- **Dependencies.** None new: `common-expression-language` 0.10.0 is already pinned by ADR-036 slice 1.

## Worked examples (configuration only)

Both examples use only the primitives above plus an operations package. Names are illustrative.

### A. Software team

The operations package provides the installed operations `vcs.open_change`, `vcs.read_change`, `vcs.comment` and `ci.status`.

```yaml
kind: Collection
metadata: {name: work, namespace: eng}
spec:
  schema: {type: object, properties: {title: {type: string}, body: {type: string}, assignee_pool: {type: string}}, required: [title]}
  claims: {lease_seconds: 14400, max_per_holder: 1, max_followups: 6}
  states: [open, ready, in_progress, in_review, done, cancelled]
  initial: open
  terminal: [done, cancelled]
  transitions:
    - {name: triage,   from: [open], to: ready, guard: "args.assignee_pool == 'eng-team.dev'"}   # "assigns to developers"
    - {name: start,    from: [ready], to: in_progress, claim: acquire, on_claim_expiry: ready,
       guard: "caller.pool == record.data.assignee_pool"}
    - {name: submit,   from: [in_progress], to: in_review, claim: holder,
       requires_receipt: {operation: vcs.open_change, in: same_task}}
    - {name: request_changes, from: [in_review], to: in_progress, guard: "caller.id != record.holder"}
    - {name: approve,  from: [in_review], to: done, claim: release, guard: "caller.id != record.holder"}
    - {name: cancel,   from: [open, ready, in_progress, in_review], to: cancelled, claim: release}
---
kind: Collection
metadata: {name: decisions, namespace: eng}      # the decisions log is just another collection
spec: {schema: {type: object, properties: {title: {type: string}, summary: {type: string}}, required: [title, summary]}}
---
kind: AgentGroup
metadata: {name: eng-team, namespace: eng}
spec:
  supervisor: sup-eng
  collections: [work, decisions]
  spend: {usd: 40, window: day}
  pools:
    - name: principal              # reviews architecture and changes, records decisions, can block
      replicas: 1
      grants: [objects.read, objects.write, models.invoke, "record:work/read", "record:work/approve", "record:work/request_changes",
               "record:decisions/create", "record:decisions/read", vcs.read_change, vcs.comment, ...]
      triggers: [{event: {types: [cardea.record.transitioned]}}]   # wakes on submit (and other transitions)
    - name: product                # exercises the system, files work
      replicas: 1
      grants: [objects.read, objects.write, models.invoke, "record:work/create", "record:work/read", ci.status, ...]
      triggers: [{schedule: {cron: "0 10 * * 1-5", timezone: UTC, misfire: skip}}]
    - name: lead                   # prioritises and assigns
      replicas: 1
      grants: [objects.read, objects.write, models.invoke, "record:work/read", "record:work/update", "record:work/triage", "record:work/cancel", "mail:eng-team.dev"]
      triggers: [{schedule: {cron: "0 9 * * 1-5", timezone: UTC, misfire: skip}}]
    - name: dev                    # implements claimed work, opens changes
      replicas: 3
      grants: [objects.read, objects.write, models.invoke, "record:work/read", "record:work/start", "record:work/submit", vcs.open_change, ci.status, "mail:eng-team.lead"]
      run: {context: [{operation: records.claimed}, {operation: mailbox.inbox}, {operation: runs.recent}]}
      triggers: [{schedule: {cron: "0 */2 * * 1-5", timezone: UTC, misfire: skip}},
                 {event: {types: [cardea.mailbox.sent]}}]
```

- **The review gate** is the `approve` and `request_changes` transitions. Only the principal pool is granted them, and the guard stops a holder from approving its own work.
- **"Can block"** means that without `approve`, nothing reaches `done`.
- **"Assigns to developers"** is the `triage` guard plus the `start` guard.
- **A human-approved merge** would be an ADR-036 rule on `vcs.merge_change`. It needs no record configuration.

### B. Discretionary trading desk

The operations package provides:
- `risk.check`, a read operation that returns exposure against configured limits;
- `broker.place_order`, an effect with an ADR-026 scope on `/order_ref`, a reconciler that looks the order up at the broker, and a refusal (`risk_limit`) when the order would breach limits;
- `broker.fills`, a read operation.

Broker fill notifications arrive as ADR-035 webhooks.

```yaml
kind: Collection
metadata: {name: orders, namespace: desk}
spec:
  schema: {type: object, required: [instrument, side, qty, limit_price, rationale],
           properties: {instrument: {type: string}, side: {enum: [buy, sell]}, qty: {type: number, exclusiveMinimum: 0},
                        limit_price: {type: number}, rationale: {type: string}, broker_order_id: {type: string},
                        filled_qty: {type: number}}}
  claims: {lease_seconds: 3600, max_per_holder: 4, max_followups: 3}
  states: [proposed, approved, placed, filled, reconciled, rejected, expired, cancelled]
  initial: proposed
  terminal: [reconciled, rejected, expired, cancelled]
  transitions:
    - name: approve
      from: [proposed]
      to: approved
      guard: "record.created_by != caller.id"
      approval: {level: conditional, when: "true", reason: "Every order needs a desk approver", expires_after_seconds: 3600,
                 on_expiry: resume, distinct_from_supervisor: false}
    - {name: reject, from: [proposed], to: rejected}
    - {name: place, from: [approved], to: placed, claim: acquire, on_claim_expiry: approved,
       requires_receipt: {operation: broker.place_order, in: same_task}}
    - {name: fill, from: [placed], to: filled, claim: holder}
    - {name: reconcile, from: [filled], to: reconciled, claim: release, requires_receipt: {operation: broker.fills, in: same_task}}
    - {name: expire, from: [proposed, approved], to: expired}
---
kind: AgentGroup
metadata: {name: desk, namespace: desk}
spec:
  supervisor: sup-desk
  collections: [orders]
  spend: {usd: 20, window: day}
  pools:
    - name: analyst                # proposes orders
      replicas: 2
      grants: [objects.read, objects.write, models.invoke, "record:orders/create", "record:orders/read", risk.check, market.quotes]
      triggers: [{schedule: {cron: "*/30 13-20 * * 1-5", timezone: UTC, misfire: skip}}]
    - name: risk                   # submits proposals for approval; may reject
      replicas: 1
      grants: [objects.read, objects.write, models.invoke, "record:orders/read", "record:orders/approve", "record:orders/reject", risk.check]
      triggers: [{event: {types: [cardea.record.transitioned]}}]
    - name: execution              # places approved orders and reconciles fills
      replicas: 1
      grants: [objects.read, objects.write, models.invoke, "record:orders/read", "record:orders/place", "record:orders/fill", "record:orders/reconcile",
               broker.place_order, broker.fills]
      triggers: [{event: {types: [cardea.record.transitioned]}}, {event: {source: "broker", types: [fill]}}]
```

**Order flow.**
1. An analyst creates an order in `proposed`.
2. The risk agent runs `risk.check` and calls `transition approve`. The ADR-036 rule makes that call wait for a human holding `approve` on `record:orders/approve`; the `distinct_from_supervisor` and requester rules apply.
3. Once approved, the transition executes, and the execution agent wakes on the transition event.
4. The execution agent claims the order with `place`. It calls `broker.place_order`, scoped by the order reference, which enforces risk limits and is reconciled if interrupted. It cites that call's receipt in the transition.
5. A fill webhook wakes the execution agent, which transitions the order to `filled`.
6. The execution agent calls `broker.fills` and transitions the order to `reconciled`.

A crash between placing and transitioning leaves the order `approved`. The effect scope deduplicates a retried placement, and an expired claim returns the order to `approved` with a new epoch.

## Acceptance tests (to write; none run)

1. **Restructure by configuration only.** Apply example A and run one record through `open → … → in_review → in_progress → in_review → done`. Then change only the configuration:
   - set `dev` replicas to 4 and remove `product`;
   - add a state `blocked` with two transitions;
   - remove the state `cancelled`, with `migrate`.

   The dry-run plan lists exactly these changes. In-flight runs keep their requests, the removed pool drains, records migrate, `observedGeneration` converges, and no code changes.
2. **Same primitives, other domain.** Example B passes its flow end to end with fake broker operations. This covers ADR-036 approval (approve, reject and expire), placement deduplicated across a crash, and a fill event through ADR-035. No Cardea change is needed between tests 1 and 2.
3. **No double claim.** Twenty concurrent claims from two members: exactly one wins. Kill the daemon after the claim commits: one holder after restart. Let the lease expire: a new epoch, `claim_lost` for the old holder, and the new holder proceeds.
4. **Idle holder keeps its claim; a failed holder loses it.** Use a weekly schedule and a 1 h lease.
   - An idle, healthy holder keeps its claim, with the same epoch, through follow-up wakes.
   - A holder whose runs all fail loses the claim within lease + 60 s.
   - A holder that never touches the record stops getting follow-ups after `max_followups` follow-ups, and the claim lapses.
   - A lease below the apply-time bound is refused.
5. **Message once, across restart.** Send a message, then kill the daemon before delivery. The next run receives it. That run fails, and the following run receives it again (`deliveries = 2`) and succeeds. No later run sees it. A pool message is consumed by exactly one member, a group message by every member. After the third failed delivery the message is dead-lettered and the sender is notified.
6. **Ceiling.** Revoke one grant from the supervisor: the next run of each pool that uses it is refused, and a running one stops at its next call. A manifest granting something the supervisor cannot delegate is refused.
7. **Guards and evidence.** A guard error refuses the transition. `submit` without a cited receipt is refused. A receipt from another task or record is refused.
8. **Budgets.** With a group ceiling equal to two runs' `max_cost_usd`, the third concurrent admission is refused. Unused reservation is released, and unknown cost is kept.
9. **Context bounds.** Every block respects its budget and the total, truncation is declared, and markup in records or mail cannot close a block.
10. **Kernel-event triggers.** A transition wakes the subscribed member exactly once per event across a restart, and bursts are coalesced.

Portable tests go in `apps/api/tests/service/`. Before the record is called verified, tests 3–5 also run on Debian with a real daemon restart.

## Implementation plan (PR-sized slices, dependency order)

1. **Collections and records:** the migration, the `Collection` model, `PUT/GET` of collections, `records.create/get/query/update` with compare-and-set, write guards and history.
   - Guard evaluation reuses the CEL compilation in `cardea.service.approvals` (ADR-036 slice 1, merged in PR #32). Guards add only their own variables (`record`, `args`, `caller`). There is no second copy of the evaluator or its limits.
2. **Claims:** standalone claim and release, epoch fencing at the tool endpoint, renewal, the expiry sweep, follow-up wakes and the apply-time lease check. Tests 3, 4.
3. **State machines:** `records.transition`, guards, claim effects, `requires_receipt`, and generations with `migrate`. Test 7.
4. **Agent groups:** `AgentGroup` apply and plan, `groups.manage`, member principals, delegated grants, compiled time triggers, one run per member. Test 6.
5. **Mailboxes:** `mailbox.send`, deliveries, `mailbox.inbox` leasing and settlement, dead letters, and the API. Test 5.
6. **Context sources and the ADR-031 amendments:** `records.claimed`, `records.summary`, `runs.recent`, `mailbox.inbox` blocks, and byte budgets. Test 9.
7. **Kernel events as ADR-035 sources:** the journal source, group event triggers and coalescing. Test 10.
8. **Change application:** drain, re-addressing, `observedGeneration`, deletion. Test 1.
9. **Spend accounts and windowed expectations.** Test 8.
10. **Approval-gated transitions** (after ADR-036 slices 2–4 and its 2026-11-09 amendment): transition `approval` compiled to ADR-036 rules. Test 2.
11. **Reference operations packages and examples:** fake `vcs.*` and `broker.*` operations for the tests, and both examples as getting-started material.

## Implementation status

### Slice 4: agent groups (implemented 2026-11-09, portable tests only)

- **Migration `m8_groups`** (`down_revision = "m6_004"`): `agent_groups` (primary key `(namespace, name)`, `generation`, `observed_generation`, spec and digest) and `group_members` (one row per member principal, state `active`, `draining` or `removed`); both refuse deletes. `triggers.managed_by` gains `group`. `trigger_events` gains the kind `coalesced`, which carries the slot and the unfinished run that absorbed it. An index `task_bindings(agent_id)` serves the one-run-per-member check. Only the group tables are added here; collections, records, mailboxes and spend accounts come with their slices.
- **Manifest.** `AgentGroup` (`apiVersion: cardea/v1alpha1`, `kind`, `metadata {name, namespace}`, `spec`, `status`) is validated by Pydantic and sent as JSON. Server-owned fields (`metadata.generation`, timestamps, `status`, `schema_version`) are accepted and ignored, so a `GET` body applies again unchanged. A pool's `run` is the ADR-027 request without its per-slot fields: `objective` (defaulted), `instructions`, `operations`, `context`, `harness`, `model`, `expect` and `bounds {max_turns, max_tool_calls, max_cost_usd, attempts, timeout_seconds (per attempt), run_timeout_seconds}`. Bounds: 16 pools, 32 replicas per pool, 64 members, 8 triggers per pool.
- **API.** `PUT /v0.2/groups/{name}[?dry_run=true]` returns `GroupApplied {dry_run, change (created|updated|unchanged), generation, plan {members_added, triggers_installed, triggers_updated, triggers_removed}, group}`. `GET /v0.2/groups/{name}?namespace=` returns `GroupView` with `status {observedGeneration, members[{principal_id, pool, ordinal, state, disabled, active_task_id, triggers}]}`. `DELETE /v0.2/groups/{name}?namespace=` is refused with 409 `group_drain_unsupported` until slice 8.
- **Authority.** The caller must be `spec.supervisor` and hold `groups.manage` in the namespace, and platform policy must allow it there. `groups.manage` creates only principals `<group>.<pool>.<n>`: kind `agent`, bound to the kernel's runtime, with no external identity. An existing principal of that name that is not this group's member is refused (`principal_conflict`). Every pool grant is checked against the platform and the supervisor's `allow` and `delegate` modes; a failure names the pool and grant (`group_grant_denied`). Each compiled request then passes the same admission checks as a submitted run. Every slot re-runs full admission, and every tool call re-checks the ceiling, so revoking a grant or disabling the supervisor stops the members. A member's runs are admitted only for its group's supervisor in its namespace (`member_binding`).
- **Compilation.** Each member gets one ADR-029 trigger `<member>.t<i>` per manifest time trigger, `managed_by = 'group'`, so `DELETE /v0.2/triggers` and provisioning cannot change it. A trigger dropped from the manifest is removed; in-flight runs keep their request.
- **One run per member.** Admission refuses a member that has an unfinished run with 409 `member_busy`. The scheduler records such a slot as `coalesced` against that run and closes the slot. A manual submission gets the 409.
- **Reconciler.** Create, update and scale-up converge inside the apply transaction, so `observedGeneration` equals `generation` when it commits. A dry run executes the same transaction and rolls it back, so its plan and refusals are those of the real apply. Scale-down, pool removal and deletion need drain and are refused with 409 `group_drain_unsupported` until slice 8, which also brings the DBOS reconciler keyed by (group, generation).
- **Refused until their slices** (422, never silently ignored, because each is a bound the kernel would not yet enforce): `spend` (`group_spend_unsupported`, slice 9), `expect_window` (`group_expect_window_unsupported`, slice 9), and `event` triggers (`group_event_trigger_unsupported`, slice 7). Group event triggers compile to kernel-journal sources (decision 6), so ADR-035's rule that HTTP event triggers are host-owned does not apply to them; they wait for slice 7, not for host ownership.
- **Explicit run grants.** A pool's `grants` are the run's complete grants; nothing is added. An ADR-027 agent run needs `objects.read`, `objects.write` and `models.invoke`, a grant for every exposed and context operation, and `harness:<id>` for a non-implicit harness. The worked examples above list the first three.
- **Collection check** (added with slice 1). Apply refuses a `spec.collections` entry that does not exist in the namespace under the group's supervisor, with `422 collection_not_found`. Refusing to remove a referenced collection waits for collection deletion, which no slice has yet.
- **Decisions in review (lead, 2026-11-09; the owner may veto).**
  1. Member IDs stay `<group>.<pool>.<n>`. Principal IDs are global, so a group name already used in another namespace is refused with `principal_conflict`. Namespacing member IDs is a revisit item.
  2. No grants are added automatically; explicit grants are least privilege (see above).
  3. Every member admission also requires the group supervisor's live `groups.manage`, and platform policy's, in the namespace. Revoking it stops the members at their next slot.
  4. The migration name `m8_groups` stands. Whichever concurrent migration merges second re-points its `down_revision`.
- **Tests** (`apps/api/tests/service/test_groups.py`): apply creates principals, members and triggers; dry run changes nothing and refuses what apply refuses; restructure by manifest only (scale a pool up, add a pool, change the model, drop a trigger); scale-down, pool removal and deletion refused; later-slice fields refused; `groups.manage` and supervisor checks; principal conflict; HTTP routes and the `GET`→`PUT` round trip; the ceiling at apply (naming pool and grant) and at every run; a disabled supervisor stops a running member at its next call and refuses its next slot; slot coalescing and re-admission after the run ends; member binding and `member_busy`; append-only history after the migration. Acceptance test 6 is covered for time triggers. Not run on Debian.

## Verification

Slice 4 has portable tests only (above). Status moves to implemented when slices 1–10 pass portably, and to verified after the Debian runs.

**Slice 1 (2026-11-09), implemented, portable only.**
- *Implemented:* migration `m10_records` (`down_revision = "m9_approvals"`); `Collection` documents with `PUT /v0.2/collections/{name}` (`groups.manage`, generation, `?dry_run=true`) and `GET`; JSON-Schema-validated records with labels and links; built-in `records.create`, `records.get`, `records.query` and `records.update` as in-process operations on the per-attempt tool endpoint and as operation tasks, receipted by the invocation core with an ADR-026 scope on create and a history-based reconciler for interrupted writes; compare-and-set on `version`; CEL write guards for `create` and `update` through `cardea.service.approvals.compile_condition` with the guard variables, failing closed; append-only `record_events` with `GET /v0.2/collections/{name}/records/{id}/events`; `record:<collection>/<action>` grants under the live ceiling.
- *Tests* (`apps/api/tests/service/test_records.py`, 25 tests): compare-and-set conflict; schema and size validation; guard refusal and fail-closed evaluation errors; history immutability (`UPDATE`/`DELETE` refused by SQLite triggers); per-call grant checks, another collection's grant, undelegable grants and revocation between admission and dispatch; an idempotent retried create in one idempotency context; reconciliation from history; and an agent run that uses record grants over the real per-attempt MCP endpoint.
- *Not yet:* claims, state machines, transition guards, groups, mailboxes, context sources, spend; collection deletion; host-owner provisioning of collections. No acceptance test from the list above runs end to end yet, because each needs later slices.

## Limits

- Fencing ends at Cardea's invocation boundary. It cannot stop a stale harness process between tool calls; the run's lease and `claim_lost` stop it at its next call.
- Pool addressing picks the first member to run, not the least loaded.
- Collections and mailboxes are per namespace. Cross-namespace handoff remains ADR-021's separately admitted task.
- Follow-up wakes spend model budget while a holder waits; `max_followups` and the budgets bound that cost.
- Spend ceilings are metered, not provider bills.

## Revisit

Revisit if:
- a domain needs hierarchical or parallel states, in which case evaluate SCXML;
- an external tracker gains conditional writes and a deployment wants it authoritative;
- DBOS or A2A gains durable store-and-forward to a logical recipient;
- groups must span hosts, which breaks the single-writer claim argument;
- runs need more than 64 KiB of context, or retrieval instead of recency;
- approver sets need relationships that grants cannot express, in which case evaluate OpenFGA (as ADR-036 says);
- a deployment needs kernel journal events in one namespace to trigger groups in another (decision 6 keeps them namespace-local);
- group names must repeat across namespaces, which needs namespaced member principal IDs (slice 4 refuses the clash).

## Carried decisions

The lead decided these on 2026-11-08 for the first version, and they still apply; the owner may veto any of them.

1. Groups and collections are managed by a supervisor only in the first slices. Host-owner provisioning comes later.
2. Item lifecycles are configuration. The first version's fixed lifecycle is replaced by decision 3; labels remain for domain stages.
3. An observed external event never transitions a record by itself; agents do.
4. `groups.manage` may create member principals of exactly the form `<group>.<pool>.<n>`, runtime-bound, in the supervisor's own namespace.
5. Defaults: a 4 h claim lease with follow-up wakes and the apply-time check, and a 24 KiB context budget.
6. Only a dead letter notifies the sender; redelivery does not.

## Decisions settled in review (2026-11-09)

The lead decided these on 2026-11-09; the owner may veto any of them.

1. **Collections are separate documents.** They can be shared across groups, and a group references the ones it uses (decision 2.1).
2. **Resource-qualified `approve`** (for example `record:orders/approve`) is an amendment to ADR-036, because ADR-036 owns approvals. See [ADR-036, Amendment (2026-11-09)](036-approval-waits.md#amendment-2026-11-09-resource-qualified-approve) and decision 3.2.
3. **CEL guards** use the same evaluator and the same static limits as ADR-036 slice 1:
   - no comprehension macros;
   - a 512-character cap;
   - a fixed function list;
   - fail closed.

   A failing guard refuses the transition (decisions 2.3 and 3.1).
4. **Mail requires `mail:<address>` grants.** There is no implicit any-to-any mail within a namespace (decision 4.2).
5. **Kernel journal events stay namespace-local** for now. Cross-namespace triggering is a revisit item (decision 6).
