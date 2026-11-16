"""Milestone 1 dress rehearsal: a software team on the real operation packages, all fake.

`make teams` runs it after the two worked examples. Everything the team does goes through
the kernel's real primitives and the real operation packages:

- `cardea-coding-ops`: `code.session`, in its confined-worker shape, with a fake harness
  and fake gates, against a local bare repository;
- `cardea-vcs-ops`: `vcs.*` against the in-process fake GitHub, with the test-only token;
- the kernel's content gate, with a stand-in scanner and a denylist;
- records with a state machine and claims, mailboxes, spend accounts and `expect` with
  `any_of`.

What is fake: the agents (scripts over each run's real MCP endpoint), the coding harness,
the repository host, identities, and the confined-worker helper (it runs the real worker
command as a local process). Runs execute one at a time in this process.
"""

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import cardea
import httpx2
from cardea_vcs_ops import Vcs
from cardea_vcs_ops import descriptors as vcs_descriptors
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.fake import FakeGitHub
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import kernel as kernel_module
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.confined import Workers
from cardea.service.database import Database, Fault
from cardea.service.groups import apply as apply_group
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    ContentGateConfig,
    Provisioning,
)
from cardea.service.operations import OperationDescriptor, Registry
from cardea.service.records import CollectionDocument, apply_collection

HERE = Path(__file__).resolve().parent / "dev-team"
GROUP, NAMESPACE, SUPERVISOR = "dev-team", "dev", "sup-dev"
LEAD = "dev-team.lead.0"
# The secret a developer's change carries by mistake; the gate's denylist names it.
PLANTED = "ghp_PLANTEDDEMOSECRET0000000000000000"
TOKEN = "ghs_" + "rehearsal" * 4  # the fake host's test token; never real
RUN_COST = 0.02  # what each scripted session reports spending
Script = Callable[[ClientSession, SessionRequest], Awaitable[Any]]

HARNESS = """
import json, os, pathlib
for path, content in json.loads(os.environ["CARDEA_TASK"]).get("write", {}).items():
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
print("applied the change")
"""
GATE = "import sys; print('ok'); sys.exit(0)\n"


class Credentials:
    """Fixed identities for a throwaway store."""

    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "example", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in {"runtime", SUPERVISOR}:
            raise Fault("invalid_credential", "Unknown example credential", 401)
        return Proof("spiffe", "example", token, time.time() + 60)


class NoQueue:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class ScriptedAgents:
    """The agent harness: plays the next script over the run's MCP endpoint."""

    def __init__(self) -> None:
        self.scripts: list[Script] = []
        self.results: list[Any] = []

    async def run(self, request: SessionRequest) -> SessionResult:
        script = self.scripts.pop(0)
        headers = {"Authorization": f"Bearer {request.mcp_token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(request.mcp_url, http_client=http) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            self.results.append(await script(session, request))
        return SessionResult(text="Done.", is_error=False, turns=1, cost_usd=RUN_COST)


class LocalWorkerHost:
    """The confined-worker helper's interface, running the real worker command locally."""

    def __init__(self, coding_config: Path) -> None:
        self.env = {
            "CARDEA_CODING_CONFIG": str(coding_config),
            "PYTHONPATH": str(Path(cardea.__file__).parents[1]),
        }
        self.outputs: dict[str, bytes] = {}

    def profiles(self) -> dict[str, Any]:
        return {
            "profiles": {
                "coding": {
                    "wall_seconds": 3600,
                    "network": {"policy": "model_api_only"},
                    "sha256": "rehearsal-coding",
                }
            },
            "workers": {"code-session": ["coding"]},
            "sha256": "rehearsal",
        }

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "cardea.hosts.operation_worker", "coding.code.session"],
            input=content,
            capture_output=True,
            env={**os.environ, **self.env},
            timeout=120,
            check=False,
        )
        self.outputs[job] = done.stdout

    def status(self, job: str) -> dict[str, Any]:
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "output": base64.b64encode(self.outputs[job]).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        return {}

    def cleanup(self, job: str) -> dict[str, Any]:
        return {}


@dataclass
class World:
    kernel: Kernel
    agents: ScriptedAgents
    host: FakeGitHub
    repo_path: Path
    tasks: list[str] = field(default_factory=list)


def document(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((HERE / name).read_text()))


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=owner", "-c", "user.email=owner@example.invalid"]
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def provisioning(group: AgentGroup) -> Provisioning:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rights = {*RIGHTS, "models.invoke", "groups.manage", *(g for p in group.spec.pools for g in p.grants)}
    grant = {"namespace": NAMESPACE, "expires_at": expires}
    return Provisioning.model_validate(
        {
            "principals": [
                {
                    "principal_id": p,
                    "kind": "service" if p == "runtime" else "human",
                    "auth_kind": "spiffe",
                    "issuer": "example",
                    "subject": p,
                }
                for p in ("runtime", SUPERVISOR)
            ],
            "permissions": [
                {"principal_id": SUPERVISOR, "operation": r, "delegate": True, **grant}
                for r in sorted(rights)
            ],
            "platform": [{"principal_id": "platform", "operation": r, **grant} for r in sorted(rights)],
        }
    )


