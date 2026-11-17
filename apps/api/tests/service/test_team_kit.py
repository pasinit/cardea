"""The deployable dev-team kit (deploy/team-kit): its rendered documents are valid, apply
to a kernel, wake the right roles, give one claim per task, and stop on the kill switch."""

import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from cardea_coding_ops import descriptors as coding_descriptors
from cardea_env_ops import descriptors as env_descriptors
from cardea_vcs_ops import in_process_descriptors
from mcp.client.session import ClientSession
from test_agent_runs import FakeSession, call

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, journal
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.confined import Workers
from cardea.service.database import Database, Fault, borrow
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import AgentGroup, AgentRunRequest, Provisioning
from cardea.service.operations import OperationDescriptor, Registry
from cardea.service.records import CollectionDocument, apply_collection

KIT = Path(__file__).resolve().parents[4] / "deploy" / "team-kit"


def kit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("teamkit", KIT / "teamkit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def example() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((KIT / "team.host.example.json").read_text()))


def test_the_example_renders_valid_documents(tmp_path: Path) -> None:
    module = kit()
    paths = module.render(example(), tmp_path)
    assert {p.name for p in paths} >= {"group.json", "collection.work.json"}
    group = AgentGroup.model_validate_json((tmp_path / "group.json").read_text())
    assert [p.name for p in group.spec.pools] == ["pm", "tl", "dev", "pe"]
    for name in ("collection.work.json", "collection.decisions.json"):
        CollectionDocument.model_validate_json((tmp_path / name).read_text())
    # Conservative defaults: 30 runs a day for the team, a run ceiling per role.
    assert group.spec.spend is not None and group.spec.spend.runs == 30
    assert all(p.spend is not None and p.spend.runs for p in group.spec.pools)
    # Every file is private to the host owner.
    assert all(p.stat().st_mode & 0o077 == 0 for p in paths)
    vcs = json.loads((tmp_path / "vcs.team.json").read_text())
    assert vcs["credentials"]["pools"] == {"dev-team.pe": "github-reviewer"}
    assert vcs["allow_approve"] == ["dev-team.pe"]
    email = json.loads((tmp_path / "email.team.json").read_text())
    assert email["roles"]["dev-team.pm"]["recipients"] == ["owner"]


def test_the_triggers_are_the_planned_ones() -> None:
    group = kit().group_document(example())
    pools = {p["name"]: p for p in group["spec"]["pools"]}

    def kinds(name: str) -> list[Any]:
        return [t.get("schedule", {}).get("cron") or t["event"] for t in pools[name]["triggers"]]

    assert kinds("pm")[:2] == ["0 9 * * 1-5", "0 15 * * 1-5"]  # twice a day
    assert "env.try" in pools["pm"]["run"]["operations"]
    assert kinds("tl")[0] == "30 8 * * 1-5"
    assert kinds("tl")[1]["types"] == ["cardea.record.created"]
    dev = kinds("dev")
    assert {"exact": {"recordstate": "ready"}} in dev[0]["filters"]  # assigned
    assert {"exact": {"recordstate": "in_review"}} in kinds("pe")[0]["filters"]  # PR opened
    mail = {"types": ["cardea.mailbox.sent"], "subjects": ["$member", "$pool"]}
    assert all(kinds(name)[-1] == mail for name in ("pm", "tl", "dev", "pe"))
    # Only developers merge; only the PE approves, citing an approving review.
    assert "vcs.merge" in pools["dev"]["grants"] and "vcs.merge" not in pools["pe"]["grants"]
    work = kit().work_collection(example())
    approve = next(t for t in work["spec"]["transitions"] if t["name"] == "approve")
    assert approve["requires_receipt"]["result"] == "result.state == 'APPROVED'"


def test_provisioning_merges_the_team_into_the_host_document() -> None:
    module = kit()
    base = {
        "principals": [
            {
                "principal_id": "runtime",
                "kind": "service",
                "auth_kind": "spiffe",
                "issuer": "i",
                "subject": "runtime",
            }
        ],
        "permissions": [
            {
                "principal_id": "sup-dev",
                "namespace": "dev",
                "operation": "stale",
                "expires_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "principal_id": "other",
                "namespace": "ops",
                "operation": "objects.read",
                "expires_at": "2026-01-01T00:00:00+00:00",
            },
        ],
        "platform": [],
    }
    host = example()
    merged = module.merge_provisioning(base, module.provisioning_fragment(host), "dev")
    assert [p["principal_id"] for p in merged["principals"]] == ["runtime", "sup-dev"]
    ops = {p["operation"] for p in merged["permissions"] if p["principal_id"] == "sup-dev"}
    assert "stale" not in ops and {"groups.manage", "env.try", "vcs.merge"} <= ops
    assert any(p["principal_id"] == "other" for p in merged["permissions"])
    paused = module.merge_provisioning(base, module.provisioning_fragment(host, paused=True), "dev")
    assert paused["principals"][-1]["disabled"] is True


# Against a kernel


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Profiles:
    """A helper with the coding and try profiles installed; nothing here starts a worker."""

    def profiles(self) -> dict[str, Any]:
        return {
            "profiles": {
                name: {"wall_seconds": 3600, "network": {"policy": "none"}, "sha256": name}
                for name in ("coding", "try")
            },
            "workers": {"code-session": ["coding"], "env-try": ["try"]},
            "sha256": "config",
        }


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        return Proof("spiffe", "test", token, time.time() + 60)


def host() -> dict[str, Any]:
    """The example without a harness or model, so the implicit test harness runs it."""
    document = example()
    for key in ("harness", "model", "email"):
        document.pop(key, None)
    document["supervisor"] = {
        "principal_id": "sup-dev",
        "kind": "human",
        "auth_kind": "spiffe",
        "issuer": "test",
        "subject": "sup-dev",
    }
    return document


def registry() -> Registry:
    from cardea_vcs_ops import Vcs
    from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
    from cardea_vcs_ops.fake import FakeGitHub
    from pydantic import SecretStr

    confined = tuple(
        OperationDescriptor.model_validate_json(p.read_text()) for p in coding_descriptors()
    )
    vcs = Vcs(
        VcsConfig(credentials=FixedTokenConfig(kind="test", token=SecretStr("t" * 16))),
        transport=FakeGitHub(Path("/nonexistent")).transport(),
    )
    return Registry((*in_process_descriptors(), *confined, *env_descriptors()), vcs.handlers())


@pytest.fixture
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Kernel, FakeSession]]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    module = kit()
    document = host()
    base = {
        "principals": [
            {
                "principal_id": "runtime",
                "kind": "service",
                "auth_kind": "spiffe",
                "issuer": "test",
                "subject": "runtime",
            }
        ],
        "permissions": [],
        "platform": [],
    }
    installed = registry()
    database = Database(tmp_path / "data")
    database.migrate()
    merged = module.merge_provisioning(base, module.provisioning_fragment(document), "dev")
    provision(database, Provisioning.model_validate(merged), installed)
    session = FakeSession()
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    kernel = Kernel(
        database,
        cast(Authentication, Auth()),
        operations=installed,
        harnesses=harnesses,
        workers=Workers(cast(Any, Profiles())),
    )
    with database.engine.begin() as conn:
        for collection in (module.work_collection(document), module.decisions_collection(document)):
            apply_collection(
                conn, "sup-dev", CollectionDocument.model_validate(collection), dry_run=False
            )
    apply(
        kernel,
        kernel.auth.validate("sup-dev"),
        "dev-team",
        AgentGroup.model_validate(module.group_document(document)),
        False,
    )
    try:
        yield kernel, session
    finally:
        database.close()


