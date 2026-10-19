"""R01: real signature/claim checks plus explicit upstream integration gaps.

Ephemeral fictional keys only. No SPIRE server, login flow, or task policy is implied.
"""

import time
from typing import Any

import jwt
import pytest
from authlib.oidc.core import CodeIDToken
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as jose_jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey
from spiffe.bundle.jwt_bundle.jwt_bundle import JwtBundle
from spiffe.spiffe_id.spiffe_id import SpiffeId, SpiffeIdError, TrustDomain
from spiffe.svid.errors import InvalidTokenError
from spiffe.svid.jwt_svid import JwtSvid

AUDIENCE = "cardea-kernel-fit"
SUBJECT = "spiffe://cardea.example/agent/indexer"
ISSUER = "https://identity.example/realms/cardea"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def svid_token(key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    claims = {"sub": SUBJECT, "aud": [AUDIENCE], "exp": int(time.time()) + 300}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture-key"})


def bundle(key: rsa.RSAPrivateKey) -> JwtBundle:
    return JwtBundle(TrustDomain("cardea.example"), {"fixture-key": key.public_key()})


def test_svid_rotation_preserves_identity(signing_key: rsa.RSAPrivateKey) -> None:
    first = JwtSvid.parse_and_validate(svid_token(signing_key), bundle(signing_key), {AUDIENCE})
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second = JwtSvid.parse_and_validate(svid_token(rotated), bundle(rotated), {AUDIENCE})
    assert str(first.spiffe_id) == str(second.spiffe_id) == SUBJECT
    assert first.token != second.token


@pytest.mark.parametrize("changes", [{"aud": ["another-service"]}, {"exp": 1}])
def test_svid_rejects_wrong_audience_and_expiry(
    signing_key: rsa.RSAPrivateKey, changes: dict[str, Any]
) -> None:
    with pytest.raises(InvalidTokenError):
        JwtSvid.parse_and_validate(
            svid_token(signing_key, **changes), bundle(signing_key), {AUDIENCE}
        )


def test_svid_rejects_untrusted_signature(signing_key: rsa.RSAPrivateKey) -> None:
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidTokenError):
        JwtSvid.parse_and_validate(svid_token(attacker), bundle(signing_key), {AUDIENCE})


@pytest.mark.parametrize(
    "value", ["https://cardea.example/agent/a", "spiffe://cardea.example/a?role=admin"]
)
def test_sdk_rejects_non_spiffe_identity(value: str) -> None:
    with pytest.raises(SpiffeIdError):
        SpiffeId(value)


def test_direct_svid_validator_does_not_enforce_bundle_trust_domain(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    # Measured limitation, NOT desired authentication behavior. Never expose this
    # low-level API as the complete kernel authenticator (see ADR-001).
    result = JwtSvid.parse_and_validate(
        svid_token(signing_key, sub="spiffe://other.example/agent/a"),
        bundle(signing_key),
        {AUDIENCE},
    )
    assert result.spiffe_id.trust_domain != bundle(signing_key).trust_domain


def test_direct_svid_validator_missing_expiry_is_not_a_typed_rejection(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    token = jwt.encode(
        {"sub": SUBJECT, "aud": [AUDIENCE]},
        signing_key,
        algorithm="RS256",
        headers={"kid": "fixture-key"},
    )
    # Another measured adapter requirement, not an accepted kernel failure mode.
    with pytest.raises(KeyError, match="exp"):
        JwtSvid.parse_and_validate(token, bundle(signing_key), {AUDIENCE})


@pytest.fixture(scope="module")
def oidc_key() -> RSAKey:
    return RSAKey.generate_key(2048)


def oidc_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "human-17",
        "aud": "cardea-supervisor",
        "iat": now - 1,
        "exp": now + 300,
        "nonce": "fixture-login-nonce",
    }
    claims.update(overrides)
    return claims


def validate_oidc(token: str, key: RSAKey) -> CodeIDToken:
    # Signature first, then Authlib's OIDC-specific claim/nonce checks. This is a
    # relying-party validation probe, NOT an ID-token-as-API-token scheme.
    decoded = jose_jwt.decode(token, key, algorithms=["RS256"])
    claims = CodeIDToken(
        decoded.claims,
        decoded.header,
        options={
            "iss": {"essential": True, "value": ISSUER},
            "aud": {"essential": True, "value": "cardea-supervisor"},
        },
        params={"client_id": "cardea-supervisor", "nonce": "fixture-login-nonce"},
    )
    claims.validate()
    return claims


def test_oidc_identity_is_issuer_and_subject(oidc_key: RSAKey) -> None:
    token = jose_jwt.encode({"alg": "RS256"}, oidc_claims(), oidc_key)
    claims = validate_oidc(token, oidc_key)
    assert (claims["iss"], claims["sub"]) == (ISSUER, "human-17")


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://another.example"},
        {"aud": "another-client"},
        {"exp": 1},
        {"nonce": "another-login"},
    ],
)
def test_oidc_rejects_invalid_context(oidc_key: RSAKey, changes: dict[str, Any]) -> None:
    token = jose_jwt.encode({"alg": "RS256"}, oidc_claims(**changes), oidc_key)
    with pytest.raises(JoseError):
        validate_oidc(token, oidc_key)


def test_oidc_rejects_wrong_signature(oidc_key: RSAKey) -> None:
    other_key = RSAKey.generate_key(2048)
    token = jose_jwt.encode({"alg": "RS256"}, oidc_claims(), other_key)
    with pytest.raises(JoseError):
        validate_oidc(token, oidc_key)
