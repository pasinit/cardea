"""Confined harnesses (ADR-033 with ADR-028): the session worker runs either harness.

The root helper and its sandbox are Debian-only. Here a `ProcessHost` plays the helper: it
starts the real session worker as a separate process with only the environment the
sandbox would give it (the worker's configured variables, the credential path and the
profile's proxy variables). The OpenCode run goes through a recording forward proxy, as it
would through the profile's egress proxy, while the tool endpoint stays direct.
"""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from fake_model import FakeModel, Reply
from test_agent_runs import GRANTS, Tools, descriptor
from test_confined import WorkerHost, finished
from test_operations import Auth, FakeClient

from cardea.kernel.models import OperationState
from cardea.runtime.harness import HarnessRequest
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication
from cardea.service.confined import ConfinedAgentSession, Workers
from cardea.service.database import Database, borrow
from cardea.service.harnesses import Catalog, HarnessEntry, Harnesses
from cardea.service.kernel import Kernel, provision
from cardea.service.models import AgentRunRequest, Permission, Principal, Provisioning, now
from cardea.service.operations import Registry

SRC = Path(__file__).resolve().parents[2] / "src"
BINARY = os.environ.get("CARDEA_TEST_OPENCODE")
needs_binary = pytest.mark.skipif(
    not BINARY and os.environ.get("CARDEA_REQUIRE_OPENCODE") != "1",
    reason="set CARDEA_TEST_OPENCODE to the pinned OpenCode binary",
)
INSTALLED = {
    "profiles": {
        "openrouter": {"memory_mb": 768, "network": {"policy": "model_api_only"}, "sha256": "p"}
    },
    "workers": {"opencode": ["openrouter"]},
    "sha256": "config",
}


def settings(sha256: str, base_url: str = "http://model.test/v1") -> dict[str, Any]:
    return {
        "kind": "opencode",
        "base_url": base_url,
        "headers": {},
        "default_model": "z-ai/glm-4.6",
        "prices": {"z-ai/glm-4.6": [0.5, 2.0]},
        "sha256": sha256,
    }


class ProcessHost:
    """Plays the root helper by running the worker as a plain child process."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.installed = INSTALLED
        self.jobs: dict[str, subprocess.Popen[bytes]] = {}
        self.payloads: list[dict[str, Any]] = []

    def profiles(self) -> dict[str, Any]:
        return self.installed

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        self.payloads.append(json.loads(content))
        process = subprocess.Popen(
            [sys.executable, "-m", "cardea.hosts.session_worker"],
            env={**self.env, "PYTHONPATH": str(SRC)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(content)
        process.stdin.close()
        self.jobs[job] = process

    def status(self, job: str) -> dict[str, Any]:
        process = self.jobs[job]
        if process.poll() is None:
            return {"state": "running"}
        assert process.stdout is not None
        return {
            "state": "finished",
            "result": "success",
            "exit_status": str(process.returncode),
            "output": base64.b64encode(process.stdout.read()).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        process = self.jobs.get(job)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return {"stopped": True}

    def cleanup(self, job: str) -> dict[str, Any]:
        return {"cleaned": True}


class ForwardProxy:
    """A plain HTTP forward proxy that sends every host to one upstream and records it."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        self.hosts: list[str] = []

    def __enter__(self) -> "ForwardProxy":
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                target = urlsplit(self.path)
                proxy.hosts.append(target.hostname or "")
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                upstream = http.client.HTTPConnection(*proxy.upstream, timeout=30)
                headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
                path = target.path + (f"?{target.query}" if target.query else "")
                upstream.request(self.command, path, body=body, headers=headers)
                response = upstream.getresponse()
                data = response.read()
                self.send_response(response.status)
                kept = ("content-type",)
                for key, value in response.getheaders():
                    if key.lower() in kept:
                        self.send_header(key, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                upstream.close()

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def sandbox_env(tmp_path: Path, binary: Path, proxy: str | None) -> dict[str, str]:
    """What bubblewrap's --clearenv plus the worker and profile configuration leave."""
    key = tmp_path / "credential"
    key.write_text("sk-provider-secret\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CARDEA_CREDENTIAL": str(key),
        "CARDEA_HARNESS_KIND": "opencode",
        "CARDEA_OPENCODE_BINARY": str(binary),
    }
    if proxy:
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[name] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    return env


# Service side


def test_confined_opencode_sends_settings_and_no_credential() -> None:
    host = WorkerHost()
    host.installed["workers"]["opencode"] = ["session"]
    host.outcome = finished({"output": {"text": "done", "success": True, "turns": 2}})
    harness = ConfinedAgentSession(
        Workers(host),
        "session",
        "opencode",
        "z-ai/glm-4.6",
        settings=settings("0" * 64),
        requires_tool_listing=True,
    )
    request = HarnessRequest(
        system_prompt="s",
        prompt="p",
        mcp_url="http://127.0.0.1:4100/mcp",
        mcp_token="t",
        tool_names=("pages-fetch",),
        max_turns=4,
        max_cost_usd=0.5,
        timeout_seconds=30,
        job_key="task-1:1",
    )
    result = asyncio.run(harness.run(request))
    assert result.success and harness.requires_tool_listing
    (started,) = host.starts
    assert started["payload"]["harness"] == settings("0" * 64)
    assert "credential" not in json.dumps(started["payload"]).lower()


def test_registry_allows_confined_opencode() -> None:
    entry = HarnessEntry.model_validate(
        {
            "id": "openrouter",
            "kind": "opencode",
            "package": {
                "name": "opencode-ai",
                "version": "1.18.35",
                "binary": "/opt/cardea/opencode/opencode",
                "sha256": "0" * 64,
            },
            "confined": {"profile": "openrouter", "worker": "opencode"},
            "provider": {"base_url": "https://openrouter.ai/api/v1"},
            "default_model": "z-ai/glm-4.6",
            "models": {
                "z-ai/glm-4.6": {"price": {"input_usd_per_mtok": 0.6, "output_usd_per_mtok": 2.2}}
            },
        }
    )
    assert entry.confined is not None and entry.credential is None


# Worker side


def run_worker(env: dict[str, str], document: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "cardea.hosts.session_worker"],
        env={**env, "PYTHONPATH": str(SRC)},
        input=json.dumps(document).encode(),
        capture_output=True,
        timeout=120,
        check=True,
    )
    return cast(dict[str, Any], json.loads(done.stdout)["output"])