def member_run(kernel: Kernel, member: str, trigger: str = "t0") -> str:
    with kernel.database.read() as conn:
        definition = conn.exec_driver_sql(
            "SELECT definition_json FROM triggers WHERE trigger_id=?", (f"{member}.{trigger}",)
        ).scalar_one()
    template = json.loads(definition)["template"]
    request = AgentRunRequest.model_validate(
        {**template, "request_id": f"{member}.run-{time.monotonic_ns()}"}
    )
    return kernel.submit(kernel.auth.validate("sup-dev"), request).task.task_id


def admitted(kernel: Kernel, agent: str) -> list[str]:
    with kernel.database.read() as conn:
        return [
            str(r[0])
            for r in conn.exec_driver_sql(
                "SELECT task_id FROM task_bindings WHERE agent_id=? ORDER BY rowid", (agent,)
            )
        ]


def test_an_assignment_wakes_the_developers_and_only_one_claims_it(
    team: tuple[Kernel, FakeSession],
) -> None:
    kernel, session = team
    created: dict[str, Any] = {}

    async def lead(client: ClientSession, request: SessionRequest) -> SessionResult:
        made = await call(
            client, "records-create", collection="work", client_key="k1", data={"title": "Fix it"}
        )
        assert not made.is_error, made.content
        record = made.structured_content["record"]
        created.update(record)
        moved = await call(
            client,
            "records-transition",
            collection="work",
            record_id=record["record_id"],
            transition="triage",
        )
        assert not moved.is_error, moved.content
        return SessionResult(text="Triaged.", is_error=False)

    session.scripts = [lead]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.tl.0")) == "succeeded"
    journal.Dispatcher(kernel).tick()
    runs = [*admitted(kernel, "dev-team.dev.0"), *admitted(kernel, "dev-team.dev.1")]
    assert len(runs) == 2  # both developers woke for the ready item
    assert admitted(kernel, "dev-team.pe.0") == []  # nothing in review yet
    outcomes: list[bool] = []

    async def developer(client: ClientSession, request: SessionRequest) -> SessionResult:
        started = await call(
            client,
            "records-transition",
            collection="work",
            record_id=created["record_id"],
            transition="start",
        )
        outcomes.append(not started.is_error)
        return SessionResult(text="Done.", is_error=False)

    session.scripts = [developer, developer]
    for task_id in runs:
        AgentRunRuntime(kernel).execute(task_id)
    assert sorted(outcomes) == [False, True]  # one claim per task
    with kernel.database.read() as conn:
        holder = conn.exec_driver_sql(
            "SELECT holder FROM records WHERE record_id=?", (created["record_id"],)
        ).scalar_one()
    assert holder in {"dev-team.dev.0", "dev-team.dev.1"}
    # The same event never wakes them twice.
    journal.Dispatcher(kernel).tick()
    assert len([*admitted(kernel, "dev-team.dev.0"), *admitted(kernel, "dev-team.dev.1")]) == 2


