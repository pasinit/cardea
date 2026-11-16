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

import httpx2
from cardea_vcs_ops import Vcs
from cardea_vcs_ops import descriptors as vcs_descriptors
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.fake import FakeGitHub
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

import cardea
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
PLANTED = "PLANTED-DEMO-SECRET-not-a-real-key"
TOKEN = "fake-host-token-" + "rehearsal" * 2  # the fake host's test token; never real
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
    pooled = (g for p in group.spec.pools for g in p.grants)
    rights = {*RIGHTS, "models.invoke", "groups.manage", *pooled}
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
            "platform": [
                {"principal_id": "platform", "operation": r, **grant} for r in sorted(rights)
            ],
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
    entry = implicit_entry("rehearsal-model")
    catalog = Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
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


# The team's steps, as scripts


async def transition(client: ClientSession, item: str, name: str, **extra: Any) -> Any:
    return await call(
        client, "records-transition", collection="work", record_id=item, transition=name, **extra
    )


async def tell_lead(client: ClientSession, key: str, text: str) -> Any:
    return await call(
        client,
        "mailbox-send",
        client_key=key,
        to={"instance": LEAD},
        parts=[{"text": text}],
    )


def file_work(title: str, body: str) -> Script:
    async def script(client: ClientSession, request: SessionRequest) -> Any:
        # The product manager's scripted test pass found this; it files it as work.
        return await call(
            client,
            "records-create",
            collection="work",
            client_key=title.lower().replace(" ", "-")[:64],
            data={"title": title, "body": body},
        )

    return script


def assign(item: str) -> Script:
    async def script(client: ClientSession, request: SessionRequest) -> Any:
        found = await call(client, "records-query", collection="work", state="open")
        record = next(r for r in found["records"] if r["record_id"] == item)
        await call(
            client,
            "records-update",
            collection="work",
            record_id=item,
            expected_version=record["version"],
            data={**record["data"], "assignee_pool": f"{GROUP}.dev"},
        )
        moved = await transition(client, item, "triage", args={"assignee_pool": f"{GROUP}.dev"})
        await call(
            client,
            "mailbox-send",
            client_key=f"assign-{item}",
            to={"pool": f"{GROUP}.dev"},
            parts=[{"text": f"{item} is ready for you."}],
        )
        return moved

    return script


def implement(item: str, files: dict[str, str], notes: dict[str, Any]) -> Script:
    """A developer's run: claim the item, code it, push, open the change and submit; or,
    when anything refuses, tell the team lead. Either satisfies the pool's `any_of`."""

    async def script(client: ClientSession, request: SessionRequest) -> Any:
        started = await transition(client, item, "start")
        if "error" in started:
            notes["start_refused"] = started["error"]
            return await tell_lead(client, f"busy-{item}", f"Could not take {item}.")
        epoch = started["claim"]["claim_epoch"]
        task = json.dumps({"write": files})
        session = await call(
            client, "code-session", repository="app", base_ref="main", task=task, task_key=item
        )
        notes["session"] = session
        (artifact,) = [a for a in session["artifacts"] if a["name"] == "change.bundle"]
        branch = f"agent/{GROUP}/{item}"
        pushed = await call(
            client,
            "vcs-push_branch",
            repository="app",
            branch=branch,
            change=artifact,
            head=session["head_sha"],
            record_id=item,
        )
        if "error" in pushed:
            notes["push_refused"] = pushed["error"]
            return await tell_lead(client, f"blocked-{item}", f"{item} is blocked: push refused.")
        opened = await call(
            client,
            "vcs-open_change",
            repository="app",
            head=branch,
            base="main",
            title=f"Fix {item}",
            body="Implements the work item.",
            # Names the record, so `submit` can cite this call as its evidence.
            record_id=item,
        )
        notes["change"] = opened
        return await transition(client, item, "submit", claim_epoch=epoch)

    return script


def review(item: str, number: int, notes: dict[str, Any], *, approve: bool = True) -> Script:
    """The principal engineer reads the change, reviews it and moves the item."""

    async def script(client: ClientSession, request: SessionRequest) -> Any:
        notes["read"] = await call(client, "vcs-read_change", repository="app", number=number)
        notes["approve_review"] = await call(
            client,
            "vcs-review",
            repository="app",
            number=number,
            event="APPROVE",
            body="Looks good.",
            client_key=f"approve-{item}",
        )
        notes["review"] = await call(
            client,
            "vcs-review",
            repository="app",
            number=number,
            event="COMMENT",
            body="Reviewed: the change does what the item asks.",
            client_key=f"review-{item}",
        )
        return await transition(client, item, "approve" if approve else "request_changes")

    return script


