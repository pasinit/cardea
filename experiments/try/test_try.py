"""The real daemon end to end under the development identity, plus that identity's guards."""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from cardea.service.admin import initialize
from cardea.service.database import Database, Fault
from cardea.service.kernel import provision
from cardea.service.models import Provisioning, ServiceConfig
from devauth import DevAuthentication, issue
from try_ import Trial, run


def config(tmp_path: Path, **changes: object) -> ServiceConfig:
    tmp_path.chmod(0o700)
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "spiffe_socket": "unix:///nonexistent",
        "trust_domain": "try.invalid",
        "runtime_spiffe_id": "spiffe://try.invalid/runtime/kernel",
    }
    return ServiceConfig.model_validate(values | changes)


def test_crash_recovery_and_authority_through_the_real_daemon() -> None:
    lines: list[str] = []
    result = run(say=lines.append)
    assert any("UNENFORCED development identity" in line and "expected" in line for line in lines)
    assert "[redacted]" not in "\n".join(lines)
    assert result["state"] == "succeeded"
    assert result["receipt_preserved_across_crash"] and result["outsider_denied"]


@pytest.mark.parametrize(
    "changes",
    [{"trust_domain": "example.org"}, {"host_execution": True}],
)
def test_development_identity_refuses_unsafe_configurations(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(Fault, match="unsafe_development|Use a reserved|cannot enable"):
        DevAuthentication(config(tmp_path, **changes))


def test_tokens_are_bound_to_key_audience_domain_and_expiry(tmp_path: Path) -> None:
    settings = config(tmp_path)
    auth = DevAuthentication(settings)
    subject = "spiffe://try.invalid/supervisor/alice"
    assert auth.validate(issue(settings, subject)).subject == subject
    body, signature = issue(settings, subject).split(".")
    forged = json.loads(base64.urlsafe_b64decode(body)) | {"sub": "spiffe://try.invalid/admin"}
    rejected = [
        base64.urlsafe_b64encode(json.dumps(forged).encode()).decode() + "." + signature,
        issue(settings, subject, seconds=-1),
        issue(settings, "spiffe://example.org/supervisor/alice"),
        issue(settings.model_copy(update={"audience": "other"}), subject),
    ]
    for token in rejected:
        with pytest.raises(Fault, match="could not be validated"):
            auth.validate(token)
    with pytest.raises(Fault):
        auth.validate(issue(settings, subject), kind="oidc")


@pytest.fixture
def short_store() -> Iterator[Path]:
    """A store with a short path: it holds the daemon's Unix socket (104 bytes on macOS)."""
    root = Path(tempfile.mkdtemp(prefix="cardea-", dir="/tmp"))
    store = root / "store"
    store.mkdir(mode=0o700)
    try:
        yield store
    finally:
        shutil.rmtree(root)


def test_a_startup_error_after_launch_is_logged_before_the_exit(
    tmp_path: Path, short_store: Path
) -> None:
    # serve() ends with os._exit once the runtime is up, which discards the exception. A
    # TLS certificate that is not private used to stop the daemon with status 1 and no
    # log line at all (found on the first Debian acceptance run).
    initialize(short_store)
    certificate, key = tmp_path / "server.pem", tmp_path / "server.key"
    certificate.write_text("not parsed: its mode is refused first\n")
    certificate.chmod(0o644)
    key.write_text("x\n")
    key.chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    network = {"host": "127.0.0.1", "port": port}
    files = {"certificate_file": str(certificate), "key_file": str(key)}
    settings = config(short_store, network=network | files)
    database = Database(short_store)
    try:  # the runtime identity only, so that the daemon gets past its own enrollment
        runtime = Trial.spiffe("runtime", settings.runtime_spiffe_id)
        provision(database, Provisioning(principals=(runtime,), permissions=(), platform=()))
    finally:
        database.close()
    path = tmp_path / "config.json"
    path.write_text(settings.model_dump_json(exclude_none=True))
    path.chmod(0o600)
    here = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(here.parents[1] / "apps/api/src"), str(here)])
    done = subprocess.run(
        [sys.executable, str(here / "daemon.py"), str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 1
    stopped = [
        json.loads(line)
        for line in done.stderr.splitlines()
        if line.startswith("{") and "service stopped" in line
    ]
    assert stopped, done.stderr[-2000:]
    assert stopped[0].get("code") == "unsafe_file", stopped[0]
    assert stopped[0]["level"] == "CRITICAL"