def test_the_kill_switch_refuses_every_new_run(team: tuple[Kernel, FakeSession]) -> None:
    kernel, _ = team
    module = kit()
    base = {
        "principals": [
            {
                "principal_id": "runtime",
                "kind": "service",
                "auth_kind": "spiffe",
                "issuer": "test",
                "subject": "runtime",
            }
        ],
        "permissions": [],
        "platform": [],
    }
    paused = module.merge_provisioning(
        base, module.provisioning_fragment(host(), paused=True), "dev"
    )
    provision(kernel.database, Provisioning.model_validate(paused), kernel.operations)
    with pytest.raises(Fault) as refused:
        member_run(kernel, "dev-team.tl.0")
    assert refused.value.code == "principal_disabled"
    resumed = module.merge_provisioning(base, module.provisioning_fragment(host()), "dev")
    provision(kernel.database, Provisioning.model_validate(resumed), kernel.operations)
    assert member_run(kernel, "dev-team.tl.0")


def test_the_team_stops_at_its_daily_run_ceiling(team: tuple[Kernel, FakeSession]) -> None:
    kernel, _ = team
    # The PM's role ceiling is 4 runs a day in the example.
    for _ in range(4):
        member_run(kernel, "dev-team.pm.0", "t2")
        with kernel.database.engine.begin() as conn, borrow(conn):
            conn.exec_driver_sql(
                "UPDATE tasks SET state='succeeded' WHERE task_id IN "
                "(SELECT task_id FROM task_bindings WHERE agent_id='dev-team.pm.0')"
            )
    with pytest.raises(Fault) as refused:
        member_run(kernel, "dev-team.pm.0", "t2")
    assert (
        refused.value.code == "budget_exhausted" and "pool pm run ceiling" in refused.value.message
    )


def test_status_summarises_the_overview(team: tuple[Kernel, FakeSession]) -> None:
    from fastapi.testclient import TestClient

    from cardea.service.app import create_app

    kernel, _ = team
    view = TestClient(create_app(kernel)).get(
        "/v0.2/groups/dev-team/overview",
        params={"namespace": "dev"},
        headers={"Authorization": "Bearer sup-dev"},
    )
    assert view.status_code == 200, view.text
    lines = kit().summarise(view.json())
    assert any(line.strip().startswith("dev-team.dev.0: idle") for line in lines), lines
    assert any("spend group" in line and "runs 0/30" in line for line in lines), lines
    assert any("spend pool:pm" in line and "runs 0/4" in line for line in lines), lines
