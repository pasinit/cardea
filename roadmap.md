# Agent OS Roadmap

Updated 2026-09-25. Milestones are dependency-ordered, not calendar promises. M0–M5 are completed infrastructure profiles; M6–M10 are the proposed product sequence for both individual company applications and expansion across departments. See [product direction](product.md), [gaps](docs/product/gaps.md) and [validation](docs/product/validation.md).

Update 2026-10-15: work since M5 has closed kernel gaps for running a first client application's recurring agent work (installed operations, time triggers, remote TLS API, effect reconciliation, agent runs, worker profiles, supervisor schedules, small-host profile). See [ADR-023 to ADR-030](docs/decisions/README.md); most are verified by portable tests only, not yet on Debian.

## Current Baseline

`cardea.kernel` preserves v0.1 records and durable SQLite state. `cardea.service` now provides the M1 authenticated Unix-socket API/client, persisted supervisor provenance, live authority ceilings, deterministic DBOS execution and whole-store recovery. See [M1 quickstart](docs/m1-quickstart.md). M2 adds confined SHA-256/copy workers with inline or explicitly granted immutable-object input, transactional host allocations and durable staging retirement. M3 adds the bounded OpenRouter/native MCP incident task. M4 implements bounded native A2A child delegation; broader tools remain later work.

## Milestone Completion Rule

Every milestone requires both its behavioral acceptance evidence and the relevant open-source-first decisions from the [review register](docs/standards-review-2026-09-22.md). Record evaluated alternatives, versions/licenses, fit results, and bounded custom exceptions using the [decision template](docs/decision-record-template.md). Candidate research alone is not a selected or verified implementation. Later choices may remain open until their work begins; already-written custom code is not an exemption.

## M0 — Contracts and Standards Review

Status: version 0.1 contract implementation and R01–R03 selection gate complete. [ADR-001](docs/decisions/001-m0-contracts.md) selects SPIFFE/OIDC, HTTP/OpenAPI over a Unix socket, and versioned JCS adoption while preserving v0.1 digests. Its 27 local fit tests pass. The authenticated API, provider integration, and new versioned schemas remain M1 work; this is not a shipped external API.

Reuse gate: R01–R03. Select standard identity and wire contracts, record library/provider integration work due in M1, and decide digest compatibility/versioning. Establish the later R04–R12 evaluation work without claiming those implementations selected.

- Define task transitions, identities, capabilities, operation envelopes, objects, receipts, events, and budget semantics from the kernel contract.
- Pin the Debian release and a supported execution profile; record required host facilities.
- Select one model adapter, credential mechanism, and bounded invocation interface.
- Define authenticated local session binding before implementing the transport in M1.
- Separate the future kernel package from legacy workflow models.

Exit: versioned schemas and executable contract tests, plus selected identity/wire/digest contracts with compatibility or migration plans. Version 0.1 code and ADR-001 satisfy this gate. M1 subsequently implemented and tested the selected integrations and migration before publishing the separate v0.2 authenticated contract.

## M1 — Durable Headless Kernel

Status: **complete for the bounded headless profile**, recorded in [ADR-010](docs/decisions/010-m1-authenticated-kernel.md). Real SPIRE/OIDC authentication, private socket service and structured client, immutable supervisor/task provenance, transactional Casbin ceilings, deterministic DBOS dispatch, durable waits, cancellation/deadlines, independent verification, migration and shared-store backup/restore are implemented. Native macOS and Debian 13 amd64 container acceptance are recorded separately. No model, arbitrary tool execution or host-confinement guarantee is included.

Reuse gate: R01–R05 and M1 portions of R08/R12 complete; bounded R06 evaluation/enforcement was pulled forward because authenticated admission cannot bypass the mandatory supervisor ceiling. ADR-001 selects contracts; ADR-002–007 compare/select workflow infrastructure; ADR-008 verifies real Cardea/DBOS commit integration; ADR-009 verifies SPIRE bootstrap/rotation; ADR-010 closes service/provider integration, live policy, native transactional wakeups, Alembic compatibility and operational ownership. Earlier evidence remains historical and unchanged; later milestones retain their own reuse/acceptance gates.

- Integrate or extend the existing SQLite persistence, task/object state, receipts, and journal only after the relevant reuse decision.
- Add authenticated local submission/inspection/cancellation and a structured client; persist the accountable supervisor and delegation provenance as part of task admission.
- Implement a deterministic runtime adapter and restart recovery at checkpoint boundaries.
- Commit state transitions and associated events atomically; reconcile artifact publication failures.

Exit: submit a task, persist progress, restart the service, and resume without losing acknowledged state. No UI and no model dependency.

