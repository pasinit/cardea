# Daemon and Sessions

**Reuse decision.** [ADR-010](../decisions/010-m1-authenticated-kernel.md) records selected libraries, alternatives, fit evidence and remaining scope. [ADR-025](../decisions/025-remote-kernel-api.md) adds the optional TLS network listener. Follow the [standards-first policy](../standards-first.md).

**Purpose.** Expose one authenticated agent/supervisor API.

**Design.** FastAPI/Pydantic/Uvicorn serve `/v0.2` HTTP over an owned 0600 Unix socket. An optional TLS 1.3-only network listener (`ServiceConfig.network`, off by default, optional client-CA verification) serves the same app with the same limits; there is no plaintext listener, and peer addresses or client certificates confer no identity. HTTPX provides the structured client (socket path, or `https://` URL with an explicit CA bundle); OpenAPI exports the installed routes. SPIFFE/OIDC proofs resolve exact persisted principals. Body identity overrides are rejected. Requests, responses and event pages have fixed bounds.

**Boundary and recovery.** One process holds an exclusive store lock through OS termination. DBOS owns queue/history recovery. Unknown schema/runtime versions refuse startup. Errors omit credentials and raw provider failures. Clients inspect state and retry stable request IDs after lost responses.

**Acceptance/status.** Implemented: real authenticated socket calls, persistent disablement, crash/restart, SIGTERM, bounded errors, and cross-principal denial. Human login uses a private refreshable OIDC session. Deployment packaging and untrusted-worker isolation remain later gates.
