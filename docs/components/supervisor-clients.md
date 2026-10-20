# Supervisor Clients

**Reuse decision.** [ADR-010](../decisions/010-m1-authenticated-kernel.md) records selected libraries, alternatives, fit evidence and remaining scope. Follow the [standards-first policy](../standards-first.md).

**Purpose.** Let authenticated supervisors submit, inspect, signal and cancel headless tasks.

**Design.** HTTPX over the Unix socket provides a typed Python client; argparse exposes the same CLI operations. Workload credentials refresh through SPIRE. Human login reuses Authlib/OIDC and stores a private refreshable session. The versioned kernel OpenAPI document is separate from the demo API.

**Boundary and recovery.** Credentials travel in headers, never task JSON. Stable submission IDs and control IDs support safe retries after lost responses. The service enforces current scope for every read and control action; UI/client code cannot grant extra authority. Local host-owner provisioning is separate administration.

**Acceptance/status.** Implemented Python/CLI clients with real socket/provider acceptance. No supervisor UI is required for M1; the legacy React escalation dashboard was removed on 2026-10-10. See [quickstart](../m1-quickstart.md).