def worker_document(sha256: str, mcp_url: str = "http://127.0.0.1:1/mcp") -> dict[str, Any]:
    return {
        "model": "z-ai/glm-4.6",
        "request": {
            "system_prompt": "s",
            "prompt": "p",
            "mcp_url": mcp_url,
            "mcp_token": "t",
            "tool_names": ["listings-search"],
            "max_turns": 4,
            "max_cost_usd": 0.5,
            "timeout_seconds": 30,
        },
        "harness": settings(sha256),
    }


def test_worker_refuses_a_binary_that_is_not_the_pinned_one(tmp_path: Path) -> None:
    binary = tmp_path / "opencode"
    binary.write_bytes(b"some other build")
    env = sandbox_env(tmp_path, binary, None)
    output = run_worker(env, worker_document("0" * 64))
    assert output["failure_reason"] == "harness_pin_mismatch"
    digest = hashlib.sha256(b"some other build").hexdigest()
    document = worker_document(digest)
    document["harness"]["kind"] = "claude-agent-sdk"
    assert run_worker(env, document)["failure_reason"] == "harness_kind_mismatch"


def test_claude_worker_without_a_credential_refuses(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    output = run_worker(env, {"model": "m", "request": worker_document("0" * 64)["request"]})
    assert output["failure_reason"] == "harness_credential_missing"


# End to end: a kernel run on a confined OpenCode harness


@pytest.fixture
def binary() -> Path:
    assert BINARY, "CARDEA_REQUIRE_OPENCODE=1 but CARDEA_TEST_OPENCODE is not set"
    return Path(BINARY)


def make_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Any) -> Kernel:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    tools = Tools()
    registry = Registry(
        (
            descriptor("mail.send", "effect", idempotency_scope=["/to"]),
            descriptor("listings.search", "read"),
        ),
        {"test.mail.send": tools.send, "test.listings.search": tools.search},
    )
    db = Database(tmp_path / "data")
    db.migrate()
    principals = tuple(
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="test",
            subject=p,
        )
        for p in ("runtime", "supervisor")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*GRANTS, "harness:openrouter")
    )
    entry = HarnessEntry.model_validate(
        {
            "id": "openrouter",
            "kind": "opencode",
            "package": {
                "name": "opencode-ai",
                "version": "1.18.35",
                "binary": "/opt/cardea/opencode/opencode",
                "sha256": "0" * 64,
            },
            "confined": {"profile": "openrouter", "worker": "opencode"},
            "provider": {"base_url": "http://model.test/v1"},
            "default_model": "z-ai/glm-4.6",
            "models": {
                "z-ai/glm-4.6": {"price": {"input_usd_per_mtok": 0.5, "output_usd_per_mtok": 2}}
            },
        }
    )
    installed = Harnesses({"openrouter": harness}, Catalog({"openrouter": entry}, "openrouter"))
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
        harnesses=installed,
    )
    return Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=installed)


@needs_binary
def test_a_confined_opencode_run_reaches_the_model_only_through_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: Path
) -> None:
    replies = [
        Reply(tool_calls=[("cardea_listings-search", {})]),
        Reply(tool_calls=[("cardea_mail-send", {"to": "team", "body": "3 listings"})]),
        Reply(text="Sent the recap."),
    ]
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with FakeModel(replies) as model:
        port = int(urlsplit(model.base_url).port or 0)
        with ForwardProxy(("127.0.0.1", port)) as proxy:
            host = ProcessHost(sandbox_env(tmp_path, binary, proxy.address))
            harness = ConfinedAgentSession(
                Workers(cast(Any, host)),
                "openrouter",
                "opencode",
                "z-ai/glm-4.6",
                settings=settings(digest),
                requires_tool_listing=True,
            )
            kernel = make_kernel(tmp_path, monkeypatch, harness)
            try:
                request = AgentRunRequest.model_validate(
                    {
                        "request_id": "recap-1",
                        "agent_id": "agent",
                        "namespace": "test",
                        "objective": "Daily recap",
                        "instructions": "Search listings, then send one recap.",
                        "operations": ["listings.search", "mail.send"],
                        "grants": [*GRANTS, "harness:openrouter"],
                        "harness": "openrouter",
                        "attempt_timeout_seconds": 120,
                        "max_attempts": 1,
                    }
                )
                task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
                assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
                with kernel.database.engine.begin() as conn, borrow(conn) as tx:
                    receipts = tx.list_operations(task_id)
            finally:
                kernel.database.close()
    assert [(r.request.name, r.state) for r in receipts] == [
        ("listings.search", OperationState.SUCCEEDED),
        ("mail.send", OperationState.SUCCEEDED),
    ]
    # Every model call went through the profile's proxy, to the provider's host; the
    # tool endpoint on 127.0.0.1 was reached directly (NO_PROXY).
    assert proxy.hosts and set(proxy.hosts) == {"model.test"}
    assert {h["authorization"] for h in model.headers} == {"Bearer sk-provider-secret"}
    # The credential reached the worker as a file path, never in the payload.
    (payload,) = host.payloads
    assert "sk-provider-secret" not in json.dumps(payload)
