"""Disposable real SPIRE services. Secrets never enter evidence or command arguments."""

import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from spiffe.spiffe_id.spiffe_id import SpiffeId
from spiffe.workloadapi.workload_api_client import WorkloadApiClient

from binding import AUDIENCE, DOMAIN, RUNTIME


def eventually[T](call: Callable[[], T], timeout: float = 20) -> T:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return call()
        except Exception:
            if time.monotonic() >= deadline:
                raise AssertionError("SPIRE condition did not become ready") from None
            time.sleep(0.1)


class Lab:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="csp-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.bin = Path(os.environ.get("SPIRE_BIN", "/tmp/cardea-spire-1.15.3/bin"))
        self.processes: list[subprocess.Popen[bytes]] = []
        self.client: WorkloadApiClient | None = None
        self.signer = ec.generate_private_key(ec.SECP256R1())
        self.socket = f"unix://{self.root}/agent.sock"
        self.admin = str(self.root / "server.sock")
        self.observations: dict[str, Any] = {}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content)
        path.chmod(0o600)
        return path

    def command(self, *args: str) -> str:
        # Do not include stdout/stderr on failure: admin commands can return credentials.
        result = subprocess.run(
            [str(self.bin / "spire-server"), *args, "-socketPath", self.admin],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise RuntimeError(f"SPIRE administrative command failed: {' '.join(args[:2])}")
        return result.stdout

    def launch(self, binary: str, config: Path) -> subprocess.Popen[bytes]:
        with (self.root / f"process-{len(self.processes)}.log").open("wb") as log:
            proc = subprocess.Popen(
                [str(self.bin / binary), "run", "-config", str(config)],
                cwd=self.root,
                stdout=log,
                stderr=subprocess.STDOUT,
                umask=0o077,
            )
        self.processes.append(proc)
        return proc

    def start_server(self) -> None:
        self.server = self.launch("spire-server", self.root / "server.conf")
        eventually(lambda: self.command("healthcheck"))

    def start_agent(self) -> None:
        self.agent = self.launch("spire-agent", self.root / "agent.conf")

        def health() -> None:
            result = subprocess.run(
                [
                    str(self.bin / "spire-agent"),
                    "healthcheck",
                    "-socketPath",
                    str(self.root / "agent.sock"),
                ],
                capture_output=True,
                timeout=2,
            )
            assert result.returncode == 0

        eventually(health)

    def register(self, identity: str, uid: int | None = None, ttl: int = 30) -> str:
        output = self.command(
            "entry",
            "create",
            "-parentID",
            f"spiffe://{DOMAIN}/node/fit",
            "-spiffeID",
            identity,
            "-selector",
            f"unix:uid:{os.getuid() if uid is None else uid}",
            "-jwtSVIDTTL",
            str(ttl),
            "-federatesWith",
            "spiffe://negative.test",
            "-output",
            "json",
        )
        data = json.loads(output)
        return str(data["results"][0]["entry"]["id"])

    def start(self) -> None:
        for binary in ("spire-server", "spire-agent"):
            if not (self.bin / binary).is_file():
                raise RuntimeError(
                    "Build pinned SPIRE binaries first; see experiments/spire/README.md"
                )
        for binary in ("spire-server", "spire-agent"):
            metadata = subprocess.run(
                ["go", "version", "-m", str(self.bin / binary)],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout
            assert "vcs.revision=2f7861ae3923caf1f57eb087fc2928d58c0fb1d2" in metadata
            assert "vcs.modified=false" in metadata
        self.write(
            "server.conf",
            f'''
server {{
 bind_address = "127.0.0.1"
 bind_port = {self.port}
 socket_path = "{self.admin}"
 trust_domain = "{DOMAIN}"
 data_dir = "{self.root}/server-data"
 log_level = "WARN"
 default_jwt_svid_ttl = "30s"
}}
plugins {{
 DataStore "sql" {{ plugin_data {{ database_type = "sqlite3"
 connection_string = "{self.root}/server.sqlite3" }} }}
 NodeAttestor "join_token" {{}}
 KeyManager "disk" {{ plugin_data {{ keys_path = "{self.root}/server-keys.json" }} }}
}}
''',
        )
        self.start_server()
        self.write("bundle.pem", self.command("bundle", "show", "-format", "pem"))
        token_output = self.command("token", "generate", "-spiffeID", f"spiffe://{DOMAIN}/node/fit")
        token = token_output.strip().removeprefix("Token: ")
        self.write("join-token", token)
        self.write(
            "agent.conf",
            f'''
agent {{
 data_dir = "{self.root}/agent-data"
 log_level = "WARN"
 server_address = "127.0.0.1"
 server_port = {self.port}
 socket_path = "{self.root}/agent.sock"
 trust_bundle_path = "{self.root}/bundle.pem"
 trust_domain = "{DOMAIN}"
 join_token_file = "{self.root}/join-token"
}}
plugins {{
 NodeAttestor "join_token" {{}}
 KeyManager "disk" {{ plugin_data {{ directory = "{self.root}/agent-data" }} }}
 WorkloadAttestor "unix" {{}}
}}
''',
        )
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.signer.public_key()))
        jwk.update(kid="negative-fixture", use="jwt-svid")
        self.write("negative-bundle.json", json.dumps({"keys": [jwk]}))
        self.command(
            "bundle",
            "set",
            "-id",
            "spiffe://negative.test",
            "-format",
            "spiffe",
            "-path",
            str(self.root / "negative-bundle.json"),
        )
        self.runtime_entry = self.register(RUNTIME)
        self.register(f"spiffe://{DOMAIN}/runtime/unmatched", uid=os.getuid() + 10000)
        self.start_agent()
        self.client = WorkloadApiClient(socket_path=self.socket, default_timeout=0.5)
        eventually(self.token)
        self.observations["bootstrap"] = (
            "trusted bundle + one-use join-token file; Unix UID attestation"
        )

    def token(self, identity: str = RUNTIME, audience: str = AUDIENCE) -> str:
        assert self.client is not None
        return self.client.fetch_jwt_svid({audience}, SpiffeId(identity)).token

    def validate(self, token: str, audience: str = AUDIENCE) -> str:
        assert self.client is not None
        return str(self.client.validate_jwt_svid(token, audience).spiffe_id)

    def negative(self, **changes: Any) -> str:
        claims = {
            "sub": "spiffe://negative.test/runtime/a",
            "aud": [AUDIENCE],
            "exp": int(time.time()) + 30,
        }
        claims.update(changes)
        claims = {k: v for k, v in claims.items() if v is not None}
        return str(
            jwt.encode(claims, self.signer, algorithm="ES256", headers={"kid": "negative-fixture"})
        )

    def stop(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        for proc in reversed(self.processes):
            self.stop(proc)
        self.temp.cleanup()
