# ADR-001 — M0 Identity, Wire, and Digest Contracts

Date: 2026-09-22. Owner: kernel/API. Scope: R01–R03, M0 selections and portable fit evidence.

**Status: selected; local fit evaluation complete.** The authenticated service and versioned migrations remain M1 work; complete authority enforcement remains M2. New libraries are pinned development dependencies, not production authenticators. This closes M0's reopened decision gate without claiming an external API has shipped.

## Requirements

Reuse identity proof validation and transport/schema implementations. Distinguish issuer-scoped humans from workloads, bind authenticated execution to a task, and persist its accountable supervisor. Effective task authority must remain within the supervisor's current permissions and delegation authority, every ancestor's effective authority, and platform policy. Recheck live authority independently of credential validity. Transport must support a private Unix socket, bounded inputs, typed errors, and authentication hooks. Hash changes must never reinterpret existing receipts.

## Candidates and Fit

| Decision | Evaluated choice | Evidence and limits |
|---|---|---|
| R01 workload identity | SPIFFE JWT-SVID; `spiffe==0.3.1`, Apache-2.0 | Real signed-token checks cover rotation, audience, expiry, signature, and URI parsing. Direct validation has the gaps below. SPIRE/Workload API deployment remains untested. |
| R01 human identity | OIDC `(iss, sub)`; Authlib 1.8.0, BSD-3-Clause, with joserfc signature validation | Real signed OIDC claim tests cover issuer, audience, expiry, nonce, and signature. Login, discovery/JWKS refresh, and application sessions remain untested. |
| R02 transport | HTTP resource API, OpenAPI 3.1, Pydantic schemas; FastAPI 0.136.3/Pydantic 2.13.4 (MIT), Uvicorn 0.49.0/HTTPX 0.28.1 (BSD-3-Clause) | Real subprocess/Unix-socket calls prove schema export, authentication dependency wiring, rejection of extra identity fields, wrong types, and field length overflow. |
| R03 digests | RFC 8785 JCS via `rfc8785==0.1.4`, Apache-2.0, for the next schema version | RFC numeric vectors, UTF-16 key order, Unicode behavior, unsupported-value rejection, and a frozen v0.1 receipt establish fit and incompatibility with old digests. |

Bare PyJWT is reused transitively for signatures but does not supply workload attestation or the complete OIDC client flow. A custom issuer duplicates those responsibilities without a demonstrated gap and is rejected. SPIRE is the intended workload provider for M1 evaluation; use an existing OIDC provider, with Keycloak still only a candidate if one is needed. Provider operating cost and bootstrap are not resolved by selecting identity standards.

JSON-RPC 2.0 remains a valid alternative, not a failed implementation. HTTP/OpenAPI meets the measured transport needs using the existing stack; no current requirement for batching or notifications justifies adding RPC dispatch/envelopes. No custom wire envelope is selected. Generated client conformance and full endpoint/error schemas are M1 work.

The current encoder fails JCS compatibility: it preserves `1` versus `1.0`, uses ASCII escapes, and permits integers outside the selected JCS library's safe integer domain. Replacing it would invalidate stored receipts. Retaining it **only for v0.1** is the bounded custom exception; future formats reuse JCS.

## R01 — Identity and Validation Contract

Keep stable local principal references and provision unique typed external bindings: exact SPIFFE ID for workloads, exact `(issuer, subject)` for humans. Do not equate names or email addresses with identity. Credential rotation preserves the principal. Trusted dispatch binds a runtime, logical agent, accountable supervisor, task, parent/delegation provenance, and lease; a shared SVID does not independently authenticate hosted logical agents.

Select audience-bound JWT-SVIDs over the local socket, with validation through the SPIFFE Workload API backed by the trusted provider. The `spiffe` SDK is suitable for integration evaluation, **not approved as a complete authenticator via its direct `JwtSvid.parse_and_validate` method**. The tests measured two limitations in 0.3.1:

- A token subject in another trust domain is accepted when signed by a key in the supplied bundle; that method does not match subject and bundle trust domains.
- Missing `exp` produces `KeyError`, rather than a typed authentication rejection.

These tests document gaps, not desired kernel behavior. M1 must demonstrate provider validation, configured trust-domain allowlisting, required claims, audience, expiry, signature, rotation, and fail-closed handling of malformed proof or provider outage. Use the standard Workload API validation path first; any remaining adapter must cover only documented gaps. No bypass to insecure parsing or the low-level direct method is permitted. The current tests do not prove the Workload API path resolves these gaps.

Use Authlib for an appropriate OIDC login flow and validated application session/API credentials. OIDC ID-token checks here are a relying-party probe, not approval to accept ID tokens as API bearer tokens. Local disablement and live task/supervisor authority checks remain mandatory even with valid credentials. Requests cannot choose authoritative principals or supervisors merely by supplying identifiers.

## R02 — Wire Contract

