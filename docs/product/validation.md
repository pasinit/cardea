# Company Product Validation

Status: proposed protocol, 2026-09-25. No interviews, customer baseline runs or private pilots have been performed as part of this review. Historical research and engineering acceptance do not fill those evidence slots.

## Market access constraint

Do not depend on the founder's personal relationships for discovery, pilots or distribution. Use the [dated vertical screen](vertical-ranking-2026-09-25.md) to investigate repeatable public acquisition channels and compare qualified account cohorts. Its scores are desk-research hypotheses, not commercial evidence or a selected vertical. Track acquisition cash and labor, buyer authority, authorized data access, implementation effort and support burden alongside workflow value. Public directories establish discoverability only; outreach and company access still require their own authorization. No outreach has been performed.

## M6 job contract

Product owner and workflow owner complete this record before committing the first application scope. Interviews and data access require actual willing participants; this document does not authorize outreach or accessing company systems.

| Field | Required evidence | Current status |
|---|---|---|
| Company, participating departments, buyer and accountable supervisors | Named participants; who controls budget, access and acceptance | Open |
| Trigger, frequency and current process | Walkthrough of at least five complete cross-team cases, including an exception and handoff delays | Open |
| Desired result | Concrete artifact or destination state; quality rubric and independent reviewer | Open |
| Inputs and actions | At least two operational systems, related record IDs/schemas, process definitions or procedures, source ACLs, disclosures and required actions | Open |
| Operating envelope | Deadline, typical/peak volume, wait duration, recovery/data-loss tolerance | Open |
| Authority and intervention | Supervisor ceiling, exceptional actions, revocation and reassignment behavior | Existing kernel invariant; workload-specific policy open |
| Data lifecycle | Allowed data, storage location, retention/deletion/export and recovery handling | Open; current store has lifetime history |
| Baselines | Human process plus best adequate script, workflow or agent platform on matched cases | Open |
| Adoption and commercial hypothesis | Who will run it again, who installs/supports it, and what would justify paying | Open |

Observe operators and supervisors in every participating department plus IT; document where work or information currently gets lost between systems. Seek feedback from three prospective companies to check transferability; one internal team counts as one observation, not external validation. A single consenting company can support a limited first slice while broader discovery remains explicitly open.

## Compare complete jobs

For the selected job, freeze a representative input set and outcome rubric before comparing implementations. Include common cases, missing/contradictory inputs and realistic exceptions. Start with at least 20 matched cases where available; this is a planning floor, not statistical proof. For low-frequency work, report the smaller sample and extend observation rather than manufacture cases.

Compare Cardea with the current process and an adequate existing automation. Use a maintained agent framework when judgment is needed; use an ordinary script/workflow when it is not. Keep inputs, granted access, model settings where comparable, destination and review criteria matched. Report setup effort separately and amortize it over the observed workload volume. Randomize review order when practical. Preserve all attempts, retries, failures, rework and uncertain outcomes in the denominator.

| Metric | Measurement |
|---|---|
| Accepted useful outcomes | Reviewer-accepted results / all submitted jobs; report rejection and uncertainty separately |
| Human effort | Minutes preparing, reviewing, intervening and correcting each job; include failed attempts |
| Time to outcome | Trigger to accepted outcome, including waits and rework; report distribution |
| Total cost | Provider/tool/host usage plus measured setup and operator effort; state pricing assumptions |
| Operational reliability | Completed, failed, cancelled, blocked and unknown counts; recovery time and reconciliation results |
| Adoption | Actual recurring use by the owner, repeated weeks/cycles and reasons for stopping |
| Expansion | Second application's integration effort and changes required in the kernel or privileged broker |

Set improvement and quality thresholds with the owner before implementation. Suggested starting hypothesis: at least 30% less human effort without lower accepted-result quality and within an agreed total-cost ceiling. This is a proposed target, not a measured benefit; choose a different threshold when the real business case warrants it and record why before running the comparison.

Any observed authority breach, unauthorized disclosure or unsafe duplicate effect halts affected live work for correction. Zero observed breaches in a finite sample is not proof of universal safety. Ordinary authorized work should proceed without mandatory approval clicks.

## Evidence and decision gates

M6 exits when a job contract, observed baseline, acceptance thresholds and required reuse decisions are recorded. Do not claim discovery complete from this template. Bounded technical fit work may proceed independently, but broader application scope follows the selected job.

M7 delivers one accepted cross-team process spanning multiple company systems, with related data, source-authority enforcement, real handoffs, denial and interrupted-effect tests. M8 observes recurring use for at least four weeks or four natural work cycles, whichever better represents the job; record the rationale. M9 repeats the exercise with a second process/application and measures reuse of company integrations and controls. M10 targets at least two independent company pilots and an operator who can onboard from the documentation. These are proposed release gates; changes require an explicit roadmap update before interpreting outcomes.

If existing automation meets the job at lower total cost and comparable quality, reuse it or change the job hypothesis. If the second application needs business-specific kernel changes, revise the application boundary before declaring a company platform. If users do not return after assisted setup, investigate usefulness before adding a broad management UI.

## First backlog

| ID | Deliverable and acceptance | Accountable role | Dependency |
|---|---|---|---|
| P01 | Map the cross-team process, systems, related records, owners and outcome rubric | Product + workflow owner | Willing participant and appropriate data access |
| P02 | Record human and existing-solution baselines and thresholds | Product + evaluator | P01 |
| P03 | Test candidate application adapter with pinned dependencies; preserve DBOS state ownership, broker isolation and live ceilings | Runtime maintainer | P01 requirements; ADR-020 shortlist |
| P04 | Design cross-team enrollment, visibility and handoff authority; test disablement and stale identity state | Identity maintainer | P01; standards decision |
| P05 | Integrate required sources/actions across at least two systems, related records, process state and source ACLs; test change replay and uncertain effects | Integration maintainer | P01, P03–P04; connector decision |
| P06 | Deliver first accepted cross-team outcome and handoff evidence with machine-readable and human-readable explanation | Application maintainer | P02–P05 |
| P07 | Add required recurrence and exception handling; run matched repeated-work evaluation | Runtime + supervisor-client maintainers | P06; workload-specific trigger decision |
| P08 | Add second process/application and measure integration reuse, authority isolation and cost attribution | Product + application maintainer | Successful M8 evidence |

Roles identify responsibility, not assigned people. Dates, participants, access and sample evidence remain open. Synthetic fixtures can support fit testing; label them and never substitute them for the product baseline.
