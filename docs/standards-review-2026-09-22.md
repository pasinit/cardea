# Open-Source-First Design Review

Date: 2026-09-22. Scope: all repository-owned Markdown documentation and M0–M5. Dependency caches and generated files are excluded. The initial document review is preserved below; subsequent R01–R03 selection and local fit results are recorded in [ADR-001](decisions/001-m0-contracts.md). Neither establishes deployed authentication or a dependency security audit.

## Findings and Disposition

- **Policy was too easy to bypass.** The rule now lives in root `AGENTS.md`, canonical instructions, role handoffs, and milestone acceptance. New/custom mechanisms require a [decision record](decision-record-template.md).
- **M0 completion overstated the frozen design.** The initial review reopened identity, wire format, and canonicalization decisions. ADR-001 subsequently closed the R01–R03 selection gate with 27 local fit cases; external endpoint implementation remains M1 work. ADR-010 subsequently completes the bounded M1 profile; M2’s fixed Debian profile is subsequently completed by ADR-012–015 with the [M2 acceptance report](../experiments/m2/M2-REPORT.md); M3 is complete for its bounded incident profile under ADR-016, including deterministic and live GLM-4.7 Debian acceptance and matched load comparison. M4 subsequently completes bounded native A2A delegation under ADR-017 with Debian and matched load evidence; M5 subsequently completes the bounded Debian operational profile under ADR-018/019.
- **Authentication was custom by default.** The bespoke issuer is superseded. SPIFFE/OIDC are now selected in ADR-001 with local library evidence; provider, bootstrap, and workload/task isolation still need M1 integration evidence.
- **Other components described mechanisms without reuse decisions.** Scheduling, events, host launch, budgets, delegation, and diagnostics now have explicit candidates and milestone gates below.
- **Existing code is evidence, not a reason to reject reuse.** Preserve version 0.1 behavior until a versioned migration. A passing persistence demo does not select a scheduler or prove a full kernel.
- **Historical and generated contracts must stay accurate.** Preserve the prior research and scope decision as dated context. Legacy OpenAPI remains the contract of implemented demo routes only.

## Decision Register

The choices below establish review direction. ADR-001 selects R01–R03 contracts with version/license/maintenance checks and local fit evidence. Other choices stay open until their own decision records and relevant fit tests exist. Sources establish candidate capabilities, not Cardea conformance.

<a id="r01--identity-and-authentication--m0m1"></a>

### R01 — Identity and Authentication · M0/M1

