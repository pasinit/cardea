# Agent OS Validation Plan

Updated 2026-09-25. This document defines infrastructure acceptance. The separate [company product validation protocol](docs/product/validation.md) adds observed workflow baselines, useful-outcome measures, repeat adoption and cross-team reuse for M6–M10. Neither technical tests nor historical enterprise research substitute for those product gates.

## Questions to Prove

1. Can an agent discover and compose operations without a human translating UI state?
2. Does durable execution survive process crashes and host service restarts?
3. Do real host boundaries enforce the authority and resources the kernel promises?
4. Can another application reuse these primitives without adding its business nouns to the kernel?
5. Does the runtime reduce application-specific recovery and permissions code enough to justify adopting it?

## Reuse Decision Evidence

Before each mechanism is implemented, use the [review register](docs/standards-review-2026-09-22.md) and [decision template](docs/decision-record-template.md) to compare existing standards/projects. Record evaluated versions, licenses, maintenance/security status, integration costs, and fit results. Exercise relevant failure cases in reused components as well as custom adapters. An unavailable or unrun result remains open; it does not prove the candidate inadequate.

A milestone needs both its reuse decisions and its behavioral acceptance evidence. Record any bounded custom exception and its revisit trigger. Version 0.1 tests remain valid implementation evidence. [ADR-001](docs/decisions/001-m0-contracts.md) completes M0’s R01–R03 selection gate with 27 local fit cases; [ADR-010](docs/decisions/010-m1-authenticated-kernel.md) records the executed authenticated M1 acceptance, migration and real-provider checks. Subsequent M2–M5 sections record bounded real-agent, host-denial, delegation and operational acceptance. Their checks remain required for relevant future extensions; product adoption has its own evidence gates.

## Technical Evidence

Use the [MVP scenario](mvp.md) as the primary integration test. Run deterministic fault injection separately from real-model usefulness tests.

| Test | Required observation |
|---|---|
| Headless execution | Complete with the web UI stopped; no manual tool-driving after submission. |
| Agent choice | A real model selects typed operations; record actual calls, not just a scripted sequence. |
| Identity | Caller-supplied actor names cannot impersonate another identity. |
| Authority | Unauthorized discovery, reads, writes, execution, and task inspection fail before effects. |
| Supervisor ceiling | A read-only supervisor cannot grant a task writes; a broader approver/service credential cannot bypass the bound. Added supervisor rights do not expand explicit task grants. |
| Revocation | Revoking/expiring supervisor or ancestor permissions, or disabling the supervisor, denies affected queued/retried/resumed work and descendants; in-flight access stops and effects are cancelled/reconciled. |
| Authority race and restart | Order revocation against dispatch, invalidate stale policy decisions, and retain supervisor/provenance bindings after restart; missing provenance fails closed. |
| Crash recovery | Inject failures before dispatch, after effect/before receipt, and after commit. Preserve acknowledged state and classify uncertain outcomes. |
| Event races | Events before subscription, during downtime, and on replay cause no lost wakeup or duplicate committed work. |
| Concurrency | Conditional writes detect stale versions; competing claims cannot run the same task lease concurrently. |
| Isolation | Debian workers cannot escape paths through symlinks, reach secrets, or use ungranted network access. |
| Limits | Exhausted budgets stop admission; cancellation/timeout terminates worker descendants. |
| Verification | Missing, stale, or invalid output cannot become success through a hard-coded boolean. |
| Host portability | Missing enforcement facilities reject the execution profile; macOS unit tests do not substitute for Debian checks. |

Keep a result manifest with decision IDs, selected dependency versions/licenses, custom exceptions, commit, host/kernel details, execution profile, model adapter/version, test outcome, artifact hashes, fault injection point, denied operations, usage, latency, and human interventions. Redact credentials and sensitive input. Model-generated explanations are evidence to inspect, not proof of correctness.

## Protocol Integration Acceptance — MCP and A2A

