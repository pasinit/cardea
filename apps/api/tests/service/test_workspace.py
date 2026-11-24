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


# A member's memory


def archive(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            packed.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_a_memory_archive_holds_only_plain_relative_files() -> None:
    import io
    import tarfile

    from cardea.service import memory

    assert memory.check(archive({"INDEX.md": b"x", "notes/a.md": b"yy"})) == 3
    for bad in ("/etc/passwd", "../escape", "notes/../../escape"):
        with pytest.raises(memory.MemoryRefused, match="plain relative"):
            memory.check(archive({bad: b"x"}))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        packed.addfile(link)
    with pytest.raises(memory.MemoryRefused, match="plain relative"):
        memory.check(buffer.getvalue())
    with pytest.raises(memory.MemoryRefused, match="gzipped tar"):
        memory.check(b"not an archive")
    with pytest.raises(memory.MemoryRefused, match="bytes"):
        memory.check(b"")


def test_memory_is_recalled_into_the_workspace_and_its_index_reaches_the_prompt(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    folder = workspace.recall(fresh, None)
    assert (folder / "INDEX.md").read_text().startswith("# Memory index")
    kept = tmp_path / "kept"
    kept.mkdir()
    workspace.recall(kept, archive({"INDEX.md": b"- reviewer wants small PRs\n", "a.md": b"z"}))
    assert (kept / "memory" / "a.md").read_text() == "z"
    prompt = workspace.instructions(("app",), kept)
    assert "reviewer wants small PRs" in prompt and "cardea-memory save" in prompt
    # Only the head of a long index goes into the prompt.
    (kept / "memory" / "INDEX.md").write_text("i" * 20000)
    assert workspace.instructions(("app",), kept).count("i" * 100) <= 82


class CaptureMemory(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        CaptureMemory.bodies.append(body)
        reply = json.dumps({"version": 1, "bytes": len(body)}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.loopback
def test_cardea_memory_save_round_trips_through_recall(tmp_path: Path, source: Path) -> None:
    from cardea.service import memory

    workdir = tmp_path / "session"
    workdir.mkdir()
    workspace.prepare(workdir, ("app",))
    (workdir / "memory" / "lessons.md").write_text("run targeted tests first\n")
    server = HTTPServer(("127.0.0.1", 0), CaptureMemory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {
            **os.environ,
            "HOME": str(workdir),
            **workspace.environment(
                workdir,
                "http://127.0.0.1:1/objects",
                "token",
                f"http://127.0.0.1:{server.server_port}/memory",
            ),
        }
        tool = [sys.executable, "-I", str(workdir / workspace.BIN / "cardea-memory"), "save"]
        done = subprocess.run(tool, cwd=workdir, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    finally:
        server.shutdown()
    saved = CaptureMemory.bodies[-1]
    memory.check(saved)  # what the tool packs, the kernel accepts
    later = tmp_path / "next-run"
    later.mkdir()
    workspace.recall(later, saved)
    assert (later / "memory" / "lessons.md").read_text() == "run targeted tests first\n"


@pytest.mark.loopback
def test_the_memory_route_is_absent_without_a_workspace() -> None:
    async def remember(content: bytes) -> dict[str, Any]:
        return {"version": 1}

    with ToolEndpoint([], no_call, memory=remember) as tools:
        assert post(tools.memory_url, b"x", tools.token) == (201, {"version": 1})
    with ToolEndpoint([], no_call) as tools:
        assert post(tools.memory_url, b"x", tools.token)[0] == 404


def test_a_confined_session_gets_its_memory_as_a_staged_file_not_in_the_request() -> None:
    import asyncio
    import base64
    import hashlib

    from test_confined import WorkerHost, finished

    from cardea.service.confined import ConfinedAgentSession, Workers

    class StagingHost(WorkerHost):
        def start_confined(
            self,
            job: str,
            content: bytes,
            deadline: float,
            profile: str,
            worker: str,
            inputs: list[dict[str, str]] | None = None,
        ) -> None:
            super().start_confined(job, content, deadline, profile, worker)
            self.starts[-1]["inputs"] = inputs

    host = StagingHost()
    host.outcome = finished({"output": {"text": "done", "success": True, "turns": 1}})
    session = ConfinedAgentSession(Workers(host), "session", "agent", "m")
    saved = archive({"INDEX.md": b"notes"})
    space = HarnessWorkspace(repositories=("app",), upload_url="http://127.0.0.1:1/objects")
    asyncio.run(session.run(harness_request(workspace=space, memory=saved)))
    (started,) = host.starts
    assert "memory" not in started["payload"]["request"]
    digest = hashlib.sha256(saved).hexdigest()
    assert started["payload"]["inputs"] == {
        "/memory": {"file": "0", "sha256": digest, "bytes": len(saved)}
    }
    assert started["inputs"] == [{"sha256": digest, "data": base64.b64encode(saved).decode()}]
