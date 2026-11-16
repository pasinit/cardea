"""A throwaway OIDC issuer on a random loopback port, for one acceptance run.

It serves what the kernel's OIDC path (`cardea.service.auth`) uses:

- `/.well-known/openid-configuration`, whose endpoints all share the issuer's origin;
- `/jwks`, the public half of an RSA key generated per run;
- `/introspect`, RFC 7662 token introspection behind HTTP Basic client authentication.

`mint()` signs an access token (a JWT, RS256, with `joserfc`, which the kernel's pinned
`authlib` already depends on) for a test human. There is no login: the script mints the
token itself, as a user's client would after a login at a real provider. Introspection
answers `active` only for an unexpired token signed by this run's key.

The kernel accepts this plain-HTTP issuer only because its configuration sets
`allow_loopback_http`, which is limited to 127.0.0.1 and ::1.
"""

import base64
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

SCOPE = "openid profile"


class Issuer:
    def __init__(self, audience: str = "cardea-kernel", client_id: str = "cardea-kernel") -> None:
        from joserfc import jwk

        self.audience = audience
        self.client_id = client_id
        self.client_secret = secrets.token_urlsafe(32)
        self.key = jwk.RSAKey.generate_key(2048, parameters={"kid": "acceptance-1"})
        self.introspections = 0
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.url,
            "authorization_endpoint": f"{self.url}/authorize",
            "token_endpoint": f"{self.url}/token",
            "jwks_uri": f"{self.url}/jwks",
            "introspection_endpoint": f"{self.url}/introspect",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
        }

    def mint(
        self,
        subject: str,
        *,
        seconds: int = 300,
        audience: str | None = None,
        scope: str = SCOPE,
    ) -> str:
        from joserfc import jwt

        at = int(time.time())
        claims = {
            "iss": self.url,
            "sub": subject,
            "aud": audience or self.audience,
            "iat": at,
            "exp": at + seconds,
            "scope": scope,
            "client_id": self.client_id,
        }
        return str(jwt.encode({"alg": "RS256", "kid": "acceptance-1"}, claims, self.key))

    def introspect(self, token: str) -> dict[str, Any]:
        from joserfc import jwt

        self.introspections += 1
        try:
            claims = dict(jwt.decode(token, self.key, algorithms=["RS256"]).claims)
        except Exception:
            return {"active": False}
        if claims.get("iss") != self.url or float(claims.get("exp", 0)) <= time.time():
            return {"active": False}
        return {**claims, "active": True, "token_type": "Bearer"}

    def _client_ok(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, self.client_id) and hmac.compare_digest(
            password, self.client_secret
        )

    def _form_client_ok(self, form: dict[str, list[str]]) -> bool:
        """`client_secret_post`: the client's credentials in the form instead."""
        user = (form.get("client_id") or [""])[0]
        password = (form.get("client_secret") or [""])[0]
        return bool(user and password) and (
            hmac.compare_digest(user, self.client_id)
            and hmac.compare_digest(password, self.client_secret)
        )

    def __enter__(self) -> "Issuer":
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, document: dict[str, Any]) -> None:
                data = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    self._json(200, issuer.metadata())
                elif self.path == "/jwks":
                    self._json(200, {"keys": [issuer.key.as_dict(private=False)]})
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                form = parse_qs(self.rfile.read(length).decode())
                if self.path != "/introspect":
                    self._json(400, {"error": "unsupported_grant_type"})
                elif not (
                    issuer._client_ok(self.headers.get("authorization"))
                    or issuer._form_client_ok(form)
                ):
                    self._json(401, {"error": "invalid_client"})
                else:
                    self._json(200, issuer.introspect((form.get("token") or [""])[0]))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
