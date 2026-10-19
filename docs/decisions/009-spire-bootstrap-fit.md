# ADR-009 — SPIRE Bootstrap and Identity Binding Fit

Date: 2026-09-23. Owner: kernel/identity. Scope: bounded R01/M1 experiment after ADR-008.

**Status: bounded provider experiment implemented and verified locally. Select SPIRE Workload API validation plus narrow application identity/binding checks for M1 integration. Production authentication, provenance migration and R01 integration remain open.**

## Requirement and Reuse Decision Before Implementation

Exercise real workload attestation, JWT-SVID issuance and Workload API validation before implementing the authenticated kernel. Test audience, expiry, malformed proof, signing-key rotation, trust-domain boundaries, provider outage and restart. Demonstrate what a shared runtime identity can and cannot establish about a logical task, supervisor and lease. Preserve existing v0.1 records and receipt hashes.

Select SPIRE **1.15.3**, released 2026-08-21, from the maintained, non-archived upstream repository (Apache-2.0). Build the pinned upstream source natively on macOS using its required Go toolchain; do not imply Debian confinement evidence. Reuse the already evaluated **py-spiffe 0.3.1** Workload API client (Apache-2.0), with explicit deadlines. Current API/release observations and exact source/build hashes will accompany results. No comprehensive transitive security audit is claimed.

| Candidate | Disposition |
|---|---|
| SPIRE server/agent + standard Workload API | Selected: existing attestation, issuance, trust bundle and validation implementation. Use local SQLite, built-in Unix workload attestor and join-token node bootstrap in disposable storage. |
| SDK direct JWT validator | Rejected as the complete authenticator: ADR-001 measured trust-domain and missing-expiry gaps. Do not bypass the provider on errors. |
| OIDC provider/Authlib | Retain for humans; not a substitute for local workload attestation. Human login/session integration remains separate. |
| Custom issuer or task JWT format | No demonstrated need. Reuse provider credentials; represent task/supervisor/lease relationships as trusted application state. |

The smallest adapter is provider-error normalization, exact configured identity/trust-domain mapping and a fixture for trusted execution bindings. No custom cryptography, credential issuance, policy engine or production grant system. A valid credential establishes a workload; it does not authorize an operation. The task's live supervisor/ancestor authority ceiling remains mandatory and is not supplied by SPIRE.

## Bootstrap and Test Boundary

Use a private temporary directory, loopback server, private administrative socket, explicit public trust-bundle bootstrap and one-time join token delivered through a private file. Do not use `insecure_bootstrap`, print credentials, access real providers, or install system services. Register workload selectors through the administrative socket. Test unmatched selectors and same-UID ambiguity rather than presenting UID attestation as logical-agent isolation.

Use SPIRE's public local-authority commands for signing-key rotation. If malformed signed JWTs require a controlled signer, add a fictional federated bundle using public bundle/registration APIs; generate only negative-test proofs, not a Cardea issuer. Test provider trust-domain lookup and the narrower Cardea allowlist separately.

Trusted binding fixtures will derive the runtime from verified proof and resolve task, agent, supervisor, ancestor and lease from provisioned state. Caller-supplied task/lease identifiers select records, never create provenance. A shared runtime can act for its legitimately assigned tasks; independently untrusted co-hosted agents need separate execution identities/boundaries. Local disablement must remain effective while a JWT is still cryptographically valid.

## Acceptance and Remaining Gates

Record observed bootstrap, issued/validated credentials, denial cases, rotation/restart, bounded provider failure and binding rejection with redacted evidence. Separate provider behavior from application adapter behavior. Keep unsupported host isolation, OIDC login, production persistence/migration, revocation races and full authority enforcement open. Revisit provider/SDK choices on unsupported deployment cost, advisories or failed validation/attestation requirements.

