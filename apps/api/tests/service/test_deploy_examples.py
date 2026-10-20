"""The small-host deployment examples (ADR-030) stay valid against the current models."""

import configparser
import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest
from cryptography import x509

from cardea.hosts import launcher
from cardea.service.harnesses import HarnessFile
from cardea.service.models import Provisioning, ServiceConfig

KIT = Path(__file__).resolve().parents[4] / "deploy" / "small-host"


def unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    parser.read_string(path.read_text())
    return parser


def service_config(name: str) -> ServiceConfig:
    config = json.loads((KIT / "cardea" / name).read_text())
    config["network"]["host"] = "10.0.0.2"  # placeholder in the example
    return ServiceConfig.model_validate(config)


def test_service_config_example_is_valid() -> None:
    parsed = service_config("config.json")
    assert parsed.network is not None and parsed.agent_session is not None


def test_provisioning_example_is_valid_and_grants_schedules() -> None:
    document = Provisioning.model_validate_json(
        (KIT / "cardea" / "provision.telos.json").read_text()
    )
    rights = {p.operation for p in document.permissions if p.principal_id == "telos"}
    assert {"tasks.schedule", "models.invoke", "mail.send"} <= rights
    assert {p.principal_id for p in document.platform} == {"platform"}


def test_readme_requires_a_private_store_directory() -> None:
    readme = (KIT / "README.md").read_text()
    assert "-m 0700 /var/lib/cardea" in readme and "unsafe_directory" in readme


# Confined workers


def test_worker_configuration_passes_the_helper_checks() -> None:
    config = json.loads((KIT / "cardea" / "cardea-host-workers.json").read_text())
    assert set(config) == {"profiles", "workers"}
    for name, entry in config["profiles"].items():
        assert re.fullmatch(launcher.NAME, name)
        launcher.validate_profile(entry)
    for name, entry in config["workers"].items():
        assert re.fullmatch(launcher.NAME, name)
        launcher.validate_worker(entry, config["profiles"])
    # Browsers start one at a time on a small host.
    assert config["profiles"]["browser"]["concurrency"] == 1


def test_confined_config_names_an_installed_session_profile() -> None:
    parsed = service_config("config.confined.json")
    workers = json.loads((KIT / "cardea" / "cardea-host-workers.json").read_text())
    session = parsed.agent_session
    assert parsed.host_execution and session is not None
    assert session.confined_profile in workers["workers"][session.confined_worker]["profiles"]
    profile = workers["profiles"][session.confined_profile]
    assert profile["network"]["policy"] == "model_api_only" and "credential" in profile


def test_harness_registry_example_runs_every_harness_confined() -> None:
    """ADR-033: each harness runs under a worker that lists its profile; the profile reaches
    only its provider's host, holds the credential, and the worker runs the entry's kind."""
    parsed = service_config("config.harnesses.json")
    assert parsed.host_execution and parsed.agent_session is None
    assert parsed.harnesses_file == Path("/etc/cardea-host-harnesses.json")
    registry = HarnessFile.model_validate_json(
        (KIT / "cardea" / "cardea-host-harnesses.json").read_text()
    )
    workers = json.loads((KIT / "cardea" / "cardea-host-workers.json").read_text())
    assert {h.kind for h in registry.harnesses} == {"claude-agent-sdk", "opencode"}
    for entry in registry.harnesses:
        assert entry.confined is not None and entry.credential is None, entry.id
        worker = workers["workers"][entry.confined.worker]
        assert entry.confined.profile in worker["profiles"]
        assert worker["argv"][-2:] == ["-m", "cardea.hosts.session_worker"]
        assert worker.get("env", {}).get("CARDEA_HARNESS_KIND", "claude-agent-sdk") == entry.kind
        profile = workers["profiles"][entry.confined.profile]
        assert profile["network"]["policy"] == "model_api_only" and "credential" in profile
        assert profile["jit"] is True  # Node (Claude Code) and Bun (OpenCode) need a JIT
        if entry.provider is not None:
            assert entry.provider.base_url.host in profile["network"]["hosts"]
            binary = worker["env"]["CARDEA_OPENCODE_BINARY"]
            assert entry.package.binary == Path(binary)
            assert any(binary.startswith(path + "/") for path in worker["read_only"])


def test_proxy_enforces_the_same_hosts_as_each_profile() -> None:
    squid = (KIT / "cardea" / "squid-workers.conf").read_text()
    profiles = json.loads((KIT / "cardea" / "cardea-host-workers.json").read_text())["profiles"]
    for name, entry in profiles.items():
        network = entry["network"]
        if network["policy"] == "none":
            continue
        assert f"http_port {network['proxy']} name={name}" in squid
        listed = re.search(rf"^acl {name}_hosts dstdomain (.+)$", squid, re.MULTILINE)
        if network["policy"] == "public_web":
            # No host list: the port allows any destination that non_public does not refuse.
            assert listed is None and f"\nhttp_access allow {name}_port\n" in squid
            continue
        assert listed is not None and listed.group(1).split() == network["hosts"]


