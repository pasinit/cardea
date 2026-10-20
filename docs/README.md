# Cardea documentation

## Use and operate

- [Getting started](getting-started.md): how a run flows through the kernel, the harness and MCP operations, plus six runnable use cases.
- [Try without SPIRE](../experiments/try/README.md): `make try`, unenforced identity.
- [M1 quickstart](m1-quickstart.md): initialize, provision and run the authenticated kernel.
- [Debian package](../packaging/README.md) and [operations guide](operations.md): install, upgrade, back up, restore, maintain.
- [Persistence demo](persistence-demo.md): the two-process checkpoint/resume demo.
- [Security](../SECURITY.md): trust model, out-of-scope threats, reporting.

## Architecture

- [Design](../design.md): layers, host boundary, agent-first semantics, trust and failure model.
- [Kernel contract](kernel-contract.md): implemented records and runtime semantics.
- [Runtime decisions](runtime-decisions.md): Debian target and local session contract.
- [Components](components/README.md): one short design per component, with status.

## Decisions

- [Decision records](decisions/README.md): ADR-001 to ADR-033.
- [Open-source-first policy](standards-first.md) and [review register](standards-review-2026-09-22.md).
- [Coding standard](../coding-standard.md).

## Planning and evidence

- [Roadmap](../roadmap.md): M0–M5 (complete) and the proposed M6–M10 product sequence.
- [MVP](../mvp.md) and [validation plan](../validation-plan.md): infrastructure acceptance boundaries.
- Milestone acceptance reports: [M2](../experiments/m2/M2-REPORT.md), [M3](../experiments/m3/README.md), [M4](../experiments/m4/M4-REPORT.md), [M5](../experiments/m5/M5-REPORT.md).
- [Product direction](../product.md), [gap assessment](product/gaps.md), [product validation protocol](product/validation.md), [market analysis](product/market-analysis-2026-09-25.md), [vertical ranking](product/vertical-ranking-2026-09-25.md). These are proposals and desk research, not customer validation.

## Historical

- [Scope decision, 2026-09-21](scope-decision-2026-09-21.md): the rescope to a hosted agent OS.
- [Problem research](../problem-validation.md): the pre-rescope enterprise thesis; not evidence for the current scope.
- Workflow-engine fit studies: [comparison](decisions/002-workflow-engine-comparison.md), [DBOS](decisions/003-dbos-fit.md), [Temporal](decisions/004-temporal-fit.md), [Absurd](decisions/005-absurd-fit.md), [Hatchet](decisions/006-hatchet-fit.md).
