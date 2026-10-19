# Component Designs

Snapshot: 2026-09-24. These concise designs elaborate the current [architecture](../../design.md), [kernel contract](../kernel-contract.md), and [runtime decisions](../runtime-decisions.md); those documents remain authoritative. Interfaces described below are proposed unless explicitly marked implemented.

Cardea runs above Debian/Linux. Its kernel is deterministic; agents choose work, the kernel controls authority and durable state, and the host enforces execution boundaries. These are logical ownership boundaries, not separate services.

All components follow the [standards-first decision policy](../standards-first.md). The 2026-09-22 identity direction supersedes the earlier custom-token proposal; M1 now implements its SPIFFE/OIDC adapters in ADR-010.

| Component | Current implementation | Delivery |
|---|---|---|
| [Daemon and sessions](daemon-sessions.md) | Authenticated v0.2 Unix-socket daemon and client | M1 |
| [Authentication](authentication.md) | Real SPIRE/OIDC validation, login and refresh | M1 |
| [Identity representation](identity-representation.md) | Immutable persisted principal and task provenance | M1–M2 |
| [Identity and capabilities](identity-capabilities.md) | Live Casbin namespace/task, exact-version input and ancestor ceilings | M2–M4 |
| [Tasks and scheduler](tasks-scheduler.md) | DBOS dispatch, durable waits, separate coordinator queue and recovery | M1–M4 |
| [Model and MCP broker](model-broker.md) | OpenRouter/native MCP incident profile; live Debian acceptance passed | M3 |
| [Operation broker](operation-broker.md) | Fixed JSON, confined SHA-256 and byte-copy operations | M2 |
| [Objects and persistence](objects-persistence.md) | Private SQLite store and versioned bytes | M1 |
| [Storage detail](storage.md) | Alembic extension migration and shared-store backup/restore | M1/M5 |
| [Events and input adapters](events-inputs.md) | Transactional DBOS wakeups and bounded authenticated directory input | M1–M3 |
| [Company process and data integration](company-integration.md) | Proposed: scoped company records, related data, process instances and cross-team handoffs; no implementation yet | M6 design / M7 first delivery |
| [Resource budgets](resource-budgets.md) | Transactional host/model reservations and bounded child allocations | M2–M4 |
| [Agent runtime and model broker](agent-runtime.md) | Deterministic runtime and bounded OpenRouter/MCP incident adapter | M1/M3 |
| [Debian host adapter](debian-host.md) | Systemd/bubblewrap fit and fixed host dispatch implemented | M2 |
| [Installed host operations](installed-host-operations.md) | SHA-256/copy workers, scoped inputs and recoverable staging cleanup | M2 |
| [Applications and verification](applications-verification.md) | Independent JSON and incident index/summary verification; live acceptance passed | M3 |
| [Delegation and messaging](delegation-messaging.md) | Native async A2A and bounded child coordination; Debian/load acceptance passed | M4 |
| [Supervisor clients](supervisor-clients.md) | Authenticated HTTPX/CLI client; OIDC login | M1/M5 |
| [Packaging and portability](packaging-portability.md) | Debian package, upgrades, fenced restore, diagnostics and retention | M5 |

All 17 component/detail documents link to their reuse decision in the [review register](../standards-review-2026-09-22.md). M0’s code and R01–R03 selection gate are complete in [ADR-001](../decisions/001-m0-contracts.md), with 27 local fit cases. [ADR-010](../decisions/010-m1-authenticated-kernel.md) records implemented M1 authentication, endpoints, migrations and runtime evidence. Later milestone candidates remain subject to their own selection and acceptance gates. Build order follows the [roadmap](../../roadmap.md). Portable tests demonstrate contracts and persistence; Debian enforcement requires separate host tests.