def security_review(item: str, number: int, notes: dict[str, Any]) -> Script:
    async def script(client: ClientSession, request: SessionRequest) -> Any:
        notes["security_read"] = await call(
            client, "vcs-read_change", repository="app", number=number
        )
        return await call(
            client,
            "vcs-review",
            repository="app",
            number=number,
            event="COMMENT",
            body="Security review: no secrets, no new network access.",
            client_key=f"security-{item}",
        )

    return script


def owner_merges(world: World, number: int) -> None:
    """Outside the team: the owner merges the pull request on the host (simulated)."""
    repo = world.host.repositories["acme/app"]
    pull = next(p for p in repo.pulls if p["number"] == number)
    git(world.repo_path, "update-ref", "refs/heads/main", str(repo.head(pull["head"]["ref"])))
    pull.update({"state": "closed", "merged": True})


# Checks at the end


def group_overview(world: World) -> dict[str, Any]:
    response = TestClient(create_app(world.kernel)).get(
        f"/v0.2/groups/{GROUP}/overview",
        params={"namespace": NAMESPACE, "events": 5},
        headers={"Authorization": f"Bearer {SUPERVISOR}", "X-Cardea-Auth": "spiffe"},
    )
    assert response.status_code == 200, response.json()
    view = cast(dict[str, Any], response.json())
    print(f"  Overview of {GROUP} (generation {view['generation']}):")
    for pool in view["pools"]:
        runs = " ".join(
            f"{m['principal_id'].rsplit('.', 1)[1]}:{(m['last_run'] or {}).get('outcome', '-')}"
            for m in pool["members"]
        )
        print(f"    pool {pool['name']:<9} last runs {runs}")
    for collection in view["collections"]:
        counts = ", ".join(f"{k} {v}" for k, v in collection["by_state"].items()) or "empty"
        claimed = len(collection["claimed"])
        print(f"    collection {collection['name']}: {counts}; {claimed} claimed")
    print(f"    spend: {json.dumps(view.get('spend'), sort_keys=True)[:160]}")
    return view


def claim_holders(world: World) -> dict[str, list[str]]:
    """Each record's claim holders in order, from its history. A claim is taken by a
    `claimed` event or a transition with `claim: acquire`, and given up by `released`,
    `claim_expired` or a transition with `claim: release`. Taking one while another is
    held would be a double claim."""
    holders: dict[str, list[str]] = {}
    held: dict[str, str | None] = {}
    with world.kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT record_id, kind, actor, detail_json FROM record_events ORDER BY event_id"
        ).all()
    for record_id, kind, actor, detail in rows:
        effect = (json.loads(detail) or {}).get("claim") if kind == "transitioned" else None
        if kind == "claimed" or effect == "acquire":
            assert held.get(record_id) is None, f"double claim of {record_id} by {actor}"
            held[record_id] = actor
            holders.setdefault(record_id, []).append(actor)
        elif kind in {"released", "claim_expired"} or effect == "release":
            held[record_id] = None
    return holders


def check(world: World) -> dict[str, int]:
    """No double claim; every run has receipts and costs; the gate refused one change."""
    claimed = claim_holders(world)
    with world.kernel.database.read() as conn:
        for task_id in world.tasks:
            receipts = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,)
            ).scalar_one()
            costs = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM run_attempt_costs WHERE task_id=? AND cost_usd IS NOT NULL",
                (task_id,),
            ).scalar_one()
            assert receipts >= 1 and costs >= 1, (task_id, receipts, costs)
        refused = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM operations WHERE json_extract(snapshot,'$.request.name')="
            "'vcs.push_branch' AND json_extract(snapshot,'$.failure.code')='content_refused'"
        ).scalar_one()
    assert refused == 1, refused
    claims = sum(map(len, claimed.values()))
    return {"runs": len(world.tasks), "gate_refusals": refused, "claims": claims}


# The rehearsal


