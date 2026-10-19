> Historical research for the enterprise workflow thesis, retained as of 2026-09-21. This document is superseded as project direction by [design.md](design.md) and is not validation of the agent OS. Its market claims and sources have not been refreshed for the rescope. Use [validation-plan.md](validation-plan.md) for current acceptance and adoption questions. Current engineering decisions follow [open source first](docs/standards-first.md); this historical research is not a component-selection record.

# Problem Validation

Date: 2026-06-03

## Verdict

The problem is real, and the stronger thesis is broader than agent governance alone.

The updated thesis is:

> Modern startups are becoming agent-native operating environments. Large enterprises were not built that way. Cardea should make existing enterprise software behave like an agent-native operating loop: observe work, model live process state, evaluate what is going wrong, and safely let agents close the loop.

Public evidence strongly validates the trust, control, visibility, monitoring, and governance parts of the problem. The broader "single operating layer across enterprise processes" still needs customer validation, but it is directionally consistent with where the market is moving: observability, governance, identity, workflow orchestration, and AI agents are converging.

## Testable Claims

1. Large enterprises cannot reliably understand live operational state because work is fragmented across many systems and communication channels.
2. Existing enterprise software is optimized around systems of record and human-operated workflows, not agent-mediated process loops.
3. Agentic AI adoption is blocked by security, privacy, compliance, identity, monitoring, and control gaps.
4. A useful enterprise agent OS must do more than answer questions. It must observe process state, evaluate health, recommend or execute action, and verify whether the loop closed.
5. Governance is not a side feature. Permissioning, policy checks, approvals, and audit trails must sit directly in the action path.
6. The first wedge should be one high-value cross-system enterprise process, not a generic all-company dashboard.

## Evidence For

### Standards and security bodies are formalizing agent identity and control

NIST NCCoE published a February 2026 concept paper on software and AI agent identity and authorization. It explicitly frames AI agents as systems that autonomously perform tasks and says realizing their benefits requires identifying and authorizing their access to diverse datasets, tools, and applications. NIST also asked for feedback on identification, authorization, auditing, non-repudiation, and prompt-injection controls.

OWASP released a Top 10 for Agentic AI Applications in December 2025. The examples map directly to the required trust layer: goal hijack, tool misuse, identity and privilege abuse, memory/context poisoning, insecure agent communication, human-agent trust exploitation, and insufficient safety around autonomous action.

### Surveys show production rollout is blocked by operational trust gaps

Dynatrace's 2026 agentic AI report surveyed 919 senior leaders and decision makers at large enterprises. The top barriers to production were security, privacy, or compliance concerns at 52%, and technical challenges managing and monitoring agents at scale at 51%. Detailed technical barriers included limited ability to set clear rules for autonomy versus human approval, limited visibility into decisions, monitoring cost/compliance as agents change, and tracing downstream impacts.

Okta's January 2026 buyer survey says 69% of respondents report security concerns slowing AI agent adoption, only 27% agree their current identity systems are fully equipped to govern non-human identities at scale, and the top concerns are data leakage and over-privileged access.

SailPoint's 2025 enterprise survey reported that 82% of organizations already use AI agents, but only 44% have policies in place to secure them. Respondents reported broad concern over access to privileged data, unintended actions, sensitive-data sharing, and lack of visibility.

CSA and Google Cloud's 2025 AI security and governance survey found governance maturity is strongly tied to AI readiness. Organizations with comprehensive governance policies were nearly twice as likely to report early agentic AI adoption as those with partial guidelines.

### Incumbent activity validates the direction

Microsoft Agent 365 is positioned directly as a "control plane" to observe, govern, and secure agents. Its capabilities include an agent registry, access control, lifecycle management, audit and logging, data compliance, and identity protection through Microsoft Entra.

Microsoft Entra Agent ID treats agents as governable identities with owners/sponsors, lifecycle management, access packages, approval paths, and expiring access.

ServiceNow AI Control Tower is positioned around discovering, observing, governing, securing, and measuring enterprise AI across systems. ServiceNow is adding real-time controls for agentic workloads, identity/access governance, scoped permissions, least-privilege enforcement, and shutoff behavior when agents exceed permissions.

This validates the category pressure. It also means a new company must be sharper than "generic AI governance dashboard." The opportunity is to own the operating loop across enterprise processes: live state, evaluation, action, and proof of closure.

## Evidence Against Or Not Yet Proven

The problem is real, but these are not yet proven:

- That buyers want a standalone cross-enterprise operating layer rather than capabilities bundled into Microsoft, ServiceNow, Salesforce, Okta, Datadog, cloud providers, or model platforms.
- That "agent-native operating layer" is a clear buying category. It may work as a thesis, but the product must be explained through concrete workflows.
- That broad enterprise integration is feasible early. Entity resolution, permissions, and process semantics will be difficult across messy internal systems.
- That enterprises will trust a new vendor to sit in the action path for sensitive workflows.
- That evaluation can be accurate enough to avoid becoming noisy process alerts.
- That there is a wedge process with urgent pain, reachable buyer, clear ROI, and enough permission to act.

