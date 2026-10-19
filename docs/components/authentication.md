# Authentication

**Reuse decision.** [ADR-010](../decisions/010-m1-authenticated-kernel.md) records selected libraries, alternatives, fit evidence and remaining scope. Follow the [standards-first policy](../standards-first.md).

**Purpose.** Validate external identity independently from task authority.

**Design.** Reuse py-spiffe Workload API JWT-SVID validation against configured trust domain/audience, and Authlib authorization code + PKCE for human login. Verify signed ID-token issuer/audience/nonce at login; API requests require RFC 7662 introspected access tokens. Configure a trusted issuer and same-origin HTTPS endpoints. Exact external bindings and disabled flags live in SQLite; no Cardea token issuer exists.

**Boundary and recovery.** Every request resolves current principal state. Every newly executed phase validates runtime identity and the current policy ceiling. Unavailable validation fails closed. Private OIDC sessions refresh access tokens; ID tokens never authenticate API calls. Provider registration/account deletion does not replace explicit kernel revocation. Same-UID processes remain one host trust boundary. Over the optional TLS listener ([ADR-025](../decisions/025-remote-kernel-api.md)) the same bearer validation applies; TLS 1.3, audience binding and short token lifetimes bound replay.

**Acceptance/status.** Implemented and tested with real SPIRE and Keycloak, including PKCE/state/nonce/replay denial, refresh, wrong audience, outages, persistent disablement and restart. ADR-009 separately records provider rotation/bootstrap. General worker credential isolation remains M2.