Prefer [SPIFFE](https://spiffe.io/docs/latest/spiffe-about/overview/) with [SPIRE](https://spiffe.io/docs/latest/spire-about/spire-concepts/) for workloads and [OpenID Connect](https://openid.net/specs/openid-connect-core-1_0.html) through an existing provider; [Keycloak](https://www.keycloak.org/) is a candidate if a provider is needed. Supersede custom credential issuance. Gate: demonstrate local bootstrap, credential rotation, audience checks, disabled principals, and distinguishable runtime/task bindings. A shared process identity does not isolate logical agents. [ADR-001](decisions/001-m0-contracts.md) selects SPIFFE/OIDC contracts, Authlib for OIDC integration, and the SPIFFE Workload API validation path for M1 evaluation. Its local SDK tests reveal direct-validator trust-domain and missing-claim gaps; do not use that method as a complete authenticator. [ADR-009](decisions/009-spire-bootstrap-fit.md) subsequently records 33 real SPIRE cases for trusted-bundle/join-token bootstrap, Workload API validation, JWT signing-key rotation, outage and binding checks. It resolves the measured direct-validator gaps through the provider path and demonstrates same-UID identity sharing. ADR-010 now implements and verifies OIDC, persisted provenance and authenticated admission in the installed service. Providers are tested in disposable state; production provider deployment and Debian task isolation are not claimed.

<a id="r02--wire-format-and-schemas--m0m1"></a>

### R02 — Wire Format and Schemas · M0/M1

[ADR-001](decisions/001-m0-contracts.md) selects HTTP/OpenAPI 3.1 and Pydantic [JSON Schema 2020-12](https://json-schema.org/draft/2020-12) over a Unix socket, reusing FastAPI/Uvicorn/HTTPX. Real socket and bounded-field tests pass. [JSON-RPC 2.0](https://www.jsonrpc.org/specification) was considered; no required batching/notifications justify a second transport stack. ADR-010 implements the v0.2 endpoints, typed HTTPX client, OpenAPI/error schemas and real socket conformance. Gate: authentication integration, bounded errors/payloads, version handling, duplicate-call behavior, and conformance tests. Operation receipt IDs remain distinct from transport correlation IDs. The old envelope is not JSON-RPC 2.0 merely because it carries JSON methods. Follow-up R13 evaluates MCP at the M3 agent/tool boundary and A2A at the M4 agent/agent boundary. It does not replace the kernel control API or establish existing protocol conformance.

<a id="r03--request-digests--m0m1"></a>

### R03 — Request Digests · M0/M1

[ADR-001](decisions/001-m0-contracts.md) selects [RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785) via `rfc8785` for the explicit v0.2 submission digest in ADR-010; retain the current encoder only for v0.1 compatibility. RFC byte vectors and a frozen receipt pass locally. Concrete compatibility gap: version 0.1 distinguishes `1` from `1.0`; switching serialization can change receipt digests. Gate: cross-language numeric/Unicode vectors, supported number range, versioned digest semantics, and migration preserving old retries. ADR-010 preserves old receipt hashes and migrations; only explicit v0.2 submission identities use JCS. Do not label embedded v0.1 operation digests JCS.

<a id="r04--storage--m1m5"></a>

### R04 — Storage · M1/M5

Retain SQLite and its transaction engine for the current single-host bounded store; application tables describe Cardea data. Reuse database backup/migration facilities and evaluate maintained tooling as schema management grows. Gate: existing crash/conditional-write tests plus tested upgrades and consistent restores. Local transactions do not cover external effects. Large external artifacts remain deferred rather than motivating a new storage engine. [SQLite transaction reference](https://www.sqlite.org/atomiccommit.html). [ADR-008](decisions/008-dbos-storage-integration.md) verifies native shared-store DBOS transactions against Cardea admission and publication, preserving existing v0.1 data in a bounded experiment. ADR-010 adds coordinated Alembic/DBOS version checks and whole-store snapshot/restore. ADR-015 adds terminal host-payload retirement and transactional host budgets. ADR-019 closes M5 operational storage with transactional upgrade/refusal checks, artifact-verified staging, append-only recovery epochs, sealed historical scopes and lifetime history retention plus explicit generated-backup pruning.

<a id="r05--scheduler-and-recovery--m1"></a>

### R05 — Scheduler and Recovery · M1

Compare maintained scheduling libraries such as [APScheduler](https://apscheduler.readthedocs.io/en/master/userguide.html) and durable workflow infrastructure such as [Temporal](https://docs.temporal.io/) with composition over the existing store. These are different levels of machinery, not drop-in equivalents. Gate: task leases, checkpoint/wait atomicity, cancellation, uncertain effects, and measured single-host operating cost. Select an engine or document precise unmet requirements before building a custom dispatch loop. Documentation on a development branch is not a pinned release.

The [ADR-002 comparison](decisions/002-workflow-engine-comparison.md) screens 17 projects, including newer alternatives, and shortlists DBOS, Temporal, Hatchet, and Absurd for common failure tests. Release/license evidence is recorded. [ADR-003](decisions/003-dbos-fit.md) reports 38 DBOS observation cases across SQLite and PostgreSQL, including unsafe raw behaviors and partial acceptance coverage. [ADR-004](decisions/004-temporal-fit.md) adds Temporal development-server observations, including native thread interruption and explicit policy differences. [ADR-005](decisions/005-absurd-fit.md) adds Absurd PostgreSQL/synchronous-SDK observations, including queue-scoped events, claim expiry and explicit retry semantics. [ADR-006](decisions/006-hatchet-fit.md) adds Hatchet embedded-sidecar observations. [ADR-007](decisions/007-runtime-integration-direction.md) selects DBOS for the next bounded integration experiment, with a commit-boundary and ownership gate before production adoption. [ADR-008](decisions/008-dbos-storage-integration.md) completes 28 actual-store integration cases and selects shared-file native transactions as the next integration direction. Distinct-executor recovery is explicitly initiated and broker-fenced, not automatic failover. ADR-010 adopts DBOS for the bounded authenticated service with tested native transactions, exclusive process ownership, versioned migration, restore and Debian userspace conformance. R05 is complete for M1; distributed takeover and external-effect runtimes require separate evidence. The 13 candidates outside the shortlist remain unrun.

<a id="r06--authorization--m2"></a>

### R06 — Authorization · M2

Evaluate [OPA](https://www.openpolicyagent.org/docs) and [OpenFGA](https://openfga.dev/docs/modeling/getting-started) for policy and relationship decisions. Gate: resource/operation scope, disclosure, and the invariant that task authority never exceeds its accountable supervisor’s current permissions/delegation authority; enforce transitive ancestor ceilings, revocation at dispatch, attenuation, and policy-state freshness. Test overbroad grants, broader approvers, disablement, concurrent revocation, retry/resume, and privileged-service bypass. Policy decisions must integrate with transactional admission; they do not themselves reserve budget or stop workers. ADR-010 additionally evaluates Casbin 1.43.0 and selects its maintained Python implementation for M1 exact ACLs in the current SQLite transaction. Live supervisor/delegation/ancestor/platform ceilings and expiry/revocation are tested. OPA/OpenFGA remain candidates if future richer relationships exceed these exact ACLs. [ADR-014](decisions/014-object-scoped-host-inputs.md) extends the same Casbin equality matcher to exact immutable-object read grants, with a recorded tuple-substitution fit and live source ceilings. ADR-015 verifies both installed operations, live disablement and policy freshness. R06 is complete for M2’s exact immutable-input/task-owned-output scopes; broad ambient paths are outside the MVP and M4 adds only allocated inline host children under ADR-017.

<a id="r07--host-execution--m2"></a>

### R07 — Host Execution · M2

Evaluate Linux/systemd confinement with maintained launchers such as [bubblewrap](https://github.com/containers/bubblewrap); keep Cardea's adapter as configuration and boundary integration where possible. A launcher alone does not supply every promised limit. Gate: Debian path/symlink/network denial, credential isolation, descendant termination, resource ceilings, and unsupported-profile rejection. Select a tested composition before custom launcher code. [systemd execution controls](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml). [ADR-012](decisions/012-m2-host-confinement.md) selects systemd + bubblewrap after 11 real Debian VM probes; the systemd-only candidate failed scratch-capacity enforcement. ADR-013 implements the first fixed sudo helper, bounded staging, live-authority dispatch and conservative crash reconciliation for `host.sha256`; ADR-014/015 complete M2’s fixed two-operation registry, input scopes, transactional accounting and recoverable staging retirement. The final 20 authenticated Debian scenarios and 11 hostile production-profile probes pass. New operations or execution profiles require their own acceptance.

<a id="r08--events-and-ingestion--m1m3"></a>

### R08 — Events and Ingestion · M1/M3

Evaluate [CloudEvents](https://cloudevents.io/) for exchanged event envelopes and maintained input watchers for detection. Envelope interoperability does not implement a durable journal or reliable wakeup. Reuse the selected scheduler/store for those responsibilities. Gate: stable source IDs, authorized payloads, replay/deduplication, wait races, and rescanning changes made during downtime. ADR-010 composes existing internal events with native DBOS `send_in_transaction` and durable timers, closing M1 wakeup/restart races. ADR-016 implements bounded directory rescan and idempotent authenticated additive input, using native DBOS messages and the existing journal. Early/late input and downtime rescans pass. CloudEvents is deferred: this interface sends a task input command, not an exchanged external event envelope. A future external producer requires its own envelope fit.

<a id="r09--runtime-and-budgets--m2m3"></a>

### R09 — Runtime and Budgets · M2/M3

Reuse the selected provider's maintained SDK and evaluate existing runtime orchestration before adding loops. Use selected host resource controls for CPU/memory/process limits. Compare accounting libraries with the required transactional reservation, reconciliation, and parent ceiling semantics; document any remaining domain-specific ledger. Gate: measured usage, exhausted-budget denial, concurrent reservation, unknown provider usage, and crash recovery. Provider/runtime selection does not authorize bypassing the Cardea broker; no new scheduler, HTTP client, or token estimator by default. ADR-012 demonstrates enforced host CPU/memory/process/deadline limits and cancellation. ADR-013 reserves and conservatively charges one fixed host-operation allocation through the durable receipt; ADR-015 adds SQLite-serialized per-supervisor/namespace allocations, concurrent reservations, measured settlement/release, unknown full charges and migration/cleanup recovery. The host/admission R09 gate is complete for M2. ADR-016 implements model reservations/reported settlement and conservative unknown charges through the native OpenRouter SDK. Concurrent/exhausted/overage/uncertain tests pass; live GLM-4.7 reported usage is recorded in the M3 acceptance report. ADR-017 adds bounded shared child allocations within live ancestor ceilings; delegated model work remains unsupported.

<a id="r10--application-verification--m3"></a>

### R10 — Application Verification · M3

Retain application-specific acceptance rules, using existing parsers, schema validators, and test tooling. A schema validator can check document shape; matching the incident index to inputs still needs explicit application assertions. Gate: corrupted/stale/missing output and false-success rejection. Scope bespoke code to demonstrated domain requirements, not a new general verification framework. ADR-016 composes Pydantic and JSON Schema with exact incident-content/index checks and anchored summary references; native SDK, portable service and deterministic-provider Debian acceptance pass. Live GLM-4.7 acceptance also passes for the bounded incident profile.

<a id="r11--delegation-and-messaging--m4"></a>

### R11 — Delegation and Messaging · M4

First reuse the chosen workflow/event infrastructure and authorization engine for child tasks, delivery, and permissions. Gate: parent cancellation, duplicate delivery, attenuated authority, shared budget ceilings, and child-failure recovery. Decide whether these capabilities suffice before designing a new mailbox, queue, or delegation credential. No general multi-agent guarantee is implied by existing parent fields. ADR-017 closes this gate for the installed one-level host profile: existing DBOS queues/wakeups, immutable SQLite child/message associations and live Casbin checks suffice. Native A2A supplies the peer interaction; no new mailbox or scheduler is justified. Portable races, Debian restart/cancel/revocation/child-failure scenarios and a separate matched load reference/comparison pass.

<a id="r12--clients-packaging-and-diagnostics--m1m5"></a>

### R12 — Clients, Packaging, and Diagnostics · M1/M5

Reuse maintained CLI/client tooling and the selected auth/wire integration; retain React only for a useful later supervisor. Reuse Debian/systemd deployment and SQLite backup mechanisms. Evaluate [OpenTelemetry](https://opentelemetry.io/docs/what-is-opentelemetry/) for diagnostics; telemetry does not replace the transactional journal. Gate: no hidden authority path, reproducible install/reboot/restore, dependency/license inventory, and redaction. ADR-010 implements the M1 socket client, provider login, diagnostics, version inventory and shared-store recovery. ADR-018 adds the initial Debian package/systemd and installed reboot scenario. ADR-019 completes installed upgrades, conservative restore, redacted support diagnostics and retention with [M5 acceptance](../experiments/m5/M5-REPORT.md); general telemetry and additional host adapters are not implied. Other hosts require actual adapter conformance, not nominal portability.

<a id="r13--mcp-and-a2a-interoperability--m3m4"></a>

### R13 — MCP and A2A Interoperability · M3–M4

Updated 2026-09-24. **Status: MCP v2 selected and implemented in M3 under [ADR-016](decisions/016-m3-openrouter-and-native-mcp.md); portable and deterministic-provider Debian acceptance pass, live GLM-4.7 acceptance and matched load comparison also pass. A2A 1.0/SDK 1.1.5 is implemented and accepted for the bounded M4 profile under ADR-017, including native async handlers, Debian recovery/denial and matched load.** The immediate kernel-front-door experiment was discarded. MCP belongs to the agent/tool interaction in M3; A2A belongs to agent/agent communication in M4. R13 does not gate M2 or replace the authenticated kernel API.

- **MCP candidates:** the [specification](https://modelcontextprotocol.io/specification/2026-07-28) and [official Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT). Evaluate native client discovery, tool invocation, resource retrieval, authentication and transports inside the trusted broker. Assess optional Tasks support independently. A server exposing Cardea tools needs an actual agent consumer and a bounded operation scope before selection.
- **A2A candidates:** the [1.0 specification](https://a2a-protocol.org/v1.0.0/specification/) and [official Python SDK](https://github.com/a2aproject/a2a-python) (Apache-2.0). Evaluate native Agent Cards, messages, tasks, artifacts, updates and cancellation between two controlled agent runtimes before admitting external peers.
- **Selection evidence:** pin exact releases and supported bindings at implementation time; check maintenance, licenses and security; execute real protocol clients/servers against each milestone's actual interaction. Prefer SDK types and extension points over bespoke wire contracts. Record precise gaps for authorization, durable request association, reconciliation and verification. A transport request ID is not automatically a durable operation key.
- **State and compatibility:** the kernel owns local admission, task authority, receipts and recovery. The MCP server or peer agent owns its remote operation/task state; persist the association and reconciliation evidence, without mirroring it as a second local execution engine. Protocol types apply at their own boundary. Any change to existing persisted records or public kernel contracts requires a separate justified versioned migration.

Every call, disclosure and delegation stays within explicit task grants, the supervisor's live permissions/delegation authority, all ancestor ceilings and platform policy. Recheck at admission, dispatch, retry and resume. Bind peer identity to authorized delegation; cards, context IDs, approval and broad broker credentials do not confer authority. Reserve child work within the parent budget. Reject remote execution profiles that cannot meet the required revocation/cancellation guarantees, and explicitly reconcile already-dispatched uncertain effects.

Gate: execute the [protocol acceptance matrix](../validation-plan.md#protocol-integration-acceptance--mcp-and-a2a) and record milestone-specific fit evidence before adoption. M3 requires a real agent consuming an MCP tool and independently verified output; M4 requires two agents delegating bounded work, including recovery, denial and child-failure handling. M2's Debian enforcement gate remains a prerequisite for arbitrary worker execution. Neither a successful tool response nor a peer's completed status proves the local objective.

### R14 — Company Product and Application Expansion · M6–M10

Added 2026-09-25. **Status: direction selected; candidate shortlist and product gap review recorded; implementation fit decisions and customer validation open.** [ADR-020](decisions/020-company-product-direction.md) compares existing agent runtimes/platforms, a protocol gateway, workflow automation and identity reuse using current primary sources and release/license signals. No new dependency or bespoke mechanism is selected by this review. [Product direction](../product.md), [gaps](product/gaps.md) and [validation](product/validation.md) link the company application and cross-department requirements to M6–M10. Before implementing application installation, connectors, team identity, handoff, recurring work or retention changes, record pinned fit tests, authority/state ownership and the smallest remaining custom gap. Technical acceptance and observed user value are separate completion gates.

## Milestone Review

R14's delivery sequence is revised by [ADR-021](decisions/021-company-process-data-integration.md): cross-team operation starts with M7 rather than waiting for M9. Process representation/integration and related company data are now explicit first-delivery requirements. BPMN/Flowable, Open Workflow Specification, CloudEvents and CDC/API approaches are shortlisted with primary sources; runtime/artifact selection, license/security fit and fault tests remain open. This does not select a second execution engine or change existing kernel contracts.

| Milestone | Revised status | Required reuse evidence before completion |
|---|---|---|
| M0 | Contract implementation and R01–R03 selection gate complete | ADR-001: selected contracts, migration plan, 27 local fit cases; authenticated endpoints and provider/migration integration remain M1 |
| M1 | Complete, bounded headless profile | ADR-010: R01–R05, bounded R06, M1 R08/R12; real providers, authenticated restart and Debian userspace evidence |
| M2 | Complete, fixed Debian execution profile | ADR-012–015: R06/R07 and host/admission R09; 20 Debian scenarios, 11 confinement probes, transactional accounting and durable cleanup |
| M3 | Complete, bounded incident profile | ADR-016 closes R08–R10 and bounded native MCP runtime R13; portable, deterministic/live GLM-4.7 Debian and matched load evidence pass |
| M4 | Complete, bounded delegation profile | ADR-017: R11 and bounded native A2A R13; SDK persistence fit, authority/accounting, recovery, child failure and matched load |
| M5 | Complete, supported Debian profile | ADR-018/019: R04/R12 packaging, installed upgrade/refusal, reboot recovery, artifact-checked fenced restore, private diagnostics and retention |
| M6 | Active discovery; customer/job evidence open | R14 / ADR-020 shortlist; observed job contract, baseline and justified first application required |
| M7–M10 | Planned product delivery, repeat operation, cross-team reuse and private release | R14 plus affected R01–R13 decisions; pinned fit evidence and milestone-specific product outcomes remain required |

Later milestone decisions may remain open until their implementation begins. An open decision blocks the relevant new mechanism, not unrelated documentation or regression maintenance.

## Document Coverage

Every repository-owned Markdown file existing at review start was examined. Related paths below share a disposition; the component rows are individually linked to R01–R12 in their files.

| Documents | Disposition |
|---|---|
| `AGENTS.md`, `.agents/AGENT.md`, `coding-standard.md`, `.agents/README.md`, `.agents/coding-standard.md` | Make reuse mandatory, correct the obsolete first-build instruction, retain canonical pointers |
| `.agents/subagents/kernel-api-client.md`, `host-runtime-adapters.md`, `kernel-state-verification.md`, `human-supervisor.md`, `authority-operations-journal.md`, `runtime-qa-security.md` | Require relevant decision evidence in each role; no delegation is triggered by these files alone |
| `README.md`, `design.md`, `mvp.md`, `roadmap.md`, `validation-plan.md` | Separate requirements from custom implementation; expose reopened M0 review and per-milestone gates |
| `apps/api/README.md`, `apps/web/README.md`, `packages/README.md` | Preserve legacy behavior and current tooling; link standards-first extension requirements |
| `docs/kernel-contract.md`, `runtime-decisions.md`, `persistence-demo.md`, `standards-first.md` | Preserve implemented semantics; mark external choices provisional and link decisions |
| `docs/scope-decision-2026-09-21.md`, `problem-validation.md` | Preserve dated/historical content; add a current-policy pointer without refreshing historical market claims |
| All 17 files in `docs/components/` (index plus 16 component/detail documents) | Review every design/status and add a specific reuse decision link; no component is exempt |

This report and the decision-record template are new review outputs. Local link/anchor, coverage, and whitespace checks validate the documentation changes. The initial review itself changed no runtime code. Subsequent ADR-001–010 decisions and evidence record the implementations and milestone results linked above.


### M1 load-test tooling follow-up (2026-09-24)

[ADR-011](decisions/011-m1-load-testing.md) selects maintained Vegeta over a new load scheduler, composes HTTPX/psutil/RFC 8785 with the existing real-provider fixtures, and bounds the custom scope to orchestration, durable assertions and baseline/report handling. The independently installable [package](../packages/cardea-load/README.md), [measured reference](../packages/cardea-load/reports/2026-09-24-m1/baseline.json) and [executed report](../packages/cardea-load/reports/2026-09-24-m1/REPORT.md) are complete. All recorded correctness checks passed; native Debian capacity and longer/further fault profiles remain unverified. Existing M1 source and prior evidence are unchanged.
