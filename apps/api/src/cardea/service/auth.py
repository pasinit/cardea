"""Standard provider validation and exact provisioned identity mapping."""

import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa
from authlib.integrations.httpx_client import OAuth2Client
from spiffe.spiffe_id.spiffe_id import SpiffeId
from spiffe.workloadapi.workload_api_client import WorkloadApiClient

from cardea.service.database import Fault, private_file
from cardea.service.models import ServiceConfig
from cardea.service.policy import Policy


@dataclass(frozen=True)
class Proof:
    kind: str
    issuer: str
    subject: str
    expires_at: float

    def resolve(self, conn: sa.Connection) -> str:
        if self.expires_at <= time.time():
            raise Fault("invalid_credential", "Credential has expired", 401)
        row = conn.exec_driver_sql(
            "SELECT principal_id FROM principals WHERE auth_kind=? AND issuer=? AND subject=?",
            (self.kind, self.issuer, self.subject),
        ).first()
        if row is None:
            raise Fault("unknown_identity", "Identity is not provisioned", 401)
        principal = str(row[0])
        Policy(conn).principal(principal)
        return principal


def validate_endpoint(url: str, allow_loopback_http: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise Fault("provider_configuration", "Invalid identity provider URL", 500)
    if parsed.scheme != "https" and not (
        allow_loopback_http and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    ):
        raise Fault("provider_configuration", "Identity provider requires HTTPS", 500)


def discovery(issuer: str, *, allow_loopback_http: bool = False) -> dict[str, Any]:
    validate_endpoint(issuer, allow_loopback_http)
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
        response = client.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        response.raise_for_status()
        if len(response.content) > 262144:
            raise Fault("provider_configuration", "Provider metadata exceeds limit", 500)
        data: dict[str, Any] = response.json()
    if data.get("issuer") != issuer:
        raise Fault("provider_configuration", "Discovery issuer does not match configuration", 500)
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri", "introspection_endpoint"):
        validate_endpoint(str(data[name]), allow_loopback_http)
        # Prevent discovery from redirecting privileged client credentials to another origin.
        if urlparse(str(data[name])).netloc != urlparse(issuer).netloc:
            raise Fault(
                "provider_configuration", "Provider endpoints must share the configured origin", 500
            )
    return data


class IdentityProvider(Protocol):
    def validate(self, token: str, kind: str = "spiffe") -> Proof: ...
    def runtime_token(self) -> str: ...
    def runtime_proof(self) -> Proof: ...
    def close(self) -> None: ...


class Authentication:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.workload = WorkloadApiClient(
            socket_path=config.spiffe_socket, default_timeout=config.rpc_timeout
        )
        self.oidc_metadata: dict[str, Any] | None = None
        if config.oidc:
            self.oidc_metadata = discovery(
                config.oidc.issuer, allow_loopback_http=config.oidc.allow_loopback_http
            )

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if not token or len(token) > 16384:
            raise Fault("invalid_credential", "Credential is missing or exceeds limit", 401)
        try:
            if kind == "spiffe":
                svid = self.workload.validate_jwt_svid(token, self.config.audience)
                if str(svid.spiffe_id.trust_domain) != self.config.trust_domain:
                    raise ValueError("Trust domain mismatch")
                return Proof("spiffe", self.config.trust_domain, str(svid.spiffe_id), svid.expiry)
            if kind != "oidc" or not self.config.oidc or not self.oidc_metadata:
                raise ValueError("Unsupported authentication method")
            config = self.config.oidc
            secret = private_file(config.client_secret_file).decode().strip()
            with OAuth2Client(
                client_id=config.client_id,
                client_secret=secret,
                timeout=self.config.rpc_timeout,
                trust_env=False,
            ) as client:
                response = client.introspect_token(
                    self.oidc_metadata["introspection_endpoint"],
                    token=token,
                    token_type_hint="access_token",
                )
                response.raise_for_status()
                if len(response.content) > 65536:
                    raise ValueError("Introspection response exceeds limit")
                claims = response.json()
            audience = claims.get("aud", [])
            if isinstance(audience, str):
                audience = [audience]
            if (
                claims.get("active") is not True
                or claims.get("iss") != config.issuer
                or config.audience not in audience
                or claims.get("token_type", "").lower() != "bearer"
                or "openid" not in claims.get("scope", "").split()
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"]
                or type(claims.get("exp")) not in {int, float}
                or claims["exp"] <= time.time()
            ):
                raise ValueError("Invalid access token")
            return Proof("oidc", config.issuer, claims["sub"], float(claims["exp"]))
        except Exception:
            raise Fault(
                "invalid_credential", "Identity proof could not be validated", 401
            ) from None

    def runtime_token(self) -> str:
        """Fresh provider credential for the installed local peer, never persisted."""
        try:
            svid = self.workload.fetch_jwt_svid(
                {self.config.audience}, SpiffeId(self.config.runtime_spiffe_id)
            )
            self.validate(svid.token)
            return str(svid.token)
        except Exception:
            raise Fault(
                "runtime_identity_unavailable", "Cannot authenticate peer call", 503
            ) from None

    def runtime_proof(self) -> Proof:
        try:
            svid = self.workload.fetch_jwt_svid(
                {self.config.audience}, SpiffeId(self.config.runtime_spiffe_id)
            )
            proof = self.validate(svid.token)
            if proof.subject != self.config.runtime_spiffe_id:
                raise ValueError("Runtime identity mismatch")
            return proof
        except Exception:
            raise Fault(
                "runtime_identity_unavailable", "Runtime identity cannot be established", 503
            ) from None

    def close(self) -> None:
        self.workload.close()
