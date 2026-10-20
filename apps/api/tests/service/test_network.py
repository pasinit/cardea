"""ADR-025 TLS network listener: real uvicorn sockets, a test CA and fake provider proofs."""

import ipaddress
import os
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from cardea.service import kernel as handlers
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import Database, Fault
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    NetworkListener,
    Permission,
    Principal,
    Provisioning,
    ServiceConfig,
    TaskRequest,
    now,
)
from cardea.service.server import bind_network, serve_listeners, tls_context


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token != "supervisor":
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


def credentials() -> tuple[str, str]:
    return "spiffe", "supervisor"


# Test PKI


def private(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return path


@dataclass
class Pki:
    ca: Path
    cert: Path
    key: Path
    client_cert: Path
    client_key: Path
    other_ca: Path


def issue(
    directory: Path,
    name: str,
    issuer: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None,
    usage: x509.ObjectIdentifier | None,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signer_cert, signer_key = issuer or (None, key)
    start = datetime.now(UTC) - timedelta(minutes=1)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer_cert.subject if signer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=usage is None, path_length=None), critical=True)
    )
    if usage is None:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        ).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
    else:
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        assert signer_cert is not None
        issuer_key = signer_cert.public_key()
        assert isinstance(issuer_key, ec.EllipticCurvePublicKey)
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key), False
        )
    cert = builder.sign(signer_key, hashes.SHA256())
    cert_path = private(directory / f"{name}.pem", cert.public_bytes(serialization.Encoding.PEM))
    key_path = private(
        directory / f"{name}.key",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return cert, key, cert_path, key_path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("pki")
    ca_cert, ca_key, ca, _ = issue(directory, "ca", None, None)
    _, _, cert, key = issue(directory, "server", (ca_cert, ca_key), ExtendedKeyUsageOID.SERVER_AUTH)
    _, _, client_cert, client_key = issue(
        directory, "client", (ca_cert, ca_key), ExtendedKeyUsageOID.CLIENT_AUTH
    )
    _, _, other_ca, _ = issue(directory, "other-ca", None, None)
    return Pki(ca, cert, key, client_cert, client_key, other_ca)


def listener_config(pki: Pki, mutual: bool = False) -> NetworkListener:
    return NetworkListener(
        host="127.0.0.1",
        port=8443,
        certificate_file=pki.cert,
        key_file=pki.key,
        client_ca_file=pki.ca if mutual else None,
    )


# Kernel and running listeners


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    db = Database(tmp_path / "data")
    db.migrate()
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in RIGHTS
    )
    principals = (
        *(
            Principal(principal_id=p, kind=kind, auth_kind="spiffe", issuer="test", subject=p)
            for p, kind in cast(
                tuple[tuple[str, Literal["service", "human"]], ...],
                (("runtime", "service"), ("supervisor", "human")),
            )
        ),
        Principal(principal_id="agent", kind="agent", runtime_id="runtime"),
    )
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
    )
    instance = Kernel(db, cast(Authentication, Auth()))
    try:
        yield instance
    finally:
        db.close()


@dataclass
class Running:
    socket_path: Path
    url: str