## Current Best Framing

Use this as the near-term problem statement:

> Large enterprises are trapped in human-operated, tool-fragmented process systems. Startups can become agent-native from day one, but incumbents need a governed layer that observes their existing software, evaluates process health, and safely lets agents close the loop.

Use this as the product description:

> Cardea is a governed operating loop over enterprise software. It connects internal systems, builds a live process graph, evaluates what is blocked or at risk, and routes agent actions through permissions, approvals, and audit trails.

Avoid reducing the product to:

> Agent governance.

Governance is necessary, but the product promise is:

> One place to understand what is happening across the enterprise and safely move work forward.

## Initial Wedge Hypothesis

Start with one process where the enterprise already has real pain from fragmented tools and manual follow-up.

Good wedge criteria:

- The workflow spans at least 3 systems.
- The current process depends on status meetings, Slack or Teams chasing, spreadsheets, and manual updates.
- There is a measurable failure mode: missed SLA, delayed renewal, unresolved incident, blocked approval, audit scramble, customer escalation, or access risk.
- The product can recommend or execute concrete next actions.
- The product can verify whether the action changed downstream state.
- The buyer already has budget for operations, transformation, AI, compliance, or workflow automation.

Candidate first wedges:

1. Customer escalation and renewal command center.
2. IT or security incident coordination.
3. Enterprise procurement or access review workflows.
4. Compliance evidence collection.
5. Cross-functional implementation or onboarding programs.

Coding agents remain a useful demo path, but they should not define the company. The broader idea is agent-native enterprise operations.

## Validation Plan

Interview 15 to 20 people who own cross-system enterprise processes:

- COO or business operations leaders.
- CIO and enterprise architecture leaders.
- Heads of AI transformation.
- Platform and automation teams.
- Security, risk, compliance, and audit leaders.
- Owners of customer operations, support, renewals, ITSM, procurement, and access review.

Ask for concrete stories, not opinions:

- Which processes require the most manual status chasing?
- Which systems hold the state of that process?
- Where do handoffs fail?
- Which approvals are slow, unclear, or unauditable?
- What metrics show the process is unhealthy?
- What does leadership ask for that takes too long to answer?
- What would a perfect "what is going on?" answer include?
- Which actions could an agent safely take today?
- Which actions would require approval?
- How would they prove the loop closed?
- Which incumbent vendor would they expect to solve this?
- What would they pay for: visibility, action, evaluation, audit, or all of the above?

Disqualify or narrow the idea if most teams say one of the following:

- They do not have urgent cross-system process pain.
- Existing enterprise search, BI, workflow automation, or ITSM tools already solve the problem well enough.
- They will only accept this capability from an incumbent system of record.
- They want dashboards and summaries but will not let the product take action.
- The pain is not tied to measurable outcomes or budget.

## Sources

- NIST NCCoE, "Accelerating the Adoption of Software and Artificial Intelligence Agent Identity and Authorization", 2026-02-05: https://csrc.nist.gov/pubs/other/2026/02/05/accelerating-the-adoption-of-software-and-ai-agent/ipd
- OWASP, "Top 10 for Agentic Applications", 2025-12-09: https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/
- Dynatrace, "The Pulse of Agentic AI in 2026": https://cdn.dm.dynatrace.com/assets/documents/reports/bae22697-agentic-ai-report-2026.pdf
- Okta, "Survey: AI agent security is now a priority for enterprise buyers", 2026-02-23: https://www.okta.com/newsroom/articles/enterprise-buyer-survey-ai-agent-security/
- SailPoint, "AI agents: the new attack surface", press release, 2025-05-28: https://www.sailpoint.com/press-releases/sailpoint-ai-agent-adoption-report
- Cloud Security Alliance and Google Cloud, "State of AI Security and Governance Survey Report", press release, 2025-12-18: https://cloudsecurityalliance.org/press-releases/2025/12/18/csa-and-google-cloud-study-finds-governance-maturity-is-strongest-predictor-of-ai-readiness
- Gartner, "Applying Uniform Governance Across AI Agents Will Lead to Enterprise AI Agent Failure", 2026-05-26: https://www.gartner.com/en/newsroom/press-releases/2026-05-26-gartner-says-applying-uniform-governance-across-ai-agents-will-lead-to-enterprise-ai-agent-failure
- Microsoft Agent 365: https://www.microsoft.com/en-us/microsoft-agent-365
- Microsoft Entra, "Governing Agent Identities": https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview
- ServiceNow, "AI Control Tower", 2026 expansion announcement: https://newsroom.servicenow.com/press-releases/details/2026/ServiceNow-expands-AI-Control-Tower-to-discover-observe-govern-secure-and-measure-AI-deployed-across-any-system-in-the-enterprise/default.aspx