**Status: M3 native MCP implementation passes portable and deterministic-provider Debian acceptance under ADR-016. Matched load and live GLM-4.7 restart/new-input acceptance also pass; M4 native async A2A delegation passes SDK fit, portable, real Debian and matched load acceptance under ADR-017.** [R13](docs/standards-review-2026-09-22.md#r13--mcp-and-a2a-interoperability--m3m4) and the [roadmap](roadmap.md#protocol-boundaries--mcp-in-m3-a2a-in-m4) place MCP at the M3 agent/tool boundary and A2A at the M4 agent/agent boundary. The discarded kernel-front-door prototype is not acceptance evidence. An SDK's own tests do not establish Cardea conformance.

| Test | Required observation |
|---|---|
| Native adoption and SDK fit | Pin protocol/SDK versions and transports; record licenses/maintenance. Exercise native SDK types and transports at the actual agent/tool or agent/agent boundary, with kernel authority and execution accounting. Analyze task states, IDs, artifacts, errors, cancellation and retries; require evidence for each custom conversion. Negotiate optional features and reject unsupported ones without silently changing semantics. |
| Redundancy and migration | Use native models at each protocol boundary; do not treat protocol adoption as a reason to replace the kernel API. Before changing persisted receipts, identities or public contracts, test an explicit versioned migration/compatibility plan preserving acknowledged state, retry identity and authority. One authoritative execution state remains; no duplicate store is introduced to mirror SDK state. |
| MCP server exposure · conditional M3 scope | If an actual agent consumer requires Cardea tools, expose only selected authorized tools/resources through a maintained SDK. Test disclosure, invocation and revocation over the chosen transport. This is not a generic kernel endpoint or M2 requirement. |
| MCP client/runtime interoperability · M3 | A real agent selects a permitted tool; the trusted broker calls an approved MCP server under bounded credentials and grants, validates results, enforces disclosure/budgets and independently checks the output. Test long-running work through verified Tasks support or an explicit limitation. |
| A2A interoperability · M4 | Two controlled agent runtimes discover supported Agent Cards, delegate a bounded objective, exchange messages, follow task/artifact updates and request cancellation. Persist the local child/remote task association; authenticate and authorize inbound delegation before admission. State mappings must preserve waits, terminality and verification semantics; no card or context ID confers authority. |
| Live authority on both protocols | Test cross-supervisor access, overbroad grants, forged provenance, broader adapter credentials, supervisor/ancestor revocation and disablement at admission, dispatch, retry and resume. No unauthorized discovery, artifact disclosure or new effect may occur. |
| Recovery and duplicate delivery | Disconnect or crash after dispatch and before response/receipt persistence; recover the same durable task and preserve operation digests. Identical retries do not duplicate committed effects; changed arguments conflict. Unknown external outcomes stay explicit and are reconciled before retry. |
| Cancellation and delegation | Cancellation remains observable after restart and propagates to bounded child/remote work. Preserve supervisor/ancestor ceilings and shared budgets across delegation. Reject peers/profiles where required enforcement cannot be demonstrated; a transport cancellation acknowledgment alone does not prove effects stopped. |
| Completion evidence | A successful tool response or remote completed status cannot bypass independent verification; missing, stale, corrupted or mismatched artifacts fail the task's acceptance check. |
| Load and fault parity | Extend the separate load package with M3 agent/tool and M4 delegation profiles, reusing applicable M1 fault scenarios. Measure admission and completion separately with matched tasks, rates and resource limits; record protocol/transport/authentication overhead and any justified integration overhead. Preserve the existing reference and create new comparison profiles where contracts differ; do not compare incompatible runs as regressions. |

Keep fit results, protocol traces with credentials redacted, source/version manifests and remaining gaps alongside the decision record. Prototype success advances the fit review only; it does not complete M2 host confinement, M3 real-agent execution or M4 general coordination.

## Reuse and User Validation

After the first scenario works, build a second bounded application such as repository inspection using the same kernel contracts. Measure which recovery, identity, and execution code the application can omit. Compare with a small direct script/runtime baseline on the same tasks, including failures and setup effort; do not assert an OS advantage from a clean happy-path demo.

Ask agent application developers to run both tasks, inspect failures, and explain what they still have to implement themselves. Seek repeated usage and willingness to move a task onto the runtime. Broad interest in AI or enterprise governance is not evidence for this product.

## Continue, Narrow, or Revisit

Continue when the Debian acceptance suite passes, independent applications reuse the primitives, and external users value the execution guarantees. Narrow when a smaller runtime library satisfies the same needs. Revisit the design if useful tasks require broad ambient authority, every workflow needs kernel changes, or recovery works only through human repair.

Existing escalation tests establish a legacy demo baseline only. The historical research in `problem-validation.md` concerns a different enterprise thesis and does not validate the agent OS.

## M1 load and reliability reference

The separate [cardea-load package](packages/cardea-load/README.md), selected in [ADR-011](docs/decisions/011-m1-load-testing.md), measures the authenticated M1 API against an immutable repeated reference. It uses Vegeta for pacing/HTTP metrics and the real M1 provider fixtures for durable-state and authority checks. See the [executed campaign report](packages/cardea-load/reports/2026-09-24-m1/REPORT.md) and machine-readable evidence beside it. Its emulated Debian throughput is a local comparison reference, not native host capacity or M2 confinement evidence. Performance warnings are provisional; durable correctness failures are unconditional failures.


## Bounded installed host operation — M2 increment

[ADR-013](docs/decisions/013-installed-host-operations.md) adds real Debian execution for `host.sha256`. Acceptance combines portable denial/recovery tests, the original M1 real-provider service regression suite, seven Debian SPIRE/daemon/sudo-helper scenarios and eleven hostile production-profile probes. See the [integration report](experiments/m2/INTEGRATION-REPORT.md) for exact results, hashes and limits.

Check admission and dispatch against current allow/delegate/platform policy and principal status; deny expiry, extra command/path fields, unknown operations and child host tasks. Verify that supervisor revocation between durable intent and launch prevents start, cancellation confirms stop before terminal acknowledgment, and daemon kills before/after launch or before receipt commit retain unknown outcomes without replay. A worker's successful exit with a false digest must fail independent verification.

The full fixed reservation is charged conservatively; broader accounting, multiple operations, task-shared budgets and retention are not established by this increment. Production packaging/upgrades and full-OS reboot tests were deferred to M5. ADR-014/015 subsequently complete the bounded M2 profile; ADR-018/019 close the packaged operational gates. This earlier increment’s evidence remains historical.

## M2 exact-version input acceptance

[ADR-014](docs/decisions/014-object-scoped-host-inputs.md) extends `host.sha256` to stored bytes named by an immutable object reference. Require exact namespace/object/version/digest permissions in addition to source-namespace ceilings, with supervisor allow/delegate and platform allow checked at every execution boundary. Test wrong-object/version/digest substitutions, omitted/revoked/expired policy, newer stored versions, missing/corrupt/oversized bytes, retained source provenance and no relaunch after interrupted intent. Preserve inline request digests and all prior service acceptance.

Real Debian acceptance creates the source through the authenticated kernel, grants its exact version, runs the confined worker, and revokes each of the supervisor allow, supervisor delegate and platform allow permissions before launch and before publication. A daemon kill after object dispatch must produce `outcome_unknown`. This uses one Debian host for the stack and many jobs; it adds no per-job VM. Keep ADR-013 evidence unchanged and record this increment separately. Executed results are in the [object-input report](experiments/m2/OBJECT-INPUT-REPORT.md).

## M2 completion acceptance

[ADR-015](docs/decisions/015-m2-accounting-retention-and-acceptance.md) closes the fixed-registry MVP profile with two installed operations, live scoped authority, transactional host allocations and measured settlement, conservative unknown charges and recoverable worker-data retirement. The [M2 report](experiments/m2/M2-REPORT.md) maps every exit requirement to portable and real Debian acceptance, preserves earlier evidence, and records exact source/package hashes. Concurrent reservation, exhaustion, allocation replacement, migration rollback, lost cleanup acknowledgments and no-relaunch tombstones are required checks. M3 and M4 subsequently add bounded provider accounting and shared child budgets. M5 subsequently supplies installed full-machine reboot and operational recovery evidence.

## M4 completion acceptance

[ADR-017](docs/decisions/017-m4-native-a2a-delegation.md) closes R11 and the bounded native A2A portion of R13. The [M4 report](experiments/m4/M4-REPORT.md) records SDK task-store persistence/owner fit, native discovery/message/task/history/list/cancel interoperability, transactional allocation races, live ancestor ceilings, deadline/cancellation propagation, migration and independent artifact checks. Real Debian scenarios exercise lost replies, restart before publication, revocation, running-worker cancellation, allocation exhaustion and child verification failure with an explicitly partial parent objective.

Native A2A handlers must remain async and allow other requests to progress while synchronous authentication or transactional admission waits. A regression test holds each boundary open while another authenticated request completes. The initial concurrent load failure is preserved; the corrected run uses the same client timeouts and workload. The independent [M4 load report](packages/cardea-load/reports/2026-09-24-m4/REPORT.md) has two fresh-store reference repetitions and one comparison, with receipts, output versions, integrity and native round-trip timing. Keep this profile separate from M1/M3 references and report warnings without relabelling them as passed performance gates.

## M5 packaging increment acceptance

[ADR-018](docs/decisions/018-m5-debian-package.md) reuses debhelper/dh-virtualenv and systemd. The [packaging report](experiments/m5/PACKAGING-REPORT.md) records a fresh inactive installation, explicit identity/policy enrollment, worker denial, isolated CLI imports, native async A2A delegation, whole-machine reboot, unchanged results/receipt, persistent supervisor revocation and new permitted host work. Installed source hashes, package digest, dependency/license inventory and exact host versions are retained. The initial fixture readiness failure is preserved and the corrected full scenario passes.

## M5 completion acceptance

[ADR-019](docs/decisions/019-m5-operational-recovery.md) closes the remaining operational gates. The [M5 report](experiments/m5/M5-REPORT.md) records actual package revision 1→2 migration, old-package refusal on the new schema, unchanged receipts, interrupted host intent and durable wait across a whole-machine reboot, post-snapshot spending/revocation followed by fenced restore, fresh-scope A2A work, installed corruption/restore fault injection, diagnostics and explicit retention. Portable tests separately cover clock/lease refusal, artifact references, authority/budget fences, immutable provenance, pending recovery markers and secret redaction. Historical raw-snapshot replay evidence remains historical; M5 deliberately supersedes it. No arbitrary future runtime upgrade or other Unix host is certified.