Primary sources: [SPIRE release](https://github.com/spiffe/spire/releases/tag/v1.15.3), [native quickstart](https://spiffe.io/docs/latest/try/getting-started-linux-macos-x/), [pinned Unix attestor](https://github.com/spiffe/spire/blob/v1.15.3/doc/plugin_agent_workloadattestor_unix.md), [server configuration/administration](https://github.com/spiffe/spire/blob/v1.15.3/doc/spire_server.md), [SPIFFE Workload API](https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/), [JWT-SVID specification](https://spiffe.io/docs/latest/spiffe-specs/jwt-svid/), [py-spiffe](https://github.com/HewlettPackard/py-spiffe). Pinned source and executed observations determine claims.

## Observed Results

**33 cases passed** against real server/agent processes on macOS arm64, Python 3.12.0, upstream SPIRE commit `2f7861ae3923caf1f57eb087fc2928d58c0fb1d2` built with Go 1.26.6, and py-spiffe 0.3.1. The binaries report `1.15.3-dev-unk` because this is an unmodified source build, not an upstream release binary; embedded VCS metadata verifies the exact release commit and clean tracked source. [Evidence](spire-fit-evidence-2026-09-23.json) binds versions, build/source hashes and individual case outcomes. [Reproduction](../../experiments/spire/README.md) uses `make spire-fit` after the pinned native build.

| Boundary | Measured result |
|---|---|
| Bootstrap | Explicit trusted bundle and private join-token file attest the node; Unix selectors obtain a real workload JWT. An unrelated root certificate is rejected and the consumed token cannot bootstrap a second agent. |
| Workload selectors | A registration for an unmatched UID cannot be fetched by choosing its SPIFFE ID. A second process under the matching UID can obtain both identities registered for that UID. |
| Proof validation | Wrong audience, missing `exp`/`sub`/`aud`, expired or future-not-before claims, malformed/unsigned proofs and untrusted signatures are rejected. A genuinely issued short-lived credential expires through the SDK/provider path. |
| Trust domain | A fixture signing key trusted for `negative.test` cannot authenticate a subject in `cardea.test` or an unknown domain. A valid federated proof is accepted by SPIRE but rejected by Cardea's narrower allowlist. |
| Key rotation | Native prepare/activate changes the signing-key ID; old and new valid SVIDs resolve to the same application principal during overlap. This is JWT signing-key rotation, not CA rotation or key revocation. |
| Execution binding | Provisioned task/agent/supervisor/ancestor references resolve from the authenticated runtime. Unknown tasks, a different runtime and stale/expired leases are denied. Both legitimately assigned tasks remain accessible to their shared trusted runtime. |
| Local disablement | Disabling the runtime, logical agent, supervisor or ancestor in fixture state denies resolution while SPIRE still validates the credential. This tests a binding guard, not permissions or delegation authority. |
| Agent unavailable/unresponsive | The adapter returns a sanitized denial within the test's two-second bound, using a 0.5-second RPC deadline. It never falls back to local signature validation. Recovery after agent restart succeeds. |
| Server outage/restart | The running agent validates from its current bundle while the server is down. Server restart preserves registration/key state and serves fresh issuance. Server liveness is not immediate revocation. |
| Registration deletion | Once the agent observes deletion, new fetches fail; a previously issued JWT remains valid. Deletion propagation is asynchronous and not a measured production revocation SLA. |

The Workload API provider resolves ADR-001's two measured direct-validator gaps in this pinned configuration: missing expiry is rejected by the server-side validation path with a typed SDK error, and key lookup is scoped to the subject's trust domain. The SDK internally parses the token after a successful provider RPC; our adapter calls only the validated public method and uses only the SPIFFE identity. No caller custom claim becomes supervisor or task provenance.

A configured federation is not automatically accepted application identity. Keep a local allowlist and exact provisioned SPIFFE-ID mapping; then apply live task/supervisor/ancestor policy separately. Authentication does not grant even a read operation. The experiment's binding map, lease and disablement state are in memory and administratively seeded. Human authentication, persistent provenance and authenticated binding creation are **not** verified here.

Regression checks pass: 138 backend and 4 frontend tests, root lint/type checks, isolated Ruff formatting/lint and strict mypy for five experiment files, plus the pinned build script. Earlier experiment sources/evidence remain unchanged. Credentials, tokens and private keys are excluded from evidence and removed with the disposable service state.

## Integration Decision and Open Gates

Proceed with the supported provider path instead of inventing credentials or a validator. Preserve a bounded failure response, explicit endpoint/deadline, configured trust domain and exact identity mapping. Bootstrap from the host administrator's trusted boundary using an explicit bundle; the join-token experiment establishes local feasibility, not unattended production node enrollment. Select/test production selector configuration on Debian before allowing untrusted workers near the Workload API socket.

Next, design and implement the versioned persisted identity/execution-binding migration together with the DBOS shared-store migration from ADR-008, then the authenticated Unix-socket service/client. Retain legacy v0.1 records as trusted administrative history; never infer supervisor provenance from their owner labels. Establish an authenticated supervisor at admission and require live authority decisions before exposed task operations. Follow R06's maintained-policy evaluation before implementing that enforcement.

Still open: OIDC human login/discovery/session lifecycle, durable disablement/provenance, policy freshness and transitive authority/revocation races, production bootstrap/selector provisioning, coordinated upgrade/backup/restore, reboot and Debian host isolation. X.509/CA rotation, compromised-key revocation and untrusted same-UID isolation are not established. The two-process identity-sharing observation specifically rules out treating Unix UID or a shared runtime SVID as a logical-agent sandbox. M1/R01 are not complete.
