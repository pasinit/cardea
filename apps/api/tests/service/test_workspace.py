"""A run's own workspace (ADR-037 amendment 2026-11-24): the checkout, the built-in tools,
the upload route and `cardea-change`, and `env.try` trying what was uploaded."""

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from cardea_env_ops.try_ import stage
from mcp import types
from pydantic import ValidationError
from test_harness_contract import request as harness_request

from cardea.runtime import workspace
from cardea.runtime.agent_session import ClaudeAgentSession
from cardea.runtime.harness import HarnessWorkspace
from cardea.service import artifacts, tool_endpoint
from cardea.service.database import Database
from cardea.service.models import AgentRunRequest, RunTemplate
from cardea.service.tool_endpoint import ToolEndpoint


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


async def no_call(name: str, arguments: dict[str, Any], gone: Any) -> types.CallToolResult:
    return types.CallToolResult(content=[], is_error=True)


def post(url: str, body: bytes, token: str | None) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as reply:
            return reply.status, json.load(reply)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}


# The upload route


@pytest.mark.loopback
def test_the_upload_route_needs_the_token_and_a_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[bytes] = []

    async def upload(content: bytes) -> dict[str, Any]:
        if content == b"refuse":
            raise ValueError("lease lost")
        received.append(content)
        return {"name": "change.bundle", "bytes": len(content)}

    monkeypatch.setattr(tool_endpoint, "MAX_UPLOAD_BYTES", 16)
    with ToolEndpoint([], no_call, upload=upload) as tools:
        assert tools.upload_url == f"http://{tools.host}:{tools.port}/objects"
        assert post(tools.upload_url, b"bundle", None)[0] == 401
        assert post(tools.upload_url, b"bundle", "wrong")[0] == 401
        assert post(tools.upload_url, b"bundle", tools.token) == (
            201,
            {"name": "change.bundle", "bytes": 6},
        )
        assert received == [b"bundle"]
        assert post(tools.upload_url, b"x" * 17, tools.token)[0] == 413
        status, body = post(tools.upload_url, b"refuse", tools.token)
        assert (status, body["error"], body["detail"]) == (422, "upload_refused", "lease lost")
    # A run without a workspace has no upload route.
    with ToolEndpoint([], no_call) as tools:
        assert post(tools.upload_url, b"bundle", tools.token)[0] == 404


def test_an_upload_is_refused_outside_its_bounds_and_lease(tmp_path: Path) -> None:
    database = Database(tmp_path / "data")
    database.migrate()
    with pytest.raises(artifacts.ArtifactRefused):
        artifacts.store_upload(database, "test", "task-1", "lease-1", b"", "agent")
    with pytest.raises(artifacts.ArtifactRefused):
        artifacts.store_upload(
            database, "test", "task-1", "lease-1", b"x" * (artifacts.MAX_UPLOAD_BYTES + 1), "a"
        )
    # No such task, so no lease to write under: the store refuses.
    with pytest.raises(Exception) as refused:
        artifacts.store_upload(database, "test", "task-1", "lease-1", b"bundle", "agent")
    assert type(refused.value).__name__ == "StoreError"


# The request


def run_request(**changes: Any) -> AgentRunRequest:
    fields: dict[str, Any] = {
        "request_id": "run-1",
        "agent_id": "dev-1",
        "namespace": "test",
        "objective": "Change the code",
        "instructions": "Do the item.",
        "operations": ["env.try"],
        "grants": ["models.invoke", "objects.read", "objects.write", "env.try", "repo:app"],
        **changes,
    }
    return AgentRunRequest.model_validate(fields)


def test_a_workspace_repository_needs_the_runs_repo_grant() -> None:
    assert run_request(workspace={"repositories": ["app"]}).workspace is not None
    with pytest.raises(ValidationError, match="repo:<alias>"):
        run_request(workspace={"repositories": ["other"]})
    # Unset, it is left out of the request's document, so existing digests are unchanged.
    assert "workspace" not in run_request().model_dump(mode="json")
    template = RunTemplate.model_validate(
        {"instructions": "x", "operations": ["env.try"], "workspace": {"repositories": ["app"]}}
    )
    assert template.workspace is not None and template.workspace.repositories == ("app",)