Reuse FastAPI HTTP resource endpoints and OpenAPI 3.1/JSON Schema 2020-12 over a private Unix-domain socket, served separately from the legacy API. Use HTTPX's Unix transport for the first Python client. Keep the socket in a service-owned mode-0700 directory and enforce mode 0600; filesystem access alone is not agent authentication.

Version external routes and schemas explicitly. Reject unsupported versions and identity overrides; derive authoritative bindings from authentication and trusted admission. Keep operation IDs/receipts distinct from HTTP correlation IDs. On lost connections, inspect the receipt and revalidate authority before any new dispatch. M1 must implement total request/response limits, stable typed error mapping, version negotiation/rejection, duplicate-call semantics, and authenticated restart tests before publishing endpoints.

The `/fit/echo` application is a test fixture with a fictional constant credential that exercises a dependency hook. It is never mounted in the product and proves no identity or authorization enforcement. The former `/kernel/v1/call` sketch is superseded; legacy OpenAPI remains unchanged.

## R03 — Digest Compatibility and Migration

Keep v0.1's full normalized request hash exactly as implemented: SHA-256 of sorted-key compact ASCII JSON with finite numbers. The committed fixture freezes a literal receipt digest and verifies retry acceptance and changed-argument conflict. Never recalculate old receipts using a new algorithm or permit a retry to switch versions.

The next request version will use SHA-256 of RFC 8785 UTF-8 bytes via the selected library. Bind the explicit schema version and the entire normalized request, including authenticated identity, task, operation ID/name, target, arguments, and deadline. Persist an explicit digest-format identifier with new receipts; accept only the schema/format pairs defined by that version. Reject unknown pairs, never guess from a hash. Final fields and schema version are set in M1 before external exposure.

For the new version, integers must stay within ±(2^53−1); floats must be finite binary64 values and strings must contain valid Unicode scalar values. Reject unsupported values before acceptance or effects. JCS equates `1`, `1.0`, and equivalent numeric representations and maps negative zero to zero; it does not normalize Unicode. Exact large integers or decimals require explicitly typed string fields, never automatic lossy coercion. Old requests with such integers remain valid only under their original version.

Version 0.1 tasks lack authenticated supervisor provenance. Keep them administrative/trusted records; a migration cannot infer supervisor authority from `owner_id`. Authenticated admission requires an explicit verified binding and fresh grants bounded by live supervisor/ancestor authority. Preserve historical records rather than manufacturing provenance.

## Verification and Maintenance

Reproduce the 27 fit cases with `make standards-test` after `make install`. Tests use ephemeral fictional keys, real loopback-free Unix sockets, independent RFC byte vectors, and a frozen receipt. No real provider credentials or user data are used. On macOS arm64, Python 3.12.0:

- `make test`: 138 backend and 4 frontend tests passed, including all 27 fit cases.
- `make lint` and `make typecheck`: passed (50 Python files plus TypeScript).
- `make kernel-demo`: passed two-process checkpoint/resume with verified stored output.

The existing FastAPI/Starlette test-client deprecation warning remains. Package versions, release dates, repository activity/license observations, test hashes, and results are recorded in [the evidence snapshot](m0-fit-evidence.json); exact resolution is in [uv.lock](../../apps/api/uv.lock). The inspected upstream repositories are not archived and have recent activity. PyPI reported no advisories for the sampled pinned versions at capture time; this is not a complete dependency audit or security certification. Licenses are compatible with this evaluation; redistribution must retain required notices.

Unrun: SPIRE installation/attestation and Workload API validation, complete OIDC login/session lifecycle, live supervisor ceiling enforcement, generated client interoperability, and Debian confinement. RFC reference vectors pass in Python; independent multi-language client execution remains M1 work. Revisit choices on incompatible releases, advisories, provider/deployment constraints, or failed integration tests. R04–R12 still require their own relevant decisions.

## Primary Sources

- [SPIFFE JWT-SVID](https://spiffe.io/docs/latest/spiffe-specs/jwt-svid/), [SPIRE concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/), and [py-spiffe](https://github.com/HewlettPackard/py-spiffe).
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html) and [Authlib](https://docs.authlib.org/).
- [FastAPI OpenAPI](https://fastapi.tiangolo.com/how-to/extending-openapi/), [HTTPX Unix transport](https://www.python-httpx.org/advanced/transports/), and [JSON-RPC 2.0](https://www.jsonrpc.org/specification).
- [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) and [rfc8785.py](https://github.com/trailofbits/rfc8785.py).

## Follow-up — 2026-09-23

[ADR-009](009-spire-bootstrap-fit.md) tests real SPIRE 1.15.3 bootstrap and the selected Workload API path in 33 local cases. Provider validation rejects the missing-expiry and cross-trust-domain-key cases measured here. JWT signing-key rotation, bounded agent failure and trusted execution-binding fixtures pass; same-UID identity sharing and delayed registration revocation remain explicit limits. The historical results above are unchanged. Production provider provisioning, persisted/authenticated supervisor provenance, OIDC login and Debian confinement remain open.
