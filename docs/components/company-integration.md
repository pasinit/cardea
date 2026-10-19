# Company Process and Data Integration

**Status:** proposed design, 2026-09-25; not implemented. Required for the first product scope, rather than deferred expansion. See [ADR-021](../decisions/021-company-process-data-integration.md) for reuse candidates and open fit gates.

**Purpose.** Make company records, existing processes and permitted actions discoverable and usable by agents through Cardea. The first acceptance scenario spans multiple departments and systems, including their real handoffs.

## Design boundaries

| Layer | Responsibility |
|---|---|
| Company systems | Remain authoritative for their records, permissions and existing process executions |
| Integration adapters | Read/query records, receive changes, preserve source identity/versions, expose authorized actions and reconcile uncertain effects |
| Process/application layer | Register versioned process definitions or external process references, correlate instances, express stages, dependencies, responsibility and outcome checks |
| Kernel | Authenticate and authorize access, bind tasks to supervisors, journal accepted events, persist versioned evidence, enforce budgets and recover execution |

These are logical responsibilities, not a requirement for four services. The kernel needs generic contracts for integrated resources and process-bound work; company-specific concepts such as invoices, customers or employee onboarding remain typed application data. Exact new API/type/storage contracts require fit decisions and versioned migration before implementation.

## Company data

An integrated record needs a scoped source identity, native record ID, schema/version reference, observed revision or observation time, provenance, and access-policy binding. These are information requirements, not a new invented wire format. Preserve native identifiers and explicit relationship evidence; ambiguous cross-system matches remain unresolved rather than merged by a model.

Expose permission-filtered discovery and reads, including authorized links between records. Prefer references and on-demand queries; persist bounded, versioned snapshots when needed for execution/recovery/evidence. A source read permission does not automatically grant model disclosure or cross-team sharing. Cached records, relationship traversal, search results and derived artifacts must retain an effective access policy; source revocation cannot be bypassed through a copy. Declare freshness and fail closed for operations whose current source authority cannot be established.

Adapters need initial synchronization or query, incremental updates, deletions and restart catch-up. Keep provider continuation tokens separate from kernel journal cursors. Authenticate producers, deduplicate by source-scoped delivery identity, and durably acknowledge delivery only after accepted state/events commit. Handle out-of-order updates using source revisions or explicit reconciliation; a webhook envelope alone provides neither ordering nor durable delivery. Data minimization, deletion and retained-evidence rules must be agreed before importing sensitive records.

## Company processes

Support three input forms: an existing executable process model, an external system's running process, or an informal procedure. Reuse a standard model/engine where appropriate. Informal documents can inform a proposed model, but an accountable process owner must validate the interpretation before activation; prose is not executable authority.

Register process definition/version, source references, instance correlation, milestones, dependencies, accountable roles, permitted operations and completion evidence. A process instance can span multiple Cardea tasks, external work and human decisions. Show observed, expected, blocked and uncertain state separately. Pin instances to their definition version; changing a definition must not silently rewrite in-flight work.

Choose one execution owner for each transition. If an existing BPM engine owns a process, Cardea observes it and executes explicitly assigned agent work. If Cardea owns execution, compose the selected durable runtime with a tested process representation. Do not run two schedulers against the same transition or claim arbitrary BPMN import compatibility.

## Authority and actions

Discoverable operations declare input/output schemas, source authorization, disclosure requirements, effect class, reconciliation and verifier. External reads/writes go through capability-checked adapters with protected credentials; read back destination state where appropriate to verify completion. Imported procedures, events and model output cannot grant permissions.

Each task retains one authenticated accountable supervisor and the live ancestor ceiling. Cross-department child work is possible only within that ceiling. If a receiving department needs different authority, design a separately admitted task under its own supervisor, linked by an authorized handoff. The linkage carries no grants and does not convert that task into an overprivileged descendant. Test explicit recipient admission, minimum-data disclosure and separate budget accountability before adopting this pattern. A process owner or correlation ID has no implicit union of departmental permissions.

## Recovery and acceptance

The first proposed acceptance boundary is one company, two or three departments, at least two operational systems, a shared record relationship and one complete process. Demonstrate initial data access, a change-triggered task, a cross-team handoff, a justified downstream update and independently checked outcome. If a chosen job is read-only, explain how its complete outcome still exercises coordination.

Inject duplicate/out-of-order delivery, downtime, expired credentials, source ACL revocation, deletion, schema/definition changes, partial writes and cancellation. Reconstruct process progress from persisted evidence after restart without repeating uncertain effects. Verify cross-team denials and revoked access to derived data as well as successful work. Measure handoff delays and reconciliation effort against the current company process. Integration fit and customer outcome evidence are both still open.