def test_proxy_config_is_standalone_and_denies_by_default() -> None:
    """The kit replaces Debian's squid.conf, whose localhost allow and wildcard port would
    otherwise relay every destination for loopback clients."""
    lines = [
        line.strip()
        for line in (KIT / "cardea" / "squid-workers.conf").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not [line for line in lines if line.split()[0] == "include"]
    assert not [line for line in lines if "localhost" in line and "allow" in line]
    ports = [line for line in lines if line.startswith("http_port")]
    assert ports
    for line in ports:
        match = re.fullmatch(r"http_port (127\.\d+\.\d+\.\d+):(\d+) name=([a-z][a-z0-9-]*)", line)
        assert match is not None, line  # explicit loopback address, never a wildcard
        assert match.group(1) != "127.0.0.1", line  # the tool endpoints' address
    for line in lines:
        assert not line.startswith(("https_port", "icp_port 3130", "snmp_port")), line
    profiles = json.loads((KIT / "cardea" / "cardea-host-workers.json").read_text())["profiles"]
    public = {n for n, p in profiles.items() if p["network"]["policy"] == "public_web"}
    access = [line for line in lines if line.startswith("http_access")]
    allows = [line for line in access if line.startswith("http_access allow")]
    for line in allows:
        paired = re.fullmatch(r"http_access allow ([a-z][a-z0-9-]*)_port \1_hosts", line)
        unrestricted = re.fullmatch(r"http_access allow ([a-z][a-z0-9-]*)_port", line)
        # Only a public_web profile's port may allow without a host list.
        assert paired or (unrestricted and unrestricted.group(1) in public), line
    first_allow = access.index(allows[0])
    for rule in (
        "deny to_localhost",
        "deny to_linklocal",
        "deny non_public",
        "deny CONNECT !tls_port",
        "deny !safe_ports",
    ):
        assert access.index(f"http_access {rule}") < first_allow
    assert access[-1] == "http_access deny all"
    refused = " ".join(
        line.split(" ", 3)[3] for line in lines if line.startswith("acl non_public dst")
    )
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "224.0.0.0/4",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    ):
        assert network in refused.split(), network


def test_kernel_drop_in_allows_the_sudo_helper() -> None:
    service = unit(KIT / "cardea" / "cardea-kernel.service")["Service"]
    drop_in = unit(KIT / "cardea" / "cardea-kernel.service.d" / "host-execution.conf")["Service"]
    assert service["NoNewPrivileges"] == "yes" and drop_in["NoNewPrivileges"] == "no"
    assert drop_in["ReadWritePaths"] == str(launcher.ROOT)


# SPIRE


def test_spire_units_match_the_configuration() -> None:
    server = unit(KIT / "spire" / "spire-server.service")["Service"]
    agent = unit(KIT / "spire" / "spire-agent.service")["Service"]
    assert server["ExecStart"].endswith("-config /etc/spire/server.conf")
    assert server["User"] == "spire" and server["StateDirectory"] == "spire/server"
    server_conf = (KIT / "spire" / "server.conf").read_text()
    assert 'data_dir = "/var/lib/spire/server"' in server_conf
    assert f'socket_path = "/run/{server["RuntimeDirectory"]}/api.sock"' in server_conf
    # Join-token bootstrap: an optional environment file, emptied after first attestation.
    assert agent["EnvironmentFile"] == "-/etc/spire/agent-join-token.env"
    assert agent["ExecStart"].endswith("-config /etc/spire/agent.conf $SPIRE_AGENT_JOIN")
    agent_conf = (KIT / "spire" / "agent-cardea.conf").read_text()
    assert 'data_dir = "/var/lib/spire/agent"' in agent_conf
    socket_path = f"/run/{agent['RuntimeDirectory']}/agent.sock"
    assert f'socket_path = "{socket_path}"' in agent_conf
    assert service_config("config.json").spiffe_socket == f"unix://{socket_path}"
    kernel = unit(KIT / "cardea" / "cardea-kernel.service")["Unit"]
    assert kernel["Requires"] == "spire-agent.service"


# TLS


def openssl3() -> str:
    path = shutil.which("openssl")
    if path is None:
        pytest.skip("openssl is not installed")
    version = subprocess.run([path, "version"], capture_output=True, text=True).stdout
    if not version.startswith("OpenSSL 3"):
        pytest.skip(f"make-tls.sh needs OpenSSL 3, found {version.strip()}")
    return path


def test_make_tls_certificates_pass_strict_verification(tmp_path: Path) -> None:
    openssl3()
    out = tmp_path / "tls"
    subprocess.run(
        ["sh", str(KIT / "cardea" / "make-tls.sh"), "127.0.0.1", str(out)],
        check=True,
        capture_output=True,
    )
    assert sorted(p.name for p in out.iterdir()) == ["ca.crt", "ca.key", "server.crt", "server.key"]
    ca = x509.load_pem_x509_certificate((out / "ca.crt").read_bytes())
    leaf = x509.load_pem_x509_certificate((out / "server.crt").read_bytes())
    ca_usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert ca_usage.critical and ca_usage.value.key_cert_sign and ca_usage.value.crl_sign
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    leaf_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage)
    assert leaf_usage.critical and leaf_usage.value.digital_signature
    leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)

    # A real handshake with the default client context, which is strict on Python 3.13.
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(out / "server.crt", out / "server.key")
    client_context = ssl.create_default_context(cafile=out / "ca.crt")
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        def serve() -> None:
            connection, _ = listener.accept()
            with server_context.wrap_socket(connection, server_side=True) as tls:
                tls.recv(1)

        thread = threading.Thread(target=serve)
        thread.start()
        with (
            socket.create_connection(("127.0.0.1", port)) as raw,
            client_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
        ):
            assert tls.version() == "TLSv1.3"
            tls.sendall(b"x")
        thread.join(timeout=5)
