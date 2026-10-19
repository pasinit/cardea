# Repository Instructions

Read `.agents/AGENT.md` and `coding-standard.md` before implementation.

Prefer established open standards and maintained open-source implementations for all design decisions. Before building a custom mechanism, document the alternatives and concrete requirements they fail to meet; implement only the remaining gap. Follow `docs/standards-first.md`. Existing bespoke proposals are subject to this review.

## Required Modus Operandi: Open Source First

- Apply this to every task, component, dependency, protocol, and milestone, including existing designs.
- Search for relevant open standards and maintained open-source implementations before designing a replacement. Prefer reuse, configuration, composition, and small adapters.
- Before implementation, record requirements, candidates, primary sources, license/maintenance checks, fit tests, and the choice in a decision record. Follow `docs/decision-record-template.md` and `docs/standards-review-2026-09-22.md`.
- Invent only when no suitable solution exists or tested/documented gaps make available solutions inadequate. Record the exact gap, rejected alternatives, and smallest necessary custom scope. Convenience, unfamiliarity, and already-written code are insufficient reasons.
- Keep candidate, selected, implemented, and verified status separate. Do not silently change existing contracts or mark a milestone complete while its reuse decisions or acceptance checks remain unresolved.
- Include decision evidence and remaining gaps in handoffs and milestone reviews. This is part of normal autonomous work, not a new requirement to ask the user for permission.

Cardea was rescoped on 2026-09-21 as an agent-first userspace operating layer hosted on Debian/Linux, with future Unix host adapters. The pre-rescope customer-escalation demo lives only on the `archive/legacy-escalation-demo` branch. Use `design.md`, `docs/kernel-contract.md`, `mvp.md`, and `roadmap.md` for current scope. Clearly distinguish proposed primitives from implemented behavior.

## Mandatory Authority Ceiling

A task's effective permissions must never exceed its accountable supervisor's current permissions or delegation authority. Bind each task to an authenticated supervisor and intersect explicit task grants with that live ceiling, platform policy, and every ancestor task's effective authority. Recheck at grant creation, admission, dispatch, retry, and resume; supervisor revocation or disablement restricts all descendants. Approval, service credentials, role labels, and task reassignment cannot bypass this ceiling. Follow the authority-ceiling section of `docs/kernel-contract.md`; it is a required design/enforcement invariant, not a claim that enforcement already exists.