def test_built_in_tools_only_with_a_workspace(tmp_path: Path) -> None:
    session = ClaudeAgentSession(model="m", oauth_token="secret")
    plain = session.options(harness_request(), tmp_path)
    assert plain["tools"] == []
    assert plain["allowed_tools"] == ["mcp__cardea__pages-fetch"]
    assert "CARDEA_UPLOAD_URL" not in plain["env"]
    space = HarnessWorkspace(repositories=("app",), upload_url="http://127.0.0.1:1/objects")
    options = session.options(harness_request(workspace=space), tmp_path)
    assert options["tools"] == ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    assert "Bash" in options["allowed_tools"] and "Edit(./**)" in options["allowed_tools"]
    assert "mcp__cardea__pages-fetch" in options["allowed_tools"]
    assert options["env"]["CARDEA_UPLOAD_URL"] == "http://127.0.0.1:1/objects"
    assert options["env"]["PATH"].startswith(str(tmp_path / ".cardea/bin"))
    assert "./app" in options["system_prompt"] and "cardea-change" in options["system_prompt"]
    # Repository configuration stays untrusted.
    assert options["setting_sources"] == [] and options["strict_mcp_config"] is True


# The checkout, cardea-change and env.try


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "origin"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    (work / "mode").write_text("main\n")
    git(work, "add", "mode")
    git(work, "commit", "--quiet", "-m", "main")
    bare = tmp_path / "source.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
    registry = tmp_path / "repositories.json"
    registry.write_text(json.dumps({"repositories": {"app": {"source": str(bare)}}}))
    registry.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(registry))
    return bare


class Capture(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        assert self.headers["Authorization"] == "Bearer upload-token"
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Capture.bodies.append(body)
        reply = json.dumps({"name": "change.bundle", "bytes": len(body)}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.loopback
def test_a_workspace_change_reaches_env_try_without_a_push(tmp_path: Path, source: Path) -> None:
    workdir = tmp_path / "session"
    workdir.mkdir()
    (checkout,) = workspace.prepare(workdir, ("app",))
    assert (checkout / "mode").read_text() == "main\n"
    # The clone runs no repository hooks and commits as the agent, not as the host.
    assert git(checkout, "config", "--local", "core.hooksPath") == os.devnull
    server = HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {
            **os.environ,
            **workspace.environment(
                workdir, f"http://127.0.0.1:{server.server_port}/objects", "upload-token"
            ),
        }
        tool = [sys.executable, "-I", str(workdir / workspace.BIN / "cardea-change")]
        empty = subprocess.run(tool, cwd=checkout, env=env, capture_output=True, text=True)
        assert empty.returncode != 0 and "no commits" in empty.stderr
        (checkout / "mode").write_text("changed\n")
        subprocess.run(["git", "commit", "-qam", "change"], cwd=checkout, env=env, check=True)
        head = git(checkout, "rev-parse", "HEAD")
        done = subprocess.run(tool, cwd=checkout, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    finally:
        server.shutdown()
    printed = json.loads(done.stdout)
    assert printed["head"] == head and printed["repo"] == "app"
    assert printed["change"]["bytes"] == len(Capture.bodies[-1])
    # env.try tries exactly that commit, which the source never received.
    scratch = tmp_path / "try"
    scratch.mkdir()
    tried = stage(str(source), head, scratch, bundle=Capture.bodies[-1])
    assert (tried / "mode").read_text() == "changed\n"


def test_a_workspace_names_only_registered_repositories(tmp_path: Path, source: Path) -> None:
    with pytest.raises(workspace.WorkspaceRefused, match="unknown"):
        workspace.prepare(tmp_path, ("other",))
