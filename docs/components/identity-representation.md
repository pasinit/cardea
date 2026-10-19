# Identity Representation

**Reuse decision.** [ADR-001](../decisions/001-m0-contracts.md) selects standard external identity proofs with local application mappings and a versioned migration plan. [ADR-009](../decisions/009-spire-bootstrap-fit.md) adds real SPIRE attestation/validation and trusted binding fixture tests. [ADR-010](../decisions/010-m1-authenticated-kernel.md) implements authenticated admission and migration; host isolation remains M2. See [R01](../standards-review-2026-09-22.md#r01--identity-and-authentication--m0m1).

Implemented M1 representation: 2026-09-23. Existing v0.1 identity metadata remains compatible.

**Legacy metadata.** `AgentIdentity` contains `agent_id`, `owner_id`, `display_name`, `created_at`, and `disabled`. These are local metadata, not authenticated identities. Existing identifier fields cannot contain SPIFFE URIs; M1 preserves them as internal references and adds external bindings through Alembic.

**Persisted M1 records.**

| Record | Fields and responsibility |
|---|---|
| Principal | Stable internal `Identifier` (UUIDs permitted, not required), kind (`human`, `agent`, `service`), disabled flag; logical agents name an assigned runtime |
| Identity binding | Principal reference plus either an exact SPIFFE ID or the OIDC `(issuer, subject)` pair; administrative provisioning, not caller assertions |
| Execution binding | Verified runtime identity, logical agent principal, accountable supervisor principal, admitted task, parent/delegation provenance, and current lease; established through authenticated submission/dispatch |

Keep credentials and permissions separate. Ownership does not automatically grant permissions; the accountable supervisor is an explicit authorization binding, not an inference from `owner_id`. Task authority can never exceed that supervisor’s current permissions and delegation authority; changing the model/provider does not change the principal. External bindings must be unique and cannot be linked merely because display names or email addresses match.

**Workloads.** Use [SPIFFE](https://spiffe.io/docs/latest/spiffe-about/overview/) identities, for example `spiffe://cardea.example/agent/incident-indexer`. The service uses the verified [SPIRE](https://spiffe.io/docs/latest/spire-about/spire-concepts/) Workload API provider path. The URI is a name; a validated SVID establishes proof. Selectors must distinguish actual workloads. A shared Unix UID or runtime SVID cannot independently authenticate every logical agent/task it hosts. Trusted dispatch must establish task bindings; independently untrusted agents require distinguishable execution boundaries.

**Humans.** Use [OpenID Connect](https://openid.net/specs/openid-connect-core-1_0.html), keyed by exact `(iss, sub)`. Reuse an existing provider; [Keycloak](https://www.keycloak.org/) 26.7.4 verifies the implemented flow; deployments may use another conforming configured provider. A human login identity does not itself confer supervisor authority.

**Acceptance.** Credential rotation preserves the principal; unknown trust domains/issuers, disabled principals, forged bindings, and cross-task requests fail. Authentication identifies the caller; separate current grants authorize each operation. Library fit results and direct-validator gaps are in ADR-001. ADR-009 verifies real provider validation, rotation and fixture binding denials, and demonstrates same-UID sharing. ADR-010 adds persisted authenticated provenance, human sessions and current transactional policy checks. Production workload selectors and isolated host execution remain deployment/M2 responsibilities.
