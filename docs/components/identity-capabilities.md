# Identity and Capabilities

**Reuse decision.** [ADR-010](../decisions/010-m1-authenticated-kernel.md) records selected libraries, alternatives, fit evidence and remaining scope. Follow the [standards-first policy](../standards-first.md).

**Purpose.** Keep task authority below its live accountable supervisor and every ancestor.

**Design.** Persist immutable supervisor, logical-agent, runtime, namespace, explicit grants and parent binding at admission. Authenticate the supervisor rather than accepting a body claim. Casbin evaluates exact principal/namespace/action allow and delegation entries from the current SQLite transaction. Intersect those decisions with platform policy, explicit task grants and each ancestor; all involved principals must be enabled.

**Stored input grants.** [ADR-014](../decisions/014-object-scoped-host-inputs.md) reuses the same matcher for exact namespace/object/version/digest read grants. A host task explicitly selects one version, requiring both source-namespace authority and exact-version supervisor allow/delegate plus platform allow. No wildcard, latest-version or service-account bypass exists. Root host tasks only; transitive resource delegation remains future work.

**Boundary and recovery.** Host-owner provisioning replaces current policy and commits an audit event plus DBOS wakeups. Permission/delegation withdrawal, expiry and disablement restrict new work. Replayed DBOS results and broader service credentials cannot restore revoked authority. Identity reassignment and task provenance updates are rejected.

**Acceptance/status.** M1 enforces this bounded policy for its installed JSON runtime and API. Tests cover admission, dispatch, restart/resume, ancestor constraints, expiry and revocation. M2 adds isolated host execution (ADR-013) and exact-version input grants (ADR-014). ADR-015 adds separate copy/hash grants and supervisor host allocations. The MVP scope is exact immutable input and task-owned output; ADR-016 implements explicit model invocation/disclosure and additive-input grants for the bounded M3 profile. ADR-017 adds authenticated native A2A delegation, attenuated child grants and parent allocations. Child admission and dispatch recheck the original supervisor and every ancestor; cards and context IDs confer no authority.
