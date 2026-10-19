# Scope Decision: Hosted Agent OS

Later policy note, 2026-09-22: the accepted product scope below is retained. Implementation choices now follow [open source first](standards-first.md); the [review](standards-review-2026-09-22.md) reopens external-contract decisions without rewriting this historical scope decision.

Date: 2026-09-21. Status: accepted product direction from the user; implementation pending.

## Decision

Cardea becomes an agent-first operating layer above Debian/Linux, designed for eventual portability to other Unix-like hosts. Its kernel is a userspace authority, execution, state, and recovery service. The existing host kernel continues to manage hardware and native OS resources.

Agents are the primary clients of basic operations. Humans set objectives and authority and supervise execution. A human dashboard is optional; useful work must proceed headlessly within granted capabilities.

## Why This Replaces the Previous Scope

The previous documents and code centered on a team-level enterprise workflow, customer escalations, fixed evaluators, and an approval-driven command center. That demonstrated a workflow application rather than the agent-native OS requested by the user.

The old roadmap's interviews, CRM integrations, and command-center milestones do not measure kernel progress. Prior enterprise-governance research does not validate adoption of the new runtime.

## Retained and Deferred

Retain the Cardea name, monorepo, Python typed backend toolchain, existing runnable demo, and reusable contract/policy/audit patterns. Keep implementation separate from business-domain models. Defer the React UI to a possible supervisor role and the escalation workflow to a possible userspace application.

Do not rename simulated execution as real execution, in-memory audit as durable storage, an actor string as authenticated identity, or a business `ProcessObject` as an agent process.

## Initial Engineering Choices

Use one Debian host, a local service, SQLite metadata/journal storage, a private artifact store, typed operation dispatch, and a replaceable runtime/host boundary. These are planned implementation choices, not shipped guarantees. M0 pins the host release, model adapter, wire schemas, and enforcement profile.

A second Unix adapter is a future conformance exercise. There is no commitment to equal isolation on every Unix kernel and no new hardware kernel, distribution image, or multi-host scheduler in the MVP.

## Historical Baseline

The pre-rescope implementation is commit `ed1db60` (Build MVP command center). Git history preserves the earlier design, MVP, roadmap, and coding standard. `problem-validation.md` remains explicitly historical. No runtime behavior changes as part of this documentation rescope.
