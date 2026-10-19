# Initial Runtime and Session Decisions

Original date: 2026-09-21; reviewed 2026-09-22. These record the implementation direction, with R01–R03 selections recorded in [ADR-001](decisions/001-m0-contracts.md) and later integrations still subject to the [open-source-first review](standards-review-2026-09-22.md). Authenticated transport, persisted identity, deterministic runtime and operations are now implemented by [ADR-010](decisions/010-m1-authenticated-kernel.md); ADR-012/013 add tested host isolation and one fixed installed operation; the real model adapter remains later work.

## Host and Execution Profile

Target Debian 13 (trixie), amd64, with systemd and the unified cgroup v2 hierarchy. Other architectures and Unix hosts require their own conformance evidence. Pin the exact image/package revisions in the first Debian enforcement test result; accept security updates rather than freezing an unmaintained patch release. Debian identifies trixie as its version 13 release: [official release information](https://www.debian.org/releases/trixie/).

The bounded `debian-local-v1` profile is implemented for one fixed operation under [ADR-013](decisions/013-installed-host-operations.md), following the systemd/bubblewrap fit in ADR-012. [ADR-014](decisions/014-object-scoped-host-inputs.md) adds bounded immutable-object inputs with exact source authority through the same worker profile. Broader profile acceptance remains open:

- A dedicated unprivileged kernel service account; a narrow host launcher provisions restricted workers.
- Worker mount/process/network isolation, read-only granted inputs, private scratch space, and controlled output publication.
- No inherited host home directory, kernel database, model credentials, or unrestricted network access.
- No privilege escalation; process-tree termination; CPU, memory, process-count, wall-time, and output limits.
- Probe the actual selected Linux/systemd facilities and run escape/limit tests before accepting this profile. A missing facility rejects dispatch.

The current local persistence demonstration is trusted in-process Python. It executes no arbitrary worker code and must not advertise `debian-local-v1` as supported. macOS tests establish storage/contract behavior only.

## Runtime Adapter

Use a deterministic adapter for M1 recovery evidence. The original store harness remains a precursor. M1 now has authenticated submission into a fixed trusted DBOS runtime that publishes and verifies transactional JSON bytes. General agents consume the service rather than storage; the installed kernel adapter alone participates in its trusted database transactions.

The initial `openai-responses-v1` direction is superseded by the user’s OpenRouter choice on 2026-09-24. [ADR-016](decisions/016-m3-openrouter-and-native-mcp.md) implements a native OpenRouter chat/tool-call adapter with the explicit model `z-ai/glm-4.7` for acceptance; live acceptance remains pending. The earlier direction below is historical: a trusted broker sends only scoped context and installed function schemas through the Responses API, receives structured function requests, and routes them through Cardea's operation broker. Disable provider-hosted execution tools and parallel function calls initially. Explicit strict function schemas and zero-or-one function-call behavior are documented in [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling). Model output remains untrusted and is validated again by Cardea.

The model ID is a required deployment setting recorded with each run; there is no implicit latest-model default. Exact model selection and adapter execution tests occur when implementing M3. The adapter accepts a bounded invocation containing task identity, authorized context/object references, permitted operation schemas, remaining token/step budgets, and a deadline. It returns proposed calls or a final result proposal plus measured usage. It cannot grant authority or mark the task verified.

Provider authentication uses a service-owned API credential loaded into the broker from a restricted credential file/environment. It never goes into worker environments, task JSON, receipts, or logs. Do not read credentials or make model calls during this storage milestone. Adapter implementations are replaceable without changing task/storage contracts.

## Identity and Local Transport — Revised 2026-09-22

The [standards-first decision](standards-first.md) supersedes the earlier custom bearer-token issuer as the default. Use SPIFFE workload identities and OpenID Connect human identities, mapped to stable internal principals. Evaluate SPIRE and an existing OIDC provider before building credential issuance or login. See [identity representation](components/identity-representation.md) and [authentication](components/authentication.md).

Retain the initial local-only daemon boundary, separate from the legacy API: a private Unix-domain socket in a service-owned mode-0700 directory, with socket mode 0600 and no TCP listener. Socket permissions do not distinguish agents sharing an account. ADR-001 selects JWT-SVIDs with a kernel-specific audience and the SPIFFE Workload API validation path for integration. The evaluated Python SDK’s direct validator has trust-domain and malformed-claim gaps; it is not approved as a standalone authenticator. ADR-009/010 validate the provider path with disposable UID selectors; deployments must choose selectors for their actual workload boundary. Human login must use a supported OIDC flow and appropriate application session/API credentials. Do not accept an arbitrary ID token as a kernel access token.

The handler derives the principal from validated external proof and a provisioned identity binding. Trusted dispatch establishes a logical agent/task binding; a shared runtime credential alone does not prove task identity. Reject body identity overrides and check principal disablement, task scope, and grants on requests and before dispatch. Persist the accountable supervisor and delegation provenance; task authority must remain within that supervisor’s current permissions and delegation authority, all ancestor ceilings, and platform policy. Old grants, approvals, retries, and service credentials cannot bypass that bound. Keep provider credentials out of model context, general tool workers, task records, and logs.

ADR-010 implements host-owner provisioning, selected provider libraries, session handling and the structured socket client; ADR-009 supplies real SPIRE bootstrap/rotation evidence. M2 implements exact immutable-input and task-owned-output grants, confined hash/copy workers, transactional host allocations and staging retirement (ADR-012–015). Broader ambient filesystem access is outside the MVP. No fallback custom issuer is approved without evidence that available solutions fail requirements. SPIFFE credential expiry does not replace immediate local authority checks.

The earlier `/kernel/v1/call` sketch is superseded by HTTP resource endpoints, OpenAPI 3.1/JSON Schema 2020-12, and the existing FastAPI/Pydantic/Uvicorn stack with an HTTPX Unix-socket client. ADR-001 verifies local socket and schema fit; ADR-010 now publishes the separate `/v0.2` kernel OpenAPI contract. Preserve the needed semantics: explicit schema compatibility, bounded requests, typed results/errors, correlation IDs, and operation receipts for safe recovery after a lost connection. The existing `cardea.kernel.cli` remains owner-level store administration, not this authenticated transport.

## Local Persistence Decisions

R04 retains SQLite; evaluate its backup/migration facilities before custom operational tooling. ADR-001 preserves v0.1 hashes and selects RFC 8785 for an explicit future schema/digest version with a restricted numeric domain; no migration has run. Use SQLite WAL with `synchronous=FULL`, private file permissions, an application ID, and numbered transactional schema migrations. Store task snapshots, unique dispatch leases, operation intents/receipts, versioned object bytes, and append-only journal entries in one database. Large artifacts are deferred: the initial object limit is one MiB. Keeping these bounded bytes in SQLite makes object publication and its journal entry part of the same transaction; no separate-file atomicity is claimed.

Mutations use `BEGIN IMMEDIATE` and compare revisions/versions. Any mutation failure poisons the transaction even if an internal caller catches the error. Reused dispatch lease IDs are rejected. A trusted worker must present its current unexpired lease for state changes and new operations.

Recovery only handles expired leases. Accepted but undispatched operations become cancelled; dispatched operations without a result become uncertain. A task with uncertain effects fails pending reconciliation, rather than silently replaying. Reconciling its receipt does not reopen a terminal task. Without uncertain effects, an eligible task can resume from a checkpoint, or from initial state if no completed operations would be repeated. External process termination and external effect reconciliation remain host/broker responsibilities.

SQLite's durability depends on its filesystem and host guarantees; local process-crash tests do not establish recovery from every storage-device or power-loss failure. See [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html).