def rehearse(world: World) -> dict[str, Any]:
    print("Dev team (examples/teams/dev-team): milestone 1 dress rehearsal")
    configure(world)
    _, made = run(world, f"{GROUP}.product.0", file_work("Login retry is flaky", "Fails 1 in 5."))
    item = made["record"]["record_id"]
    _, leaked = run(
        world, f"{GROUP}.product.0", file_work("Add an API client", "For the billing service.")
    )
    second = leaked["record"]["record_id"]
    print(f"  product's test pass filed {item} and {second}")
    for work in (item, second):
        _, out = run(world, LEAD, assign(work))
        print(f"  the lead assigned {work} to the developers: {out['record']['state']}")

    notes: dict[str, Any] = {}
    state, out = run(world, f"{GROUP}.dev.0", implement(item, {"retry.py": "RETRIES = 2\n"}, notes))
    assert state == "succeeded" and out["record"]["state"] == "in_review", (state, out, notes)
    number = int(notes["change"]["number"])
    print(f"  dev.0 claimed {item}, coded it, pushed and opened #{number}: in_review")
    race: dict[str, Any] = {}
    state, _ = run(world, f"{GROUP}.dev.1", implement(item, {"x.py": "x\n"}, race))
    print(f"  dev.1 tried to take {item} too and was refused; it told the lead ({state})")
    assert "start_refused" in race and state == "succeeded"
    assert claim_holders(world)[item] == [f"{GROUP}.dev.0"]

    reviewed: dict[str, Any] = {}
    _, out = run(world, f"{GROUP}.principal.0", review(item, number, reviewed))
    assert "approve_refused" in reviewed["approve_review"]["error"]
    state = out["record"]["state"]
    print(f"  the principal read #{number}, was refused APPROVE, commented and approved: {state}")
    owner_merges(world, number)
    print(f"  the owner merged #{number} on the host (outside the team)")

    blocked: dict[str, Any] = {}
    secret = {"client.py": f"API_TOKEN = '{PLANTED}'\n"}
    state, _ = run(world, f"{GROUP}.dev.2", implement(second, secret, blocked))
    assert state == "succeeded" and "content_refused" in blocked["push_refused"], blocked
    print(f"  dev.2's change for {second} carried a secret: the gate refused the push")
    print("  dev.2 told the lead it was blocked")
    overview = group_overview(world)
    checked = check(world)
    print(
        f"  checks: {checked['runs']} runs, all with receipts and costs; no double claim; "
        f"{checked['gate_refusals']} gate refusal"
    )

    print("  restructure by configuration: 2 developers and a security pool")
    result = apply(world, "group.restructured.json")
    pools = [p["name"] for p in result["group"]["spec"]["pools"]]
    print(f"  applied generation {result['generation']}: pools {', '.join(pools)}")
    with world.kernel.database.read() as conn:
        left = conn.exec_driver_sql(
            "SELECT state FROM group_members WHERE principal_id=?", (f"{GROUP}.dev.2",)
        ).scalar_one()
        handed_back = conn.exec_driver_sql(
            "SELECT state, holder FROM records WHERE record_id=?", (second,)
        ).one()
    assert left == "removed" and tuple(handed_back) == ("ready", None), (left, handed_back)
    print(f"  dev.2 was drained ({left}); its claim on {second} was released: ready")
    _, made = run(world, f"{GROUP}.product.0", file_work("Rate-limit logins", "Too many tries."))
    third = made["record"]["record_id"]
    run(world, LEAD, assign(third))
    again: dict[str, Any] = {}
    state, out = run(world, f"{GROUP}.dev.1", implement(third, {"limit.py": "LIMIT = 5\n"}, again))
    assert out["record"]["state"] == "in_review", out
    third_number = int(again["change"]["number"])
    security: dict[str, Any] = {}
    script = security_review(third, third_number, security)
    _, reviewed_by = run(world, f"{GROUP}.security.0", script)
    assert "review_id" in reviewed_by, reviewed_by
    final: dict[str, Any] = {}
    _, out = run(world, f"{GROUP}.principal.0", review(third, third_number, final))
    print(f"  {third} went dev.1 -> security.0 review -> principal: {out['record']['state']}")
    owner_merges(world, third_number)
    return {
        "first": item,
        "blocked": second,
        "third": third,
        "overview": overview,
        "checks": check(world),
        "final_state": out["record"]["state"],
    }


def main(directory: Path | None = None) -> dict[str, Any]:
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory() as scratch, world(directory or Path(scratch)) as w:
            outcome = rehearse(w)
    finally:
        logging.disable(logging.NOTSET)
    print("The dev team ran on the real operation packages with no code of its own.")
    return outcome


if __name__ == "__main__":
    sys.exit(0 if main()["final_state"] == "done" else 1)
