"""UNENFORCED development identity for `make try`: HMAC tokens shaped like SPIFFE proofs.

Lives outside the shipped package (packaging copies only apps/api/src) and is reachable
only through this directory's launcher, which injects it into `serve()`. The installed
`cardea serve` command has no way to select it. Anyone who can read the store's key file
can act as any provisioned principal: this proves nothing about who is calling.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from cardea.service.auth import Proof
from cardea.service.database import Fault, private_file
from cardea.service.models import ServiceConfig

KEY_FILE = "dev-identity.key"
TOKEN_SECONDS = 300


def check(config: ServiceConfig) -> None:
    if not config.trust_domain.endswith(".invalid"):
        raise Fault("unsafe_development", "Use a reserved *.invalid trust domain", 500)
    if config.host_execution or config.oidc:
        raise Fault("unsafe_development", "Development identity cannot enable host/agent/OIDC", 500)
    if os.geteuid() == 0:
        raise Fault("unsafe_development", "Development identity cannot run as root", 500)


def key(data_dir: Path) -> bytes:
    path = data_dir / KEY_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return private_file(path)
    with os.fdopen(fd, "wb") as stream:
        stream.write(os.urandom(32))
    return private_file(path)


def sign(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def issue(config: ServiceConfig, subject: str, seconds: int = TOKEN_SECONDS) -> str:
    check(config)
    claims = {"sub": subject, "aud": config.audience, "exp": time.time() + seconds}
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode())
    return body.decode() + "." + sign(key(config.data_dir), body)


class DevAuthentication:
    def __init__(self, config: ServiceConfig) -> None:
        check(config)
        self.config = config
        self.secret = key(config.data_dir)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        try:
            if kind != "spiffe" or not token or len(token) > 4096:
                raise ValueError("Unsupported credential")
            body, signature = token.encode().split(b".", 1)
            if not hmac.compare_digest(sign(self.secret, body), signature.decode()):
                raise ValueError("Invalid signature")
            claims = json.loads(base64.urlsafe_b64decode(body))
            subject, expiry = str(claims["sub"]), float(claims["exp"])
            if claims["aud"] != self.config.audience or expiry <= time.time():
                raise ValueError("Wrong audience or expired")
            if not subject.startswith(f"spiffe://{self.config.trust_domain}/"):
                raise ValueError("Trust domain mismatch")
        except Exception:
            raise Fault(
                "invalid_credential", "Identity proof could not be validated", 401
            ) from None
        return Proof("spiffe", self.config.trust_domain, subject, expiry)

    def runtime_token(self) -> str:
        return issue(self.config, self.config.runtime_spiffe_id)

    def runtime_proof(self) -> Proof:
        return self.validate(self.runtime_token())

    def close(self) -> None:
        pass