def gate(directory: Path) -> ContentGateConfig:
    """The kernel's content gate: a stand-in for the Gitleaks binary and a denylist."""
    scanner = directory / "no-findings"
    scanner.write_text("#!/bin/sh\ncat >/dev/null\necho '[]'\n")
    scanner.chmod(0o700)
    denylist = directory / "denylist"
    denylist.write_text(PLANTED + "\n")
    denylist.chmod(0o600)
    return ContentGateConfig(
        gitleaks_binary=scanner,
        gitleaks_sha256=hashlib.sha256(scanner.read_bytes()).hexdigest(),
        denylist_file=denylist,
        notify_supervisor=True,
    )


def coding(directory: Path, source: Path) -> Path:
    """`code.session`'s host configuration: the repository, its gates and the fake harness."""
    scripts = directory / "coding"
    scripts.mkdir()
    (scripts / "harness.py").write_text(HARNESS)
    (scripts / "gate.py").write_text(GATE)
    gates = [
        {"name": name, "argv": [sys.executable, str(scripts / "gate.py")]}
        for name in ("lint", "test")
    ]
    config = directory / "coding.json"
    config.write_text(
        json.dumps(
            {
                "repositories": {"app": {"source": str(source), "gates": gates}},
                "harness": {"argv": [sys.executable, str(scripts / "harness.py")]},
            }
        )
    )
    return config


@contextmanager
def world(directory: Path) -> Iterator[World]:
    setattr(kernel_module, "DBOSClient", NoQueue)  # noqa: B010
    host = FakeGitHub(directory / "host")
    host.root.mkdir()
    repository = host.add("acme", "app")
    repo_path = host.repositories["acme/app"].path
    seed = directory / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("# app\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "Initial")
    git(seed, "push", "-q", str(repo_path), "main")
    vcs = Vcs(
        VcsConfig(
            repositories={"app": repository},
            credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)),
        ),
        transport=host.transport(),
    )
    session = OperationDescriptor.model_validate_json(
        (
            Path(__file__).resolve().parents[2]
            / "packages/cardea-coding-ops/src/cardea_coding_ops/descriptors/code.session.json"
        ).read_text()
    )
    registry = Registry((*vcs_descriptors(), session), vcs.handlers())
    database = Database(directory / "store")
    database.migrate()
    provision(database, provisioning(AgentGroup.model_validate(document("group.json"))), registry)
    agents = ScriptedAgents()
    catalog = Catalog({IMPLICIT_ID: implicit_entry("rehearsal-model")}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    kernel = Kernel(
        database,
        cast(Authentication, Credentials()),
        operations=registry,
        harnesses=Harnesses({IMPLICIT_ID: agents}, catalog),
        workers=Workers(LocalWorkerHost(coding(directory, repo_path))),
    )
    kernel.content_gate = gate(directory)
    try:
        yield World(kernel, agents, host, repo_path)
    finally:
        database.close()


def apply(world: World, name: str) -> dict[str, Any]:
    """`PUT /v0.2/groups/dev-team` with a manifest from this directory, in process."""
    group = AgentGroup.model_validate(document(name))
    return apply_group(world.kernel, world.kernel.auth.validate(SUPERVISOR), GROUP, group, False)


def configure(world: World) -> None:
    for path in sorted(HERE.glob("collection.*.json")):
        collection = CollectionDocument.model_validate(json.loads(path.read_text()))
        with world.kernel.database.engine.begin() as conn:
            apply_collection(conn, SUPERVISOR, collection, dry_run=False)
    result = apply(world, "group.json")
    members = ", ".join(m["principal_id"] for m in result["group"]["status"]["members"])
    print(f"  applied {GROUP} (generation {result['generation']}): {members}")


async def call(client: ClientSession, tool: str, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        return {"error": str(getattr(result.content[0], "text", ""))}
    return result.structured_content


def run(world: World, member: str, script: Script) -> tuple[str, Any]:
    """One run of a member, from the member's own trigger template (its pool's run: its
    operations, context, bounds and `expect`), played by `script`."""
    with world.kernel.database.read() as conn:
        definition = conn.exec_driver_sql(
            "SELECT definition_json FROM triggers WHERE trigger_id=?", (f"{member}.t0",)
        ).scalar_one()
    template = json.loads(definition)["template"]
    request = AgentRunRequest.model_validate(
        {**template, "request_id": f"{member}.{time.monotonic_ns()}"}
    )
    world.agents.scripts, world.agents.results = [script], []
    task_id = world.kernel.submit(world.kernel.auth.validate(SUPERVISOR), request).task.task_id
    world.tasks.append(task_id)
    state = AgentRunRuntime(world.kernel).execute(task_id)
    return state, world.agents.results[-1] if world.agents.results else None
