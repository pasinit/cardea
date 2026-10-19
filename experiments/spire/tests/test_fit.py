"""Actual SPIRE provider calls; all identities, keys and binding state are fictional."""

import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from spiffe.errors import PySpiffeError
from spiffe.spiffe_id.spiffe_id import SpiffeId
from spiffe.workloadapi.errors import FetchJwtSvidError, ValidateJwtSvidError

from binding import AUDIENCE, DOMAIN, RUNTIME, Bindings, Denied, Execution
from lab import Lab, eventually


@pytest.fixture
def bindings(lab: Lab) -> Iterator[Bindings]:
    registry = Bindings(lab.socket)
    registry.executions["task-a"] = Execution(
        "task-a",
        "runtime-a",
        "agent-a",
        "supervisor-a",
        ("parent-agent",),
        "lease-a",
        time.time() + 60,
    )
    try:
        yield registry
    finally:
        registry.close()


def test_real_bootstrap_and_validation(
    lab: Lab, record_property: Callable[[str, object], None]
) -> None:
    assert lab.validate(lab.token()) == RUNTIME
    assert lab.root.stat().st_mode & 0o777 == 0o700
    assert (lab.root / "join-token").stat().st_mode & 0o777 == 0o600
    record_property(
        "observation", "Real node/workload attestation and audience-bound JWT validation"
    )


def test_unmatched_workload_cannot_select_identity(lab: Lab) -> None:
    with pytest.raises(FetchJwtSvidError):
        lab.token(f"spiffe://{DOMAIN}/runtime/unmatched")


def test_wrong_audience_rejected(lab: Lab) -> None:
    with pytest.raises(ValidateJwtSvidError):
        lab.validate(lab.token(audience="other-service"))


def test_federation_and_application_allowlist_are_distinct(lab: Lab, bindings: Bindings) -> None:
    token = lab.negative()
    assert lab.validate(token) == "spiffe://negative.test/runtime/a"
    with pytest.raises(Denied, match="untrusted_domain"):
        bindings.authenticate(token)


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": None},
        {"sub": None},
        {"aud": None},
        {"exp": 1},
        {"aud": ["other-service"]},
        {"sub": "spiffe://untrusted.test/runtime/a"},
        {"sub": RUNTIME},
        {"sub": "https://negative.test/runtime/a"},
        {"nbf": 4102444800},
    ],
    ids=[
        "missing-exp",
        "missing-sub",
        "missing-aud",
        "expired",
        "audience",
        "unknown-domain",
        "key-from-wrong-domain",
        "non-spiffe",
        "future-nbf",
    ],
)
def test_provider_rejects_invalid_signed_claims(lab: Lab, changes: dict[str, Any]) -> None:
    # Key is genuinely trusted only for negative.test. This avoids signature-only failures.
    assert lab.validate(lab.negative()) == "spiffe://negative.test/runtime/a"
    with pytest.raises(ValidateJwtSvidError) as rejected:
        lab.validate(lab.negative(**changes))
    if changes == {"exp": None}:
        assert "token missing exp claim" in str(rejected.value)


@pytest.mark.parametrize("kind", ["malformed", "unsigned", "wrong-signature"])
def test_invalid_proof_fails_closed(lab: Lab, bindings: Bindings, kind: str) -> None:
    if kind == "malformed":
        token = "not-a-jwt"
    else:
        claims = {
            "sub": "spiffe://negative.test/runtime/a",
            "aud": [AUDIENCE],
            "exp": int(time.time()) + 30,
        }
        token = (
            jwt.encode(claims, "", algorithm="none")
            if kind == "unsigned"
            else jwt.encode(
                claims,
                ec.generate_private_key(ec.SECP256R1()),
                algorithm="ES256",
                headers={"kid": "negative-fixture"},
            )
        )
    with pytest.raises(Denied, match="proof_validation_failed") as rejected:
        bindings.authenticate(token)
    assert "eyJ" not in str(rejected.value)


def test_live_issued_credential_expires(lab: Lab) -> None:
    # Native administrative mint allows a short expiry without changing the shared registration.
    issued = lab.command("jwt", "mint", "-spiffeID", RUNTIME, "-audience", AUDIENCE, "-ttl", "2s")
    token = issued.strip()
    assert lab.validate(token) == RUNTIME
    time.sleep(2.1)
    with pytest.raises(PySpiffeError):
        lab.validate(token)


def test_shared_uid_does_not_isolate_logical_agents(lab: Lab, bindings: Bindings) -> None:
    other = f"spiffe://{DOMAIN}/runtime/b"
    lab.register(other)
    eventually(lambda: lab.token(other))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "probe.py"), lab.socket],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert {RUNTIME, other} <= set(json.loads(result.stdout))
    # Same Unix UID can obtain both SVIDs; an unprovisioned name still has no Cardea principal.
    with pytest.raises(Denied, match="principal_unavailable"):
        bindings.authenticate(lab.token(other))
    bindings.identities[other] = "runtime-b"
    with pytest.raises(Denied, match="execution_mismatch"):
        bindings.resolve(lab.token(other), "task-a", "lease-a")


def test_binding_derives_agent_and_supervisor_from_trusted_state(
    lab: Lab, bindings: Bindings
) -> None:
    resolved = bindings.resolve(lab.token(), "task-a", "lease-a")
    assert (resolved.agent, resolved.supervisor, resolved.ancestors) == (
        "agent-a",
        "supervisor-a",
        ("parent-agent",),
    )
    # Two assignments to one trusted runtime are both usable: task IDs aren't isolation secrets.
    bindings.executions["task-b"] = replace(
        resolved, task="task-b", agent="agent-b", lease="lease-b"
    )
    assert bindings.resolve(lab.token(), "task-b", "lease-b").agent == "agent-b"


