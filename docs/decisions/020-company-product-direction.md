# ADR-020: Company Product Direction and Reuse Shortlist

- **Date / owner / milestones:** 2026-09-25; product and architecture maintainers; M6–M10.
- **Status:** company application plus company-wide operating-layer direction selected by the project owner, without external customer evidence. Delivery sequence revised by [ADR-021](021-company-process-data-integration.md): cross-team process/data integration is required from the first product delivery, superseding the single-team entry approach. New technical candidates shortlisted only; no adoption, fit tests or new dependency changes in this review.
- **Requirement:** deliver a useful recurring company job, then a second departmental application using shared identity, live authority, durable execution, protected system access and checked outcomes. Preserve the existing Debian and kernel contracts.

## Alternatives and primary sources

Official documentation, repository license declarations and GitHub release metadata checked on 2026-09-25. Release labels below are observations, not selected versions. All six OSS repositories below were unarchived, with repository pushes observed during September 2026. Recent activity is a maintenance signal, not a security review. Dependency advisories, support commitments, exact artifact licensing and compatibility remain selection gates.

| Candidate | Observed source / release / license | Relevance and untested fit |
|---|---|---|
| Pydantic AI | [Repository](https://github.com/pydantic/pydantic-ai), [v2.50.0](https://github.com/pydantic/pydantic-ai/releases/tag/v2.50.0), MIT | Candidate agent runtime to replace application-specific loop code. Official [DBOS integration](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/) exists. Test against Cardea's pinned DBOS, effect broker, checkpoint/version ownership and cancellation. |
| LangGraph | [Repository](https://github.com/langchain-ai/langgraph), MIT; latest GitHub release endpoint returned CLI `0.4.32.dev0`, not a core-library version | Existing [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) means durability is not unique to Cardea. Baseline or runtime candidate; pin the actual library separately and test competing state/transaction ownership before integration. |
| Agno | [Repository](https://github.com/agno-agi/agno), [v3.0.11](https://github.com/agno-agi/agno/releases/tag/v3.0.11), Apache-2.0 | Agent framework/runtime with management surfaces: compare time to first useful application and operating effort. Test live supervisor ceiling, confinement and recovery; inspect licensing/deployment of each UI or hosted component separately. |
| agentgateway | [Repository](https://github.com/agentgateway/agentgateway), [v1.5.0](https://github.com/agentgateway/agentgateway/releases/tag/v1.5.0), Apache-2.0 | Candidate for model, MCP and A2A connectivity. Test identity propagation, per-task attenuation, cancellation, credential isolation and budget accounting. A gateway does not by itself prove kernel policy enforcement. |
| Windmill | [Repository](https://github.com/windmill-labs/windmill), [v1.817.0](https://github.com/windmill-labs/windmill/releases/tag/v1.817.0), [mixed licensing](https://github.com/windmill-labs/windmill/blob/main/LICENSE) | Baseline for jobs solvable with scripts/workflows. AGPL source, Apache clients/specifications and proprietary enterprise portions differ; distributed Community Edition includes additional restrictions. Select an appropriate artifact before reuse or redistribution. No integration selected. |
| Keycloak | [Identity capabilities](https://www.keycloak.org/), [26.7.4](https://github.com/keycloak/keycloak/releases/tag/26.7.4), [Apache-2.0 repository](https://github.com/keycloak/keycloak) | Candidate IdP/broker when the company lacks a suitable existing OIDC provider. Test enrollment, group changes, disablement and stale-token handling. Do not build a password directory or assume group claims alone satisfy current authority checks. |

[Microsoft Agent 365](https://learn.microsoft.com/en-us/microsoft-agent-365/overview) is a commercial product comparator for centralized agent inventory, lifecycle and enterprise oversight, not an OSS reuse candidate. Its documentation describes those capabilities; this review did not independently test them. Company agent management is an existing product category, not evidence of an unoccupied market.

For additional company capabilities, examine [SCIM provisioning](https://www.rfc-editor.org/info/rfc7644/) and its current updates before inventing membership synchronization, and [OpenTelemetry](https://opentelemetry.io/docs/) before custom telemetry transport. Specific adapters, versions and fit remain open. SCIM synchronization and cached tokens cannot replace the kernel's live authority check.

## Decision and smallest custom scope

Keep the selected DBOS/SPIRE/OIDC/Casbin/storage/host foundation while evaluating an application boundary. First compare composition of the existing kernel with a maintained runtime against using an existing platform directly. No candidate has been rejected as inadequate by this desk review. No new bespoke scheduler, identity provider, connector protocol or agent framework is justified here.

The likely Cardea-specific work is binding an application invocation to its accountable supervisor, permitted operations, durable receipts, budgets and result verifier. This is a hypothesis about the remaining gap, not authorization to duplicate a reusable mechanism. Each implementation decision must identify which requirements candidates actually fail and minimize the adapter accordingly.

## Fit and integration gate

Run the selected job and a small fault matrix with pinned candidate versions: duplicate delivery, crash before/after effect, uncertain provider outcome, cancellation, supervisor/group revocation during a wait, disabled application version, budget exhaustion and denied data disclosure. Demonstrate who owns authoritative task state and which process holds credentials. A framework's successful function return or remote task status cannot substitute for result evidence.

Evaluate source-system OAuth/SDK/MCP implementations for the actual connector, including source ACLs and write reconciliation. Evaluate trigger delivery using the existing durable owner and maintained scheduling/event facilities; do not add an independent queue without a documented requirement. Define migrations, application-version recovery and retirement before installed applications can update.

## Verification and revisit

Completed here: repository gap inspection, primary-source shortlist and release/license/maintenance metadata review. Not run: candidate installations, security audits, compatibility tests, company interviews, product baselines or pilots. M0–M5 reports remain the implementation evidence for existing behavior.

Revisit the direction if an existing product meets the chosen job more cheaply, users do not adopt the first application, a second application requires business logic in the kernel, or required company availability/isolation exceeds the single-host profile. [Product validation](../product/validation.md) supplies the outcome and adoption gates.
