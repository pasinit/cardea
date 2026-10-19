# Cardea

An agent-first operating layer hosted on Unix. Debian/Linux is the first deployment target.

Cardea's userspace kernel will give agents native primitives for identity, task execution, capability discovery, durable state, events, bounded resources, and recovery. Agents are the primary clients. Humans set objectives, grant authority, inspect execution, and resolve exceptions through an optional supervisory interface.

The host OS continues to own hardware, virtual memory, native processes, networking, and low-level isolation. Cardea owns the agent execution contract above it. Other Unix hosts are a portability goal, not supported targets yet.

## Status

**M0–M5 are complete for their documented bounded profiles. The next phase is company product validation:** a connected process across departments and company systems, then reuse for additional processes. Read the [product brief](product.md), [gap assessment](docs/product/gaps.md) and [M6–M10 roadmap](roadmap.md#m6--validate-the-first-company-job). Company adoption and general-purpose application support remain unverified.

The project was rescoped on 2026-09-21. For the M1 authenticated kernel, see [quickstart](docs/m1-quickstart.md) and [acceptance evidence](docs/decisions/010-m1-authenticated-kernel.md).

`cardea.kernel` provides version 0.1 records and lifecycle checks plus a private SQLite store for tasks, checkpoints, operation receipts, versioned object bytes, and atomic event history. The trusted deterministic demo resumes in a new process and checks its actual output. Portable tests cover crash windows, competing claims, worker fencing, and uncertain effects. The separate `cardea.service` adds SPIFFE/OIDC authentication, persisted supervisor provenance, current permission/delegation ceilings, a Unix-socket API/client, DBOS dispatch, independent JSON verification and shared-store backup/restore. M2 adds fixed SHA-256 and byte-copy operations through an unprivileged service, narrow privileged helper and systemd/bubblewrap workers, with scoped inputs, transactional host budgets and recoverable staging cleanup. See [setup and limits](experiments/m2/INTEGRATION.md). M3 adds a bounded OpenRouter/native MCP incident task with verified output versions and durable input waiting; [usage and acceptance limits](experiments/m3/README.md). M4 adds bounded native A2A delegation with async handlers, shared allocations and verified child results; see [usage](experiments/m4/README.md) and [acceptance](experiments/m4/M4-REPORT.md). General tool/plugin execution remains later work.

The pre-rescope customer-escalation demo (FastAPI + React, simulated state) was removed from `main` on 2026-10-10 and is preserved on the [`archive/legacy-escalation-demo`](https://github.com/pasinit/cardea/tree/archive/legacy-escalation-demo) branch.

Implementation status and design selection are separate: M0’s contract code and R01–R03 selection gate are complete, with 27 local fit tests in [ADR-001](docs/decisions/001-m0-contracts.md). [ADR-010](docs/decisions/010-m1-authenticated-kernel.md) completes the bounded M1 integration and acceptance gate; M2 is complete for the fixed Debian profile in [ADR-015](docs/decisions/015-m2-accounting-retention-and-acceptance.md) and the [acceptance report](experiments/m2/M2-REPORT.md); M3 is complete for its bounded incident profile under [ADR-016](docs/decisions/016-m3-openrouter-and-native-mcp.md); M4 is complete for bounded delegation under [ADR-017](docs/decisions/017-m4-native-a2a-delegation.md); M5 is complete for the supported Debian profile under ADR-018/019; see the [operational acceptance report](experiments/m5/M5-REPORT.md) and [installation/operations guide](packaging/README.md). See the [review and decision register](docs/standards-review-2026-09-22.md).

[ADR-014](docs/decisions/014-object-scoped-host-inputs.md) adds stored-object input to the SHA-256 worker: a task names one immutable version and requires current source-namespace and exact-version read/delegation grants. The kernel stages only that version's bounded bytes on the persistent Debian host. See [usage](experiments/m2/INTEGRATION.md#hash-an-existing-stored-object).

## New Direction

- [Open-source-first policy](docs/standards-first.md): required decision process for all components.
- [Component designs](docs/components/README.md): concise responsibilities, status, and reuse decisions.
- [Workflow engine comparison](docs/decisions/002-workflow-engine-comparison.md): 17 candidates, current release/license evidence, and the planned failure tests.
- [DBOS fit results](docs/decisions/003-dbos-fit.md): isolated SQLite/PostgreSQL recovery tests, measured gaps, and remaining acceptance work.
- [Temporal fit results](docs/decisions/004-temporal-fit.md): development-server recovery, cancellation and ID-policy comparisons with DBOS.
- [Absurd fit results](docs/decisions/005-absurd-fit.md): PostgreSQL recovery, event scope, child waits and expired-claim observations.
- [Hatchet fit results](docs/decisions/006-hatchet-fit.md): native sidecar recovery, event lookback, idempotency and effect-boundary observations.
- [Runtime integration direction](docs/decisions/007-runtime-integration-direction.md): DBOS selected for bounded integration.
- [DBOS/Cardea integration results](docs/decisions/008-dbos-storage-integration.md): 28 local cases verify shared-store commits and broker recovery; historical integration evidence preceding ADR-010.
- [SPIRE bootstrap results](docs/decisions/009-spire-bootstrap-fit.md): 33 real provider cases verify validation, rotation and binding checks; historical provider evidence preceding ADR-010.
- [Design](design.md): architecture, host boundary, and product scope.
- [Kernel contract](docs/kernel-contract.md): implemented records and proposed runtime semantics.
- [Runtime decisions](docs/runtime-decisions.md): Debian target, selected provider integration, and local session contract.
- [Persistence demo](docs/persistence-demo.md): runnable commands and limits of the current implementation.
- [MVP](mvp.md): the first demonstrable agent-native task.
- [Roadmap](roadmap.md): ordered implementation milestones and exit criteria.
- [Validation plan](validation-plan.md): recovery, isolation, autonomy, and usefulness checks.
- [Coding standard](coding-standard.md): implementation boundaries.
- [Scope decision](docs/scope-decision-2026-09-21.md): what changed and what is retained.
- [Historical problem research](problem-validation.md): prior enterprise thesis, not validation of this scope.

## Kernel Development Commands

- `make m1-demo`: run the authenticated submit/crash/restart/resume demonstration.
- `make m1-test`: run full daemon acceptance with real SPIRE and Keycloak ([setup](experiments/m1/README.md)).
- `make kernel-openapi`: export the authenticated v0.2 API to `packages/kernel-openapi.json`.

- `make kernel-test`: run the portable record, lifecycle, and retry contract tests.
- `make standards-test`: run the identity, real Unix-socket transport, and digest compatibility fit checks.
- `make spire-fit`: run real SPIRE bootstrap and binding tests after the pinned [native build](experiments/spire/README.md).
- `make dbos-fit`: run the isolated DBOS observation suite; PostgreSQL cases require native PostgreSQL binaries.
- `make dbos-integration-fit`: run the actual Cardea-store integration and crash suite ([scope](experiments/dbos-integration/README.md)).
- `make temporal-fit`: run the isolated Temporal suite; requires verified CLI 1.9.1 (see [setup](experiments/temporal/README.md)).
- `make absurd-fit`: run the isolated Absurd suite using a disposable native PostgreSQL cluster.
- `make hatchet-fit`: run the isolated Hatchet suite; requires the pinned native sidecar (see [setup](experiments/hatchet/README.md)).
- `make kernel-schema`: print the version 0.1 JSON Schema catalog to stdout.
- `make kernel-demo`: checkpoint in one process and resume/verify in another using a temporary store.

`make test` runs every portable suite (API, kernel, load package, A2A SDK fit); `make lint` and `make typecheck` match CI.

## Try it

```sh
make install
make try
```

Runs the real kernel daemon through submit, authority denial, `SIGKILL`, restart, resume and verification in a few seconds, with no SPIRE, Keycloak, VM or model key. Identity in this path is explicitly **unenforced**; see [experiments/try](experiments/try/README.md).

## Setup

- `make install`: install Python dependencies with uv.

The kernel has explicit initialization/provisioning/service commands in the [quickstart](docs/m1-quickstart.md); the M5 [Debian package and operating guide](packaging/README.md) cover installation, upgrades, recovery and maintenance. Repository: https://github.com/pasinit/cardea.

## License and security

Licensed under the [Apache License 2.0](LICENSE). See [SECURITY.md](SECURITY.md) for the trust model and how to report vulnerabilities.
