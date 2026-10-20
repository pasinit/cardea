# Cardea Product Direction

Updated 2026-09-25. Direction: serve both companies adopting specific agent applications and companies operating agents across departments. Customer demand, first workload and commercial model remain unvalidated.

## Product promise

Give company agents useful work, approved access and an accountable supervisor; let them execute, wait and recover, while the company can see the result, evidence, cost and exceptions.

The entry product is a connected company process spanning multiple departments and operational systems. The proposed first boundary is two or three departments, at least two systems, shared records and an end-to-end outcome. The expansion product supports additional company processes and applications on that same operating layer. Both use the same authority, execution and evidence foundation. Agents remain the primary execution clients; humans define objectives, assign authority and handle exceptions.

## Who uses and buys it

| Role | Job Cardea should support | Evidence today |
|---|---|---|
| Business owner / department lead | Delegate recurring work and judge whether its outcomes justify the cost | Target role; no current customer validation |
| Supervisor / operator | Understand blocked work, inspect evidence, cancel or intervene within authority | Bounded API primitives; company supervision experience missing |
| IT / security owner | Connect company identity and systems, control access, offboard people and operate recovery | Tested local foundation; company lifecycle missing |
| Agent developer / integrator | Install and maintain an application without rebuilding permissions and recovery | Fixed installed profiles; reusable application boundary missing |
| Company sponsor | Expand useful applications across departments with shared controls | Expansion hypothesis; no adoption or purchasing evidence |

Initial delivery hypothesis: one company per supported Debian installation, administered by its IT owner or an explicitly appointed operator. Departments are authorization boundaries inside that company. Multi-tenant SaaS, multi-host operation and managed hosting are separate decisions.

## What exists

M0–M5 establish a tested, bounded execution foundation: authenticated tasks, live supervisor ceilings, durable state, confined hash/copy jobs, one incident-processing agent, native MCP tools and async A2A delegation, plus Debian packaging and recovery. The [M5 report](experiments/m5/M5-REPORT.md) describes the accepted installation profile.

The incident application accepts a bounded batch and one additive input; it is not a general company agent. Neither proves useful business outcomes, unattended company operation or market demand. See the [gap assessment](docs/product/gaps.md).

## First application selection

Choose an observed recurring cross-team process with accountable owners, related data in multiple systems, a measurable result and a plausible reason to need agent judgment. Include actual handoffs, dependencies and required downstream actions in the first scope. A task isolated within one team cannot demonstrate the intended product value.

| Candidate job — all hypotheses | Outcome to test | Main selection risk |
|---|---|---|
| Operational exception investigation across support and engineering | Evidence-backed diagnosis and actionable recommendation | Requires source access and credible ground truth; summaries alone may add little value |
| Recurring operations reporting and data-quality review | Accepted report with traceable discrepancies | Existing reporting or scripts may already solve it more cheaply |
| Internal service-request handling across teams | Correct routing, evidence collection and bounded fulfillment | Identity, personal data and downstream write permissions increase scope |

Do not select incidents solely because M3 already implements them. Compare the current human process and the best adequate existing automation before choosing. The [validation protocol](docs/product/validation.md) defines the evidence and selection gate.

## Product hypothesis and boundaries

The [market and competitor analysis](docs/product/market-analysis-2026-09-25.md) compares direct enterprise platforms, smaller agent products and internal builds. It identifies process/integration competitors as well as runtime alternatives, and keeps proposed differentiation, target buyers and pricing separate from validated demand.

The [vertical ranking](docs/product/vertical-ranking-2026-09-25.md) screens 24 market segments for potential Cardea economics and cold-start customer access. Acquisition must work without relying on the founder's personal relationships. Technical consultancies, accounting firms and compliance consultancies are the recommended discovery cohort, not selected markets or validated demand. Specialization applies to the first application and integrations while preserving the cross-department product scope.

The hypothesis is that a shared authority and recovery layer reduces the work required to operate useful agents across company applications. Persistence, agent frameworks and enterprise agent registries already exist; the [standards and product comparison](docs/decisions/020-company-product-direction.md) records alternatives to reuse and compare. Differentiation and willingness to pay must be demonstrated.

Preserve these boundaries throughout expansion:

- A task never exceeds its supervisor's live permissions, delegation authority, ancestor ceilings or platform policy. Company roles, approvals and connector credentials cannot enlarge it.
- Company processes and data must be integrated through generic kernel contracts for scoped resources, events, tasks and evidence. Versioned company schemas, process definitions and business rules live in the integration/application layer. See the [company integration design](docs/components/company-integration.md); reuse maintained process, runtime and connector implementations where fit tests support them.
- Native MCP connects agents to tools; native A2A connects agents to agents. The kernel API remains its own control contract.
- Supervision surfaces use the same authorization path as agents. Already authorized work does not require ceremonial approval clicks.
- External agents outside Cardea's enforcement boundary cannot inherit its confinement or revocation guarantees merely by being registered.

## Delivery sequence

[M6–M10](roadmap.md#m6--validate-the-first-company-job) move from a measured cross-team process and integration design, to connected company execution, to repeatable operation, to reuse for a second process, to a private company release. Cross-team identity, shared data and handoffs are first-delivery requirements. Reuse for a second process remains a separate success criterion. No calendar or commercial commitment is implied.

The immediate deliverable is a company process map, system/data inventory, cross-team authority model and baseline for one end-to-end process. The assessment and roadmap are ready; observed workflow examples, customer conversations and pilot outcomes are still needed.
