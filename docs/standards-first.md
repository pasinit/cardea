# Standards-First Decisions

Decision: 2026-09-22, applying the user's direction to the entire project.

Prefer established open standards and maintained open-source implementations. A standard defines interoperable behavior; a project implements it. Evaluate both. Custom code is justified only where suitable solutions do not exist or demonstrably fail requirements.

Use the [decision-record template](decision-record-template.md), [full review register](standards-review-2026-09-22.md), and per-milestone gates in the [roadmap](../roadmap.md). This applies to every component and existing bespoke proposal. It is a normal engineering obligation, not an extra permission ceremony.

Before implementation, record:

1. Required behavior and acceptance tests, including recovery and security boundaries.
2. Relevant standards and maintained implementations, with primary sources.
3. Fit and gaps: correctness, licensing, maintenance, interoperability, operational cost, and supported hosts.
4. Reuse choice, rejected alternatives with concrete reasons, and the smallest remaining Cardea-specific integration.

Prefer configuration, composition, and adapters before replacement. Do not call a mechanism standards-compliant merely because it uses JSON, JWTs, HTTP, or standard cryptographic algorithms. Pin and test chosen implementations. A documented mismatch is evidence for a bounded exception; inconvenience or unfamiliarity alone is not. Reuse does not require deploying every available infrastructure service.

## Immediate Consequences

| Area | Direction and remaining decision |
|---|---|
| Identity | SPIFFE/SPIRE workload identity and OpenID Connect human identity; selected provider libraries and bootstrap are verified in ADR-009/010. |
| Authentication | The earlier Cardea-issued bearer-token scheme is superseded. M1 uses provider validation and bounded local sessions; never accept caller actor strings as identity. |
| Authorization | ADR-010 evaluates OPA/OpenFGA/Casbin and selects transactional Casbin for exact ACLs; ADR-014 adds immutable-version scopes. Revisit richer relationships only for a concrete unmet requirement. |
| Persistence | SQLite transactions, SQLAlchemy/Alembic migrations and DBOS shared-store integration; ADR-015 adds only domain budget records and cleanup coordination. |
| Execution | ADR-012–015 reuse Debian systemd, bubblewrap, sudo and descriptor-safe standard-library deletion, with real host acceptance. |
| Protocols and recovery | ADR-001/010 implement HTTP/OpenAPI over a private Unix socket and versioned JCS while preserving v0.1 hashes. DBOS owns scheduling/wakeup recovery. ADR-016 implements native MCP for the bounded M3 incident task; ADR-017 implements native A2A for bounded M4 delegation, retaining one execution owner. |

Existing version 0.1 code remains unchanged until deliberate, versioned migration. Domain-specific task state and mappings are integration data, not a new identity or cryptographic standard. See [identity representation](components/identity-representation.md). This policy alone installs nothing; ADR-001 adds pinned evaluation dependencies and local fit tests, without installing an identity provider.