@pytest.mark.parametrize("task,lease", [("forged-task", "lease-a"), ("task-a", "stale-lease")])
def test_forged_or_stale_execution_binding(
    lab: Lab, bindings: Bindings, task: str, lease: str
) -> None:
    with pytest.raises(Denied):
        bindings.resolve(lab.token(), task, lease)


def test_expired_execution_binding(lab: Lab, bindings: Bindings) -> None:
    bindings.executions["task-a"] = replace(bindings.executions["task-a"], expires=0)
    with pytest.raises(Denied, match="stale_lease"):
        bindings.resolve(lab.token(), "task-a", "lease-a")


@pytest.mark.parametrize("principal", ["runtime-a", "agent-a", "supervisor-a", "parent-agent"])
def test_local_disablement_overrides_valid_svid(
    lab: Lab, bindings: Bindings, principal: str
) -> None:
    token = lab.token()
    assert lab.validate(token) == RUNTIME
    bindings.disabled.add(principal)
    with pytest.raises(Denied):
        bindings.resolve(token, "task-a", "lease-a")
    assert lab.validate(token) == RUNTIME


def test_signing_key_rotation_preserves_principal(lab: Lab, bindings: Bindings) -> None:
    old = lab.token()
    prepared = json.loads(lab.command("localauthority", "jwt", "prepare", "-output", "json"))
    authority = prepared["prepared_authority"]["authority_id"]
    lab.command("localauthority", "jwt", "activate", "-authorityID", authority)
    client = lab.client
    assert client is not None

    def rotated() -> str:
        svid = client.fetch_jwt_svid({AUDIENCE, "rotation-probe"}, SpiffeId(RUNTIME))
        assert jwt.get_unverified_header(svid.token)["kid"] != jwt.get_unverified_header(old)["kid"]
        assert lab.validate(svid.token) == RUNTIME
        return svid.token

    fresh = eventually(rotated)
    assert bindings.authenticate(old) == bindings.authenticate(fresh) == "runtime-a"
    lab.observations["rotation"] = "Native prepare/activate changed signing key; overlap validated"


@pytest.mark.parametrize("paused", [False, True], ids=["stopped", "unresponsive"])
def test_provider_outage_has_bounded_denial(lab: Lab, bindings: Bindings, paused: bool) -> None:
    token = lab.token()
    if paused:
        lab.agent.send_signal(signal.SIGSTOP)
    else:
        lab.stop(lab.agent)
    try:
        start = time.monotonic()
        with pytest.raises(Denied, match="proof_validation_failed"):
            bindings.authenticate(token)
        assert time.monotonic() - start < 2
    finally:
        if paused:
            lab.agent.send_signal(signal.SIGCONT)
        else:
            lab.start_agent()
        eventually(lambda: bindings.authenticate(lab.token()))


def test_server_outage_does_not_immediately_revoke_valid_svid(lab: Lab) -> None:
    token = lab.token()
    lab.stop(lab.server)
    try:
        # Agent validates locally from its current bundle; server liveness isn't revocation.
        assert lab.validate(token) == RUNTIME
    finally:
        lab.start_server()
    assert lab.validate(token) == RUNTIME
    client = lab.client
    assert client is not None

    def renewed() -> None:
        fresh = client.fetch_jwt_svid({AUDIENCE, "after-server-restart"}, SpiffeId(RUNTIME))
        assert lab.validate(fresh.token) == RUNTIME

    eventually(renewed)


def test_join_token_cannot_bootstrap_a_second_agent(lab: Lab) -> None:
    original = (lab.root / "agent.conf").read_text()
    duplicate = original.replace("agent-data", "duplicate-data").replace(
        "agent.sock", "duplicate.sock"
    )
    config = lab.write("duplicate.conf", duplicate)
    proc = lab.launch("spire-agent", config)
    log = lab.root / f"process-{len(lab.processes) - 1}.log"
    try:

        def refused() -> None:
            text = log.read_text()
            assert "join token" in text.lower() and (
                "invalid" in text.lower() or "not found" in text.lower()
            )

        eventually(refused, timeout=15)
        assert not (lab.root / "duplicate.sock").exists()
    finally:
        lab.stop(proc)


def test_registration_removal_is_not_immediate_token_revocation(lab: Lab) -> None:
    identity = f"spiffe://{DOMAIN}/runtime/removable"
    entry = lab.register(identity, ttl=60)
    token = eventually(lambda: lab.token(identity))
    lab.command("entry", "delete", "-entryID", entry)

    def denied_issuance() -> None:
        try:
            lab.token(identity)
        except FetchJwtSvidError:
            return
        raise AssertionError("Agent has not observed registration removal yet")

    eventually(denied_issuance)
    assert lab.validate(token) == identity


def test_wrong_bootstrap_trust_bundle_is_rejected(lab: Lab) -> None:
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Wrong fictional root")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(lab.signer.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(lab.signer, hashes.SHA256())
    )
    lab.write("wrong-root.pem", certificate.public_bytes(serialization.Encoding.PEM).decode())
    original = (lab.root / "agent.conf").read_text()
    wrong = (
        original.replace("agent-data", "wrong-trust-data")
        .replace("agent.sock", "wrong-trust.sock")
        .replace("bundle.pem", "wrong-root.pem")
    )
    proc = lab.launch("spire-agent", lab.write("wrong-trust.conf", wrong))
    log = lab.root / f"process-{len(lab.processes) - 1}.log"
    try:

        def rejected_trust() -> None:
            assert "certificate signed by unknown authority" in log.read_text()

        eventually(rejected_trust, timeout=15)
        assert not (lab.root / "wrong-trust.sock").exists()
    finally:
        lab.stop(proc)