Post-M1 validation: [ADR-011](docs/decisions/011-m1-load-testing.md) adds the independent [load package](packages/cardea-load/README.md), an immutable measured baseline and [result report](packages/cardea-load/reports/2026-09-24-m1/REPORT.md). This extends operational evidence for the existing bounded kernel; it does not advance the M2 host-isolation gate.

## Protocol Boundaries — MCP in M3, A2A in M4

Status: native MCP is implemented and accepted for M3’s bounded incident profile. Native A2A is implemented and accepted for M4’s bounded delegation profile. The immediate kernel-protocol prototype was discarded on 2026-09-24. The authenticated kernel API remains the control interface for admission, authority, lifecycle and storage.

[R13](docs/standards-review-2026-09-22.md#r13--mcp-and-a2a-interoperability--m3m4) evaluates native SDKs when the corresponding interaction exists: MCP for an agent consuming tools/resources in M3, A2A for one agent communicating with another in M4. Neither is a prerequisite for M2 or a proposed replacement for the kernel API. Prefer maintained SDK types, transports, discovery and errors; justify only the remaining authority, receipt and recovery integration. Pin versions and record fit evidence at each milestone.

## M2 — Authority and Debian Execution

Status: **complete for the fixed Debian execution profile**, with the requirement-by-requirement [M2 acceptance report](experiments/m2/M2-REPORT.md). Reuse gates R06/R07 and the host/admission portion of R09 are closed by ADR-010 and ADR-012–015. [ADR-012](docs/decisions/012-m2-host-confinement.md) selects systemd + bubblewrap; [ADR-013](docs/decisions/013-installed-host-operations.md) integrates the privileged helper, live authority and crash fencing; [ADR-014](docs/decisions/014-object-scoped-host-inputs.md) adds exact-version input grants; [ADR-015](docs/decisions/015-m2-accounting-retention-and-acceptance.md) closes the two-operation registry, transactional accounting and recoverable worker-payload retirement.

The fixed registry exposes `host.sha256` and `host.copy`, with scoped immutable inputs and task-owned outputs. Real Debian acceptance passes 20 authenticated integration scenarios and 11 hostile confinement probes; portable authority/accounting/migration tests and the 46 real-provider M1 regressions also pass. Arbitrary host paths/plugins are outside the declared MVP. M3 supplies provider budgets and M4 supplies bounded child sharing; M5 subsequently completes packaging for the supported profile.

- Implement scoped grants, expiry/revocation, discovery filtering, and enforcement at operation dispatch. Effective task authority must never exceed its accountable supervisor’s current permissions/delegation authority; deny overbroad grants and propagate supervisor disablement/revocation through retries and resume.
- Integrate a fixed installed operation registry and selected confinement components through a thin host adapter.
- Add process-tree cancellation, deadlines, output/resource limits, and deny-by-default worker network access.
- Ensure workers cannot access kernel storage, model credentials, or unrelated task data.

Exit: real Debian tests prove authorized work succeeds and cross-scope I/O, symlink escape, network escape, stale authority, and resource-limit violations are blocked. Unsupported hosts fail explicitly. This gate precedes real autonomous execution.

## M3 — First Agent-Native Task

Status: **complete for the bounded incident profile** under [ADR-016](docs/decisions/016-m3-openrouter-and-native-mcp.md). OpenRouter `z-ai/glm-4.7`, native MCP, durable incident rounds, model/tool reservations, additive input and confined transformation pass portable, deterministic Debian and live restart/new-input acceptance. The matched load reference/comparison also passes. See the [M3 report](experiments/m3/M3-REPORT.md) and [usage and limits](experiments/m3/README.md). R08–R10 and the bounded MCP runtime portion of R13 are closed; remote MCP servers and general planning are not claimed.

- Connect one real model runtime via the trusted broker.
- Integrate a maintained native MCP client SDK in the trusted broker: the agent discovers permitted tools/resources, proposes a call, and the broker checks live task authority, disclosure and budget before contacting an approved MCP server. Credentials remain with the broker; server descriptions and tool annotations confer no authority.
- Start with one bounded tool server and one real agent task. Record dispatch, request identity, returned artifacts and verification evidence through the existing kernel. Reconcile uncertain external outcomes before retry; support optional asynchronous Tasks only after a separate fit test. An MCP server exposing selected Cardea tools is conditional on a concrete agent consumer, not a generic kernel front door.
- Feed bounded authorized context and typed operation schemas; validate every request.
- Reserve model/tool budgets before dispatch and record usage and receipts.
- Implement durable event waiting and independently verified completion.
- Run the restart-and-new-input scenario from the MVP.

Exit: both deterministic fault-injection tests and the real-agent scenario pass on Debian. MCP calls must also preserve receipts and recovery across disconnects/retries, propagate cancellation, and reject unverified results as success. Report observed model usage, latency, restarts, retries, and human interventions.

## M4 — Delegation and Coordination

Status: **complete for the bounded delegation profile** under [ADR-017](docs/decisions/017-m4-native-a2a-delegation.md). R11 and the A2A portion of R13 reuse native SDK discovery/messages/tasks, existing DBOS scheduling/wakeups, SQLite state and live Casbin authority. No new mailbox or competing task store is needed. The installed deterministic coordinator delegates up to four inline host jobs to a distinct logical worker agent on one Debian host. See the [M4 report](experiments/m4/M4-REPORT.md) and [usage](experiments/m4/README.md).

- Add bounded child tasks, capability attenuation, shared budget ceilings, and parent cancellation behavior. Enforce the live parent and original supervisor ceilings transitively, including revocation races and recovery.
- Integrate maintained native A2A SDK client/server interfaces at agent runtimes: Agent A discovers an approved Agent B, sends a bounded objective/input message, follows task/status/artifact updates and requests cancellation. Cardea's kernel authorizes and records the delegation; it is not itself the peer agent.
- Start with two controlled agents whose identity, authority attenuation, budget reservation and cancellation/revocation behavior can be demonstrated. Persist the association between the local child task and the peer's task/message IDs; a peer task is not a second owner of local execution. An inbound request needs an authenticated supervisor/delegation binding before local admission. Cards and context IDs do not establish that binding. Independently verify returned artifacts before completing the local objective.
- Add scoped mailboxes, correlation IDs, duplicate delivery handling, and conflict-safe shared object updates only for gaps left by the selected A2A and workflow/event components.

Exit: a parent delegates independent work, survives a child failure, and completes without authority or budget amplification. A2A client/server interoperability must additionally preserve cancellation, revocation and recovery without duplicate effects or false verified success. Reject delegation to a peer when the required authority and recovery guarantees cannot be established. No claim of general multi-agent support before this gate.

## M5 — Packaging and Portability

Status: complete for the supported single-Debian-host profile under [ADR-018](docs/decisions/018-m5-debian-package.md) and [ADR-019](docs/decisions/019-m5-operational-recovery.md). R04/R12 operational gates are closed: installed package upgrade and incompatible downgrade refusal, interrupted host effects and durable waits across a full reboot, artifact-checked fenced restore, fresh authority/budgets, private diagnostics and explicit retention pass. See the [M5 report](experiments/m5/M5-REPORT.md) and [operating guide](docs/operations.md).

- Package the Debian service, private storage, configuration, upgrades, backup/restore, and operational diagnostics.
- Add a supervisor UI only where it improves inspection or authority management.
- Implement a second host adapter when needed; run the contract suite and publish supported execution profiles.
- Port the escalation demo as an application only if it validates a real kernel use case.

Exit: a reproducible Debian installation passes the acceptance suite after reboot. A second Unix host is supported only for guarantees its adapter demonstrates.

## Product Milestone Rule

M6–M10 must meet both technical/reuse gates and observed user-outcome gates. Infrastructure tests, synthetic demos and a candidate comparison do not establish adoption. [ADR-020](docs/decisions/020-company-product-direction.md) records the initial alternatives; new technology remains unselected until its fit tests pass. Initial product scope is one company on a supported Debian host with a process spanning multiple departments and systems. The proposed boundary is two or three departments and at least two operational systems. [ADR-021](docs/decisions/021-company-process-data-integration.md) adds process/data integration requirements and supersedes the single-team starting sequence. Required controls move earlier if the chosen job needs them.

## M6 — Validate the First Company Job

Status: active discovery; product assessment and candidate shortlist complete, customer/job evidence open. Owner: product lead with a workflow owner. Gaps: G1, G7, G12–G13, requirements for G2–G11.

- Observe a recurring cross-team process, name its departmental supervisors, operators and buyer, inventory its systems/data/process definitions, and complete the [job contract](docs/product/validation.md#m6-job-contract).
- Measure the current process and best adequate existing automation. Set quality, human-effort, cost and operating thresholds before implementation.
- Choose a complete cross-team process based on measured need; design shared resource references, process state, handoffs and source-authority propagation. Record company controls and standards-first process/data fit decisions. Customer discovery can continue alongside bounded technical fit work.

Exit: a willing company with participating departments, observed process examples, related source records, handoff map, baseline and agreed acceptance rubric; integration and authority contracts with required data/access/retention boundaries. The first application must exercise the complete cross-team process. Seek three prospective-company perspectives; report any missing external evidence. Do not close M6 from documentation alone. Reconsider the application if existing automation already meets the need adequately.

## M7 — Connected Company Process

Status: planned, depends on the M6 job contract and relevant reuse decisions. Owner: application/runtime and identity maintainers. Gaps: G2–G4, G6–G10, G12–G13 across the first participating departments.

- Integrate the selected process definition or external process instance and its related records through the [company integration layer](docs/components/company-integration.md). Use maintained runtimes/connectors; declare schema/definition versions, capabilities and result verification.
- Onboard participating departments, accountable supervisors, operators and agents using company identity. Connect at least two required operational systems, preserve source ACLs and disclosure rights, and expose permission-filtered related records and changes.
- Deliver a real cross-team handoff, scoped visibility and shared process progress. Required downstream actions need duplicate protection, uncertain-effect reconciliation and destination verification. A process owner or connector credential cannot union departmental permissions.

Exit: participating owners accept a complete cross-team result against the baseline; operators can repeat it from instructions. Demonstrate supervisor/source-access revocation including derived records, denied cross-team access, duplicate/out-of-order changes, budget exhaustion and restart/uncertain-effect behavior on the installed profile. Settle actual data retention and release-use terms before a customer installation. No broad plugin or framework compatibility claim follows from one adapter.

## M8 — Repeatable Work and Daily Supervision

Status: planned, depends on M7. Owner: runtime and supervisor-client maintainers with the workflow owner. Gaps: G5–G7, G11.

- Add the actual job's event/schedule delivery, longer task lifetime if needed, catch-up behavior and exception notifications through standards-first reuse.
- Let authorized people and agents find their work, understand blocked reasons, inspect evidence/cost and take permitted recovery actions. Add a UI only where it improves this job; keep API parity.
- Run the matched quality/cost evaluation and repeated-use observation from the validation protocol. Test expected concurrency, latency, restart and recovery targets; investigate the existing M4 admission-p95 warning against this workload.

Exit: recurring work meets the predeclared outcome/effort/cost thresholds over the agreed observation period, with recorded failures and interventions. The owner chooses to keep using it. No operator must repair databases or edit provisioning files for ordinary work.

## M9 — Second Process and Wider Company Coverage

Status: planned, depends on M8 evidence. Owner: product, identity and application maintainers. Gaps: G3, G8–G9, G12–G13 across additional processes; extend controls already required in M7.

- Install a second cross-team process/application, reusing existing company integrations and adding systems or departments where useful, without adding business nouns to the kernel. Measure integration effort and document any shared extension separately.
- Extend the scoped team visibility, attributable budgets, membership/offboarding and explicit supervisor-handoff behavior delivered for M7. Handoff must preserve old provenance and cannot revive authority, reset budgets or bypass live ceilings.
- Test concurrent teams, cross-team denials, source-access changes, application disablement/update and recovery. Reuse shared context and relationships across processes while preserving source provenance and current access.

Exit: both processes operate with shared administration and reusable integrations; one team's users, data or resources cannot leak into the other's authority. Disablement restricts affected descendants. Document what was reused and what required new code. Rework the application boundary if each new application needs privileged business-specific changes.

## M10 — Private Company Release

Status: planned, depends on M8/M9 and pilot-required operational controls. Owner: release/operator and product lead. Gaps: G10–G11 and remaining pilot requirements.

- Run at least two independent company pilots with named support owners, outcome reports and explicit rollout/stop criteria. Confirm who buys, installs and maintains Cardea and test willingness to pay; do not infer demand from a successful demo.
- Demonstrate onboarding from documentation, supported application updates, backup/restore, data lifecycle/export, diagnostics and incident recovery against each pilot's declared operating targets.
- Resolve Cardea's distribution license, dependency/artifact terms and release/support policy. Record security review and capacity evidence for the shipped scope.

Exit: pilots meet the predeclared adoption and operating gates, operators can run the documented product, and the release declares supported workloads, guarantees and exclusions. Multi-host/HA, hosted SaaS and broader Unix support follow demonstrated needs and separate decisions, rather than being assumed company requirements.

## Immediate Product Task

Complete P01/P02 in the [product backlog](docs/product/validation.md#first-backlog): map an observed process across departments and systems, inspect related records and handoffs, define integration/authority contracts and record the current-process/existing-solution baseline. The assessment and M6–M10 roadmap are written; real examples, participants and results remain open. Continue bounded reuse fit work where independent, but do not substitute another synthetic demo for product evidence. Operate existing M0–M5 profiles using the [guide](docs/operations.md).
