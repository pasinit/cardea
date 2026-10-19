# Applications and Result Verification

**Reuse decision.** Reuse parsers, schema validators, and test libraries; restrict custom code to explicit domain postconditions and version/evidence checks. See [R10](../standards-review-2026-09-22.md#r10--application-verification--m3).

**Purpose.** Define useful work and determine whether its observable acceptance criteria were met.

**Design.** Applications supply objectives, input expectations, and trusted installed verifiers without adding domain-specific concepts to the kernel. A completion proposal references output versions. The designated verifier runs with bounded authority/resources, reads actual artifacts or observed state, and returns evidence tied to those versions. Only then may the kernel commit success.

**Boundary and recovery.** Neither agent assertions nor tool exit codes establish completion. Reject missing, changed, or invalid artifacts. Retain versioned verification evidence across restart. The full first application produces an incident index and Markdown summary, then updates both after new input, including input arriving during downtime.

**Acceptance.** Corrupted or stale output fails; valid outputs match the input set and all summary references resolve.

**Status.** The index-only [demo verifier](../../apps/api/src/cardea/kernel/demo.py) inspects stored bytes. M1 additionally checks its JSON artifact bytes/version independently before recording success, and rejects corrupted output. M3 implements a strict incident batch/schema, exact input-set/content preservation and one anchored summary reference per incident. It checks artifacts independently of the provider and worker and publishes versioned evidence. This bounded verifier does not establish general semantic summary quality. Native SDK, portable service and deterministic-provider Debian scenarios pass; live GLM-4.7 restart/new-input acceptance also passes. The escalation app stays legacy.
