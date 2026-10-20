# ADR-025: Remote Kernel API over TLS

- **Date / owner / milestones:** 2026-10-15; service and authentication maintainers; ADR-022 gap #9 (remote API). Affects `service/server.py`, `service/client.py`, `service/models.py` and the `cardea` CLI.
- **Status:** selected and implemented: an optional in-process TLS 1.3 listener. Portable tests pass on macOS (real uvicorn sockets, generated test CA). Not yet run: a Debian deployment test with SPIRE-issued certificates, and a Telos client in a real private network.
- **Requirement:** a client on another host in the same private network can submit and inspect tasks, list operations, and read receipts and objects. It uses the same API, bearer authentication and authorization as the Unix socket. The listener is off unless configured. There is no plaintext listener. The listener applies the same HTTP limits as the socket. A peer's network address or client certificate confers no identity.

## Problem

The kernel API is served only on a private `0600` Unix socket (`service/server.py`). Authentication already uses standard bearer proofs per request: SPIFFE JWT-SVIDs validated through the Workload API, or OIDC access tokens checked by RFC 7662 introspection. Both are audience-bound to `ServiceConfig.audience` (`service/auth.py`). The Unix socket's file mode limits who can present a token; it adds no identity. So the gap is the transport only.

A bearer token sent over a network can be replayed by anyone who observes it, until it expires. The transport must therefore give confidentiality and server authentication, and must offer no plaintext path.

## Candidates and primary evidence

Checked 2026-10-15 against the pinned dependencies in `apps/api/pyproject.toml` (Python `ssl` on OpenSSL 3.5.7 in the dev environment; `uvicorn==0.49.0` and `httpx==0.28.1` from `uv.lock`; `spiffe==0.3.1`).

