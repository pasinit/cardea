"""Authlib authorization code + PKCE and verified OIDC identity; never an API ID token."""

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from cardea.service.auth import discovery
from cardea.service.database import Fault, private_file


def store_session(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class OIDCLogin:
    def __init__(
        self, issuer: str, client_id: str, redirect_uri: str, *, loopback_test: bool = False
    ) -> None:
        self.issuer, self.client_id, self.redirect_uri = issuer, client_id, redirect_uri
        self.loopback_test = loopback_test
        self.metadata = discovery(issuer, allow_loopback_http=loopback_test)
        self.verifier, self.nonce, self.state = (secrets.token_urlsafe(32) for _ in range(3))
        self.client = OAuth2Client(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope="openid profile",
            code_challenge_method="S256",
            state=self.state,
            token_endpoint_auth_method="none",
            timeout=5,
            trust_env=False,
        )

    def start(self) -> str:
        url, _ = self.client.create_authorization_url(
            self.metadata["authorization_endpoint"],
            state=self.state,
            code_verifier=self.verifier,
            nonce=self.nonce,
        )
        return str(url)

    def complete(self, response_url: str) -> dict[str, Any]:
        try:
            token = self.client.fetch_token(
                self.metadata["token_endpoint"],
                authorization_response=response_url,
                code_verifier=self.verifier,
            )
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.get(self.metadata["jwks_uri"])
                response.raise_for_status()
                if len(response.content) > 262144:
                    raise ValueError("JWKS response exceeds limit")
                keys = KeySet.import_key_set(response.json())
            decoded = jwt.decode(token["id_token"], keys, algorithms=["RS256", "ES256"])
            claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                options={
                    "iss": {"essential": True, "value": self.issuer},
                    "aud": {"essential": True, "value": self.client_id},
                },
                params={
                    "client_id": self.client_id,
                    "nonce": self.nonce,
                    "access_token": token["access_token"],
                },
            )
            claims.validate()
            return {
                "issuer": self.issuer,
                "subject": claims["sub"],
                "client_id": self.client_id,
                "loopback_test": self.loopback_test,
                "token": dict(token),
            }
        except Exception:
            raise Fault("login_failed", "OIDC login could not be verified", 401) from None

    def close(self) -> None:
        self.client.close()


def session_credentials(path: Path) -> tuple[str, str]:
    session = json.loads(private_file(path))
    token = session["token"]
    if token.get("expires_at", 0) <= time.time() + 10:
        try:
            metadata = discovery(session["issuer"], allow_loopback_http=session["loopback_test"])
            with OAuth2Client(
                client_id=session["client_id"],
                token_endpoint_auth_method="none",
                timeout=5,
                trust_env=False,
            ) as client:
                updated = client.refresh_token(
                    metadata["token_endpoint"], refresh_token=token["refresh_token"]
                )
            session["token"] = dict(updated)
            store_session(path, session)
            token = updated
        except Exception:
            raise Fault("session_expired", "Sign in again to renew the session", 401) from None
    return "oidc", str(token["access_token"])