def start(
    kernel: Kernel, network: NetworkListener, max_request_bytes: int = 32768
) -> Iterator[Running]:
    # /tmp keeps the macOS sockaddr_un path below its length limit.
    with tempfile.TemporaryDirectory(prefix="cardea-net-", dir="/tmp") as directory:
        path = Path(directory) / "kernel.sock"
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.bind(str(path))
        unix.listen(16)
        tcp = bind_network("127.0.0.1", 0)
        url = f"https://127.0.0.1:{tcp.getsockname()[1]}"
        stop = threading.Event()
        thread = threading.Thread(
            target=serve_listeners,
            args=(
                create_app(kernel, max_request_bytes),
                [(unix, None), (tcp, tls_context(network))],
                stop,
            ),
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    socket.create_connection(tcp.getsockname()[:2], timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("TLS listener did not start")
                    time.sleep(0.05)
            yield Running(path, url)
        finally:
            stop.set()
            thread.join(timeout=10)
            unix.close()
            tcp.close()
            assert not thread.is_alive()


@pytest.fixture
def running(kernel: Kernel, pki: Pki) -> Iterator[Running]:
    yield from start(kernel, listener_config(pki))


@pytest.fixture
def mutual(kernel: Kernel, pki: Pki) -> Iterator[Running]:
    yield from start(kernel, listener_config(pki, mutual=True))


def tls13(pki: Pki, cert: bool = False) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(pki.ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if cert:
        context.load_cert_chain(pki.client_cert, pki.client_key)
    return context


# Configuration


def test_network_listener_is_off_by_default_and_has_no_plaintext_form(pki: Pki) -> None:
    base = {
        "data_dir": "/var/lib/cardea",
        "spiffe_socket": "unix:///run/spire/agent.sock",
        "trust_domain": "example.org",
        "runtime_spiffe_id": "spiffe://example.org/runtime",
    }
    assert ServiceConfig.model_validate(base).network is None
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate({**base, "network": {"host": "10.0.0.2", "port": 8443}})
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate(
            {**base, "network": {**listener_config(pki).model_dump(), "tls": False}}
        )
    configured = ServiceConfig.model_validate(
        {**base, "network": listener_config(pki).model_dump(mode="json")}
    )
    assert configured.network is not None and configured.network.client_ca_file is None


def test_tls_context_is_tls13_only_and_requires_private_files(pki: Pki, tmp_path: Path) -> None:
    context = tls_context(listener_config(pki))
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.verify_mode is ssl.CERT_NONE
    assert tls_context(listener_config(pki, mutual=True)).verify_mode is ssl.CERT_REQUIRED
    loose = tmp_path / "loose.key"
    loose.write_bytes(pki.key.read_bytes())
    os.chmod(loose, 0o644)
    with pytest.raises(Fault) as unsafe:
        tls_context(listener_config(pki).model_copy(update={"key_file": loose}))
    assert unsafe.value.code == "unsafe_file"
    mismatched = listener_config(pki).model_copy(update={"key_file": pki.client_key})
    with pytest.raises(Fault) as invalid:
        tls_context(mismatched)
    assert invalid.value.code == "tls_configuration"


# Live listener


def test_authenticated_request_succeeds_and_unauthenticated_is_rejected(
    running: Running, pki: Pki
) -> None:
    with httpx.Client(base_url=running.url, verify=tls13(pki), trust_env=False) as http:
        anonymous = http.get("/v0.2/system")
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "invalid_credential"
        forged = http.get(
            "/v0.2/system",
            headers={"Authorization": "Bearer outsider", "X-Forwarded-For": "127.0.0.1"},
        )
        assert forged.status_code == 401
        listed = http.get(
            "/v0.2/operations",
            params={"namespace": "test"},
            headers={"Authorization": "Bearer supervisor"},
        )
        assert listed.status_code == 200
        assert "tasks.submit" in listed.json()["operations"]


def test_plaintext_and_older_tls_are_refused(running: Running, pki: Pki) -> None:
    plain = running.url.replace("https://", "http://")
    with (
        httpx.Client(trust_env=False, timeout=3) as http,
        pytest.raises(httpx.HTTPError),
    ):
        http.get(plain + "/v0.2/system", headers={"Authorization": "Bearer supervisor"})
    old = ssl.create_default_context(cafile=str(pki.ca))
    old.maximum_version = ssl.TLSVersion.TLSv1_2
    with (
        httpx.Client(verify=old, trust_env=False, timeout=3) as http,
        pytest.raises(httpx.ConnectError),
    ):
        http.get(running.url + "/v0.2/system")


def test_oversized_request_is_rejected_as_on_the_socket(running: Running, pki: Pki) -> None:
    body = b'{"x":"' + b"a" * 40000 + b'"}'
    headers = {"Authorization": "Bearer supervisor", "Content-Type": "application/json"}
    responses = []
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(running.socket_path)),
        base_url="http://cardea",
        trust_env=False,
    ) as local:
        responses.append(local.post("/v0.2/tasks", content=body, headers=headers))
    with httpx.Client(base_url=running.url, verify=tls13(pki), trust_env=False) as remote:
        responses.append(remote.post("/v0.2/tasks", content=body, headers=headers))
    assert [r.status_code for r in responses] == [413, 413]
    assert responses[0].json() == responses[1].json()
    assert responses[1].json()["code"] == "request_too_large"


def test_request_cap_is_host_configuration() -> None:
    base = {
        "data_dir": "/var/lib/cardea",
        "spiffe_socket": "unix:///run/spire/agent.sock",
        "trust_domain": "cardea.internal",
        "runtime_spiffe_id": "spiffe://cardea.internal/runtime/kernel",
    }
    assert ServiceConfig.model_validate(base).max_request_bytes == 32768
    assert ServiceConfig.model_validate({**base, "max_request_bytes": 131072}).max_request_bytes
    for value in (4095, 1048577, "65536"):
        with pytest.raises(ValidationError):
            ServiceConfig.model_validate({**base, "max_request_bytes": value})
    assert ServiceConfig.model_validate(base).max_task_request_bytes == 16384
    assert ServiceConfig.model_validate({**base, "max_task_request_bytes": 65536})
    for value in (4095, 1048577, "65536"):
        with pytest.raises(ValidationError):
            ServiceConfig.model_validate({**base, "max_task_request_bytes": value})


def test_configured_request_cap_applies_to_every_listener(kernel: Kernel, pki: Pki) -> None:
    headers = {"Authorization": "Bearer supervisor", "Content-Type": "application/json"}
    kernel.max_task_request_bytes = 49152
    for running in start(kernel, listener_config(pki), max_request_bytes=65536):
        with (
            httpx.Client(
                transport=httpx.HTTPTransport(uds=str(running.socket_path)),
                base_url="http://cardea",
                trust_env=False,
            ) as local,
            httpx.Client(base_url=running.url, verify=tls13(pki), trust_env=False) as remote,
        ):
            for http in (local, remote):
                system = http.get("/v0.2/system", headers=headers).json()
                assert system["max_request_bytes"] == 65536
                assert system["max_task_request_bytes"] == 49152
                assert system["max_response_bytes"] == 262144
                # Above the default cap but within the configured one: reaches validation.
                within = b'{"x":"' + b"a" * 40000 + b'"}'
                assert http.post("/v0.2/tasks", content=within, headers=headers).status_code == 422
                above = b'{"x":"' + b"a" * 70000 + b'"}'
                refused = http.post("/v0.2/tasks", content=above, headers=headers)
                assert refused.status_code == 413
                assert refused.json()["message"] == "Request exceeds 65536 bytes"


def test_kernel_client_works_against_https_url(running: Running, pki: Pki) -> None:
    client = KernelClient(running.url, credentials, ca_bundle=pki.ca)
    try:
        view = client.submit(
            TaskRequest(
                request_id="remote-one",
                agent_id="agent",
                namespace="test",
                objective="Submit over the network listener",
                payload={"n": 1},
            )
        )
        assert view.supervisor_id == "supervisor"
        assert client.inspect(view.task.task_id).task.task_id == view.task.task_id
    finally:
        client.close()
    local = KernelClient(running.socket_path, credentials)
    try:
        assert local.inspect(view.task.task_id).request_digest == view.request_digest
    finally:
        local.close()
    untrusted = KernelClient(running.url, credentials, ca_bundle=pki.other_ca)
    try:
        with pytest.raises(httpx.ConnectError):
            untrusted.inspect(view.task.task_id)
    finally:
        untrusted.close()
    anonymous = KernelClient(running.url, lambda: ("spiffe", "outsider"), ca_bundle=pki.ca)
    try:
        with pytest.raises(ClientError) as denied:
            anonymous.inspect(view.task.task_id)
        assert denied.value.status == 401
    finally:
        anonymous.close()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8443", "https://user:pw@127.0.0.1", "https://host/v0.2", "ftp://h"]
)
def test_kernel_client_refuses_non_tls_or_ambiguous_urls(url: str, pki: Pki) -> None:
    with pytest.raises(ClientError) as refused:
        KernelClient(url, credentials, ca_bundle=pki.ca)
    assert refused.value.code == "insecure_transport"
    with pytest.raises(ClientError) as missing:
        KernelClient("https://127.0.0.1:8443", credentials)
    assert missing.value.code == "ca_bundle_required"


def test_mutual_tls_gates_transport_but_never_replaces_bearer(mutual: Running, pki: Pki) -> None:
    with (
        httpx.Client(verify=tls13(pki), trust_env=False, timeout=3) as http,
        pytest.raises(httpx.HTTPError),
    ):
        http.get(mutual.url + "/v0.2/system", headers={"Authorization": "Bearer supervisor"})
    with httpx.Client(base_url=mutual.url, verify=tls13(pki, cert=True), trust_env=False) as http:
        assert http.get("/v0.2/system").status_code == 401
    client = KernelClient(
        mutual.url, credentials, ca_bundle=pki.ca, client_cert=(pki.client_cert, pki.client_key)
    )
    try:
        assert client.request("GET", "/system")["max_request_bytes"] == 32768
    finally:
        client.close()