| Option | Standards and implementations | Fit |
|---|---|---|
| **A. TLS server authentication plus the existing bearer** | TLS 1.3 ([RFC 8446](https://www.rfc-editor.org/rfc/rfc8446)); bearer usage ([RFC 6750](https://www.rfc-editor.org/rfc/rfc6750) §5 requires TLS and short-lived, audience-restricted tokens); [JWT-SVID](https://github.com/spiffe/spiffe/blob/main/standards/JWT-SVID.md) (audience is mandatory; its §7 security notes warn of replay and recommend short TTLs). Implemented by Python `ssl` and uvicorn's `ssl` socket support. Python is PSF-licensed; uvicorn is BSD-3 and maintained. | Meets every requirement with no new dependency. Same process, same ASGI app, same uvicorn limits. Token TTL and audience binding are already enforced by the validator (SPIRE's default JWT-SVID TTL is 5 minutes; OIDC `exp` and `aud` are checked). Optional client-CA verification can be added with one `ssl` setting. |
| **B. Mutual TLS with X.509-SVIDs from the Workload API** | [X.509-SVID](https://github.com/spiffe/spiffe/blob/main/standards/X509-SVID.md); `spiffe==0.3.1` ships `X509Source` (`spiffe/workloadapi/x509_source.py`) and the [py-spiffe](https://github.com/HewlettPackard/py-spiffe) project has a separate `spiffe-tls` package (not installed). [spiffe-helper](https://github.com/spiffe/spiffe-helper) can write rotating SVIDs to files. | A strong transport identity, but as an *identity source* it is a second authentication path: each SPIFFE ID would need mapping to a principal, and OIDC humans have no X.509-SVID. Rotating SVIDs (1 h by default) need live context reload, which uvicorn does not do. Fit test not run. Kept as an optional *transport gate* only: `client_ca_file` may point at the SPIRE trust bundle written by spiffe-helper. The bearer remains the identity. Certificate rotation currently needs a service restart. |
| **C. TLS-terminating reverse proxy in front of the socket** | [Caddy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) (Apache-2.0) and [nginx](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) (BSD-2). Both can proxy to a Unix socket, set TLS 1.3 only, and verify client certificates. Not installed or fit-tested here. | Also meets the guarantees. It is a second process with its own body-size, header and timeout limits, which must be kept equal to the socket's limits by hand (e.g. nginx `client_max_body_size`, Caddy `request_body max_size`). It also forwards bearer tokens across a second trust boundary and adds a package to the supported Debian profile. On a 2 GB host it costs memory for no extra guarantee. |

## Decision

Select **A** with the optional client-CA gate from B. Do not deploy C by default. Option A is the simplest option that keeps every guarantee: the same app and the same `uvicorn.Config` serve both listeners, so the limits cannot drift. It adds no dependency.

1. **Configuration.** `ServiceConfig.network` is `null` by default, so no TCP socket is bound. When it is set, it requires `host`, `port`, `certificate_file` and `key_file`, and optionally `client_ca_file`. There is no field that disables TLS, and unknown fields are refused. All files must pass `private_file` (owned by the service user, no group or other bits, at most 256 KiB). An invalid certificate, key or CA stops startup with `tls_configuration`.
2. **TLS.** The server context is `PROTOCOL_TLS_SERVER` with `minimum_version = TLSv1_3`, no compression and no session tickets. With `client_ca_file`, the context sets `CERT_REQUIRED`. A client certificate only admits the connection. It never selects or changes a principal.
3. **Same limits.** `api_config()` builds one `uvicorn.Config` per listener with the same settings: keep-alive 2 s, graceful shutdown 5 s, concurrency 64, header events 32 KiB. The app's `Bounds` middleware caps requests at `max_request_bytes` and responses at 256 KiB. `max_request_bytes` is host configuration in `config.json`: default 32768, allowed 4096–1048576 (added 2026-10-20). `/v0.2/system` reports the value in force. The normalized (RFC 8785) size of one task request is a separate setting, `max_task_request_bytes`: default 16384, allowed 4096–1048576. Admission and trigger installation (provisioning and `PUT /v0.2/triggers`) enforce it with `413 request_too_large`. The request models enforce only the 1 MiB ceiling. `/v0.2/system` reports both limits. One uvicorn server runs per socket, because uvicorn applies `config.ssl` to every socket of a server. If either server stops, the other stops too. Proxy headers and websockets are off, so `X-Forwarded-*` cannot change the recorded peer.
4. **Identity.** No change: every request still needs `Authorization: Bearer …`, validated by `IdentityProvider.validate` and resolved to an exact provisioned principal. The listener address and the peer's IP grant nothing.
5. **Client.** `KernelClient` takes either a socket `Path` or an `https://host:port` URL. A URL needs an explicit CA bundle. The client context is TLS 1.3-only with hostname verification, `trust_env=False` and no redirects. `http://` URLs, URLs with user info, paths, queries or fragments are refused with `insecure_transport`. A client certificate and key are optional. The CLI adds `--url`, `--ca-bundle`, `--client-cert` and `--client-key`; `--url` and `--socket` are mutually exclusive.

**Bearer replay.** TLS 1.3 protects tokens in transit, and there is no plaintext listener to downgrade to. Audience binding stops a token minted for another service being replayed here. A short TTL bounds the window for a token leaked at an endpoint. Certificate-bound tokens ([RFC 8705](https://www.rfc-editor.org/rfc/rfc8705)) would close the remaining window, but JWT-SVIDs do not support them. This is a revisit trigger, not a current claim.

## Integration

- **Authority:** unchanged. The authority ceiling, policy checks and principal resolution are the same code for both transports.
- **State:** none added. The listener is stateless transport; the single-owner store lock and DBOS ownership are unchanged.
- **Failure:** a missing or unsafe certificate fails startup before the daemon binds the network port. A port conflict fails startup. The socket is closed on exit, and the existing `os._exit` path still ends the process.
- **Compatibility:** existing configurations have no `network` key and behave as before. `KernelClient(path, credentials)` and `for_workload(path, …)` keep their positional signatures.
- **Operations:** the host owner issues the server certificate from a private CA (or SPIRE through spiffe-helper), and should bind to the private-network address, not `0.0.0.0`. Firewall rules remain recommended but are not part of the guarantee. Example:

```json
"network": {
  "host": "10.0.0.5",
  "port": 8443,
  "certificate_file": "/etc/cardea/tls/server.pem",
  "key_file": "/etc/cardea/tls/server.key",
  "client_ca_file": "/etc/cardea/tls/clients-ca.pem"
}
```

```sh
cardea inspect --url https://10.0.0.5:8443 --ca-bundle ca.pem \
  --spiffe-socket unix:///run/spire/agent.sock --spiffe-id spiffe://example.org/telos task-…
```

- **Dependencies:** none at runtime. `cryptography==50.0.1` (already locked, transitively) is pinned as a dev dependency to generate the test CA.

## Verification

`apps/api/tests/service/test_network.py` runs the real `create_app` on a Unix socket and a TLS socket in one uvicorn process, with fake provider proofs in the existing test style:

- listener off by default; a network block without certificate files, or with a `tls` flag, is refused;
- the server context is TLS 1.3-only; a non-private key fails with `unsafe_file`, and a mismatched key fails with `tls_configuration`;
- plaintext HTTP to the TLS port fails; a TLS 1.2-only client fails the handshake;
- an authenticated request lists operations; a missing or unknown bearer gets `401`, and `X-Forwarded-For` changes nothing;
- a 40 000-byte body gets the same `413 request_too_large` body on the socket and on TLS;
- `KernelClient` submits and inspects over `https://`; the socket client sees the same task; an untrusted CA fails; `http://` and ambiguous URLs are refused;
- with mutual TLS, a client without a certificate fails, and a client with a valid certificate but no bearer gets `401`.

Environment: macOS, Python 3.13 venv, OpenSSL 3.5.7. Not run: Debian host acceptance, SPIRE-issued X.509-SVIDs, a cross-host network test.

## Revisit triggers

- SPIRE X.509-SVIDs become the deployed server or client certificates: add live context reload from `X509Source` or spiffe-helper signals instead of restarting.
- Cardea needs to accept public-internet clients, rate limiting, or many listeners: re-evaluate a reverse proxy (option C).
- A token profile with proof of possession (RFC 8705 or DPoP, [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449)) becomes available for the identity providers in use.
