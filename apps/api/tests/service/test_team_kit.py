"""The deployable dev-team kit (deploy/team-kit): its rendered documents are valid, apply
to a kernel, wake the right roles, give one claim per task, and stop on the kill switch."""

import importlib.util
import json
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from cardea_coding_ops import descriptors as coding_descriptors
from cardea_env_ops import descriptors as env_descriptors
from cardea_vcs_ops import in_process_descriptors
from mcp.client.session import ClientSession
from test_agent_runs import FakeSession, call
from test_vcs_ops import text

from cardea.kernel.models import DispatchLease, TaskState
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, claims, journal
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.confined import Workers
from cardea.service.database import Database, Fault, borrow
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import AgentGroup, AgentRunRequest, Provisioning, Signal, now
from cardea.service.operations import OperationDescriptor, OperationRejected, Registry
from cardea.service.policy import Policy
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
    assert email["roles"]["dev-team.pm"]["recipients"] == ["client"]


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


def test_developers_get_env_try_by_default_and_not_when_nothing_needs_it() -> None:
    def dev(host: dict[str, Any]) -> dict[str, Any]:
        pools = {p["name"]: p for p in kit().group_document(host)["spec"]["pools"]}
        pool: dict[str, Any] = pools["dev"]
        return pool

    # The merge check defaults to the "try" scenario, so developers get env.try.
    default = {k: v for k, v in example().items() if k != "env_try"}
    assert "env.try" in dev(default)["grants"]
    without = {**example(), "env_try": {"scenarios": ["try"], "merge_scenarios": []}}
    assert "env.try" not in dev(without)["grants"]
    assert "env.try" not in dev(without)["run"]["operations"]
    assert "env.try" not in dev(without)["run"]["instructions"]
    with_check = {**example(), "env_try": {"scenarios": ["try"], "dev_scenarios": ["check"]}}
    pool = dev(with_check)
    assert "env.try" in pool["grants"] and "env.try" in pool["run"]["operations"]
    assert "Before every submit, run env.try" in pool["run"]["instructions"]
    assert 'scenarios ["check"]' in pool["run"]["instructions"]


def test_developer_and_pe_schedules_are_opt_in() -> None:
    def crons(host: dict[str, Any], name: str) -> list[str]:
        pools = {p["name"]: p for p in kit().group_document(host)["spec"]["pools"]}
        return [t["schedule"]["cron"] for t in pools[name]["triggers"] if "schedule" in t]

    assert crons(example(), "dev") == [] and crons(example(), "pe") == []
    host = {**example(), "schedules": {"dev": ["*/20 * * * *"], "pe": ["5-59/20 * * * *"]}}
    assert crons(host, "dev") == ["*/20 * * * *"]
    assert crons(host, "pe") == ["5-59/20 * * * *"]
    assert crons(host, "tl") == ["30 8 * * 1-5"]  # a partial override keeps the defaults
    # Trigger ids are positional: the fixed triggers keep their places when a schedule is
    # added, so an applied team can turn the schedule on (the kernel refuses kind changes).
    before = {p["name"]: p["triggers"] for p in kit().group_document(example())["spec"]["pools"]}
    after = {p["name"]: p["triggers"] for p in kit().group_document(host)["spec"]["pools"]}
    for name in ("dev", "pe"):
        assert after[name][: len(before[name])] == before[name]


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


def test_role_instructions_name_the_repository_and_branch_from_the_host_file() -> None:
    document = {**example(), "repository": "widgets", "default_branch": "trunk"}
    group = kit().group_document(document)
    instructions = {p["name"]: p["run"]["instructions"] for p in group["spec"]["pools"]}
    # Every role with repository-scoped operations is told the exact alias it was granted.
    for name in ("pm", "dev", "pe"):
        assert "The repository is `widgets`: pass exactly that" in instructions[name], name
        assert "default branch is `trunk`" in instructions[name], name
    assert "(`repo` for env.try)" in instructions["pm"]
    assert "the head of trunk" in instructions["pm"] and "main" not in instructions["pm"]
    assert "widgets" not in instructions["tl"]  # no repository operations
    # Without the field the default branch is main.
    plain = kit().group_document({**example(), "repository": "widgets"})
    pm = next(p for p in plain["spec"]["pools"] if p["name"] == "pm")
    assert "default branch is `main`" in pm["run"]["instructions"]


def pools_of(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in kit().group_document(document)["spec"]["pools"]}


def test_the_product_goals_reach_the_product_manager_and_in_short_the_team_lead() -> None:
    module = kit()
    document = example()
    goals = "Serve small teams.\n" + "\n".join(f"- Outcome {i}: measurable." for i in range(80))
    document["product"] = {"goals": goals, "non_goals": "- No mobile app."}
    pools = pools_of(document)
    pm, tl = pools["pm"]["run"]["instructions"], pools["tl"]["run"]["instructions"]
    assert pm.endswith("Non-goals (never build these):\n- No mobile app.")
    assert "Product goals (from the host owner):\n" + goals in pm
    # It designs and decides features itself, filed with problem, behaviour and criteria.
    assert "without waiting for approval" in pm
    assert all(w in pm for w in ("user problem", "proposed behaviour", "acceptance criteria"))
    # The team lead gets the first lines only, within the summary bound.
    assert "Serve small teams." in tl and "Outcome 79" not in tl and "No mobile app" not in tl
    summary = tl.split("Product goals, in short (prioritise by them):\n", 1)[1]
    assert len(summary.encode()) <= module.MAX_GOALS_SUMMARY
    assert summary.endswith("(shortened; the product manager has the full goals)")
    # Without goals, the product manager only tests, and nothing about goals is rendered.
    del document["product"]
    plain = pools_of(document)
    assert "Product goals" not in plain["pm"]["run"]["instructions"]
    assert "Product goals" not in plain["tl"]["run"]["instructions"]
    assert "without waiting for approval" not in plain["pm"]["run"]["instructions"]


def test_goals_can_come_from_a_file_beside_the_host_file(tmp_path: Path) -> None:
    module = kit()
    document = example()
    document["product"] = {"goals_file": "goals.md"}
    (tmp_path / "goals.md").write_text("# Goals\nShip the importer.\n")
    host = tmp_path / "team.host.json"
    host.write_text(json.dumps(document))
    loaded = module.load_host(host)
    assert loaded["product"]["goals_file"] == str(tmp_path / "goals.md")
    pm = pools_of(loaded)["pm"]["run"]["instructions"]
    assert pm.endswith("Product goals (from the host owner):\n# Goals\nShip the importer.")


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"goals": "x" * 8193}, "over 8192 bytes"),
        ({"goals": "x" * 6000, "non_goals": "y" * 3000}, "over 8192 bytes"),
        ({"goals_file": "big.md"}, "over 8192 bytes"),
        ({"goals_file": "missing.md"}, "cannot be read"),
        ({"goals": "a", "goals_file": "goals.md"}, "exactly one"),
        ({"non_goals": "a"}, "exactly one"),
        ({"goals": "   "}, "non-empty"),
        ({"goals": "ok\x1b[31m"}, "control characters"),
        ({"goals": "ok", "owner": "me"}, "takes goals or goals_file"),
        ("ship it", "takes goals or goals_file"),
    ],
)
def test_oversize_or_malformed_goals_are_refused(
    tmp_path: Path, section: object, message: str
) -> None:
    module = kit()
    (tmp_path / "big.md").write_text("g" * 9000)
    document = {**example(), "product": section}
    host = tmp_path / "team.host.json"
    host.write_text(json.dumps(document))
    with pytest.raises(module.HostError, match=message):
        module.load_host(host)


def test_the_email_policy_is_rendered_only_when_the_pm_may_email() -> None:
    document = example()
    pm = pools_of(document)["pm"]
    assert "email.send" in pm["grants"] and "email.send" in pm["run"]["operations"]
    text = pm["run"]["instructions"]
    assert "email.send to `client`" in text and '"X or Y?"' in text
    assert "never wait for it and never let it block work" in text
    assert "Never email status reports" in text and "at most 3 emails every 7 days" in text
    del document["email"]
    plain = pools_of(document)["pm"]
    assert "email.send" not in plain["grants"] and "email.send" not in plain["run"]["operations"]
    assert "email" not in plain["run"]["instructions"].lower()
    # Nobody but the product manager is told to email the client.
    others = pools_of(example())
    assert all("email.send" not in others[n]["run"]["instructions"] for n in ("tl", "dev", "pe"))


def test_the_host_file_refuses_an_invalid_default_branch(tmp_path: Path) -> None:
    path = tmp_path / "team.host.json"
    path.write_text(json.dumps({**example(), "default_branch": "no spaces"}))
    module = kit()
    with pytest.raises(module.HostError, match="default_branch"):
        module.load_host(path)


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
        VcsConfig(
            credentials=FixedTokenConfig(kind="test", token=SecretStr("t" * 16)),
        ),
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


def ready_item(kernel: Kernel, session: FakeSession, title: str = "Fix it") -> str:
    """A work item the team lead created and triaged; its record ID."""
    created: dict[str, Any] = {}

    async def lead(client: ClientSession, request: SessionRequest) -> SessionResult:
        made = await call(
            client, "records-create", collection="work", client_key=title, data={"title": title}
        )
        assert not made.is_error, made.content
        created.update(made.structured_content["record"])
        moved = await call(
            client,
            "records-transition",
            collection="work",
            record_id=created["record_id"],
            transition="triage",
        )
        assert not moved.is_error, moved.content
        return SessionResult(text="Triaged.", is_error=False)

    session.scripts = [lead]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.tl.0")) == "succeeded"
    return str(created["record_id"])


@pytest.mark.loopback
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


def test_provision_and_apply_refuse_placeholder_values(tmp_path: Path) -> None:
    module = kit()
    document = example()
    with pytest.raises(module.HostError, match="credentials/reviewer_login"):
        module.provision(document, paused=False)
    with pytest.raises(module.HostError, match="placeholder"):
        module.apply(document)
    document["credentials"]["reviewer_login"] = "real-login"
    document["supervisor"]["issuer"] = "https://issuer.example.invalid"
    assert module.placeholders(document) == ["/supervisor/issuer"]
    document["supervisor"]["issuer"] = "cardea.internal"
    assert module.placeholders(document) == []


@pytest.mark.loopback
def test_a_refused_repository_call_records_the_missing_grant(
    team: tuple[Kernel, FakeSession], logs: pytest.LogCaptureFixture
) -> None:
    kernel, session = team

    async def guesses(client: ClientSession, request: SessionRequest) -> SessionResult:
        refused = await call(client, "vcs-read_branch", repository="guessed", branch="main")
        assert refused.is_error and "task_grant_denied" in str(refused.content)
        return SessionResult(text="Could not read.", is_error=False)

    session.scripts = [guesses]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.pm.0")) == "succeeded"
    (event,) = [r for r in logs.records if r.getMessage() == "tool call refused"]
    assert event.__dict__["code"] == "task_grant_denied"
    assert event.__dict__["grant"] == "repo:guessed" and event.__dict__["holder"] == "task"


# Kill switch, oversight, wall time, mail addresses


def provision_team(kernel: Kernel, *, paused: bool) -> None:
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
    fragment = module.provisioning_fragment(host(), paused=paused)
    document = module.merge_provisioning(base, fragment, "dev")
    provision(kernel.database, Provisioning.model_validate(document), kernel.operations)


def test_disabling_the_supervisor_cancels_its_unfinished_runs(
    team: tuple[Kernel, FakeSession],
) -> None:
    kernel, session = team
    queued = member_run(kernel, "dev-team.tl.0")
    second = member_run(kernel, "dev-team.dev.0")
    # An attempt that lost its worker: running, with a lease that has expired.
    interrupted = member_run(kernel, "dev-team.dev.1")
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(interrupted)
        start = now()
        tx.change_task(
            interrupted,
            TaskState.RUNNING,
            expected_revision=task.revision,
            at=start,
            lease=DispatchLease(
                lease_id="lease-lost",
                worker_id="runtime",
                acquired_at=start,
                expires_at=start + timedelta(milliseconds=50),
            ),
        )
    time.sleep(0.1)
    provision_team(kernel, paused=True)
    with kernel.database.read() as conn:
        requests = conn.exec_driver_sql(
            "SELECT kind,request_id FROM control_requests WHERE task_id=?", (queued,)
        ).all()
    assert [(kind, rid.split(".")[0]) for kind, rid in requests] == [("cancel", "disabled")]
    # Nothing has to run for the status to be true: no runtime picks these up while the
    # supervisor is disabled, so the cancel itself makes them terminal.
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        states = [tx.get_task(t).state for t in (queued, second, interrupted)]
    assert states == [TaskState.CANCELLED] * 3
    # A runtime that reaches a run later finds it settled and starts no session.
    assert AgentRunRuntime(kernel).execute(queued) == "cancelled"
    assert session.requests == []
    provision_team(kernel, paused=True)
    with kernel.database.read() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM control_requests WHERE task_id=?", (queued,)
            ).scalar_one()
            == 1
        )


@pytest.mark.loopback
def test_a_holder_interrupted_by_the_kill_switch_is_woken_after_resume(
    team: tuple[Kernel, FakeSession],
) -> None:
    """A run the pause cancelled leaves its holder's claim idle; once the team is resumed,
    the next tick wakes the holder for it, without waiting for half the lease."""
    kernel, session = team
    record_id = ready_item(kernel, session)

    async def start(client: ClientSession, request: SessionRequest) -> SessionResult:
        started = await call(
            client,
            "records-transition",
            collection="work",
            record_id=record_id,
            transition="start",
        )
        assert not started.is_error, started.content
        return SessionResult(text="Started.", is_error=False)

    session.scripts = [start]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.dev.0")) == "succeeded"
    interrupted = member_run(kernel, "dev-team.dev.0", "t1")
    provision_team(kernel, paused=True)
    at = now() + timedelta(minutes=1)
    assert claims.followups(kernel, at) == []  # nothing is admitted while paused
    provision_team(kernel, paused=False)
    woken = claims.followups(kernel, at)
    assert len(woken) == 1 and woken[0] != interrupted
    assert admitted(kernel, "dev-team.dev.0")[-1] == woken[0]
    # One wake per interruption: the follow-up is now the holder's latest run.
    assert claims.followups(kernel, at) == []


@pytest.mark.loopback
def test_submit_records_the_pull_request_and_reviews_drive_the_item(
    team: tuple[Kernel, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit` copies the opened pull request's number onto the item (data.change), where
    the PE reads it; `request_changes`, like `approve`, needs the PE's matching review."""
    kernel, session = team
    record_id = ready_item(kernel, session)
    events: list[str] = []

    async def opened(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        return {"number": 42, "url": "https://git.example/pull/42", "state": "open", "head": "b"}

    async def reviewed(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        events.append(arguments["event"])
        state = {"REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}
        return {
            "review_id": len(events),
            "state": state.get(arguments["event"], "COMMENTED"),
            "url": "https://git.example/pull/42#r",
        }

    for name, fake in (("vcs.open_change", opened), ("vcs.review", reviewed)):
        handler = kernel.operations.get(name).cardea.handler
        monkeypatch.setitem(kernel.operations.handlers, str(handler), fake)

    async def develop(client: ClientSession, request: SessionRequest) -> SessionResult:
        for transition in ("start", "submit"):
            if transition == "submit":
                made = await call(
                    client,
                    "vcs-open_change",
                    repository="cardea",
                    head="agent/dev-team/fix",
                    base="main",
                    title="Fix it",
                    record_id=record_id,
                )
                assert not made.is_error, made.content
            moved = await call(
                client,
                "records-transition",
                collection="work",
                record_id=record_id,
                transition=transition,
                **({"claim_epoch": 1} if transition == "submit" else {}),
            )
            assert not moved.is_error, moved.content
        return SessionResult(text="Submitted.", is_error=False)

    session.scripts = [develop]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.dev.0")) == "succeeded"
    with kernel.database.read() as conn:
        state, data = conn.exec_driver_sql(
            "SELECT state, data_json FROM records WHERE record_id=?", (record_id,)
        ).one()
    assert state == "in_review" and json.loads(data) == {"title": "Fix it", "change": 42}
    refusals: list[str] = []

    async def review(client: ClientSession, request: SessionRequest) -> SessionResult:
        async def request_changes() -> Any:
            return await call(
                client,
                "records-transition",
                collection="work",
                record_id=record_id,
                transition="request_changes",
            )

        refusals.append(text(await request_changes()))  # no review yet
        for event in ("COMMENT", "REQUEST_CHANGES"):
            made = await call(
                client,
                "vcs-review",
                repository="cardea",
                number=42,
                event=event,
                body="Please add a test.",
                client_key=f"review-{event}",
                record_id=record_id,
            )
            assert not made.is_error, made.content
            moved = await request_changes()
            if event == "COMMENT":
                refusals.append(text(moved))  # a comment is not a changes-requested review
            else:
                assert not moved.is_error, moved.content
        return SessionResult(text="Changes requested.", is_error=False)

    session.scripts = [review]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.pe.0")) == "succeeded"
    assert "receipt_required" in refusals[0] and "receipt_result_refused" in refusals[1]
    assert events == ["COMMENT", "REQUEST_CHANGES"]
    with kernel.database.read() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT state FROM records WHERE record_id=?", (record_id,)
            ).scalar_one()
            == "in_progress"
        )
    pe = next(p for p in kit().group_document(host())["spec"]["pools"] if p["name"] == "pe")
    assert "data.change" in pe["run"]["instructions"]


def test_the_pe_approves_only_over_passing_checks() -> None:
    """The kernel refuses the approval (test_vcs_approve_checks.py); the instructions tell
    the PE what to do in each case."""
    pools = kit().group_document(host())["spec"]["pools"]
    text = " ".join(next(p for p in pools if p["name"] == "pe")["run"]["instructions"].split())
    assert "every check on the pull request's head has passed" in text
    assert "approve_checks_pending" in text and "wait for your next run" in text
    assert "name the failing check" in text and "or say that no check ran" in text


@pytest.mark.loopback
def test_events_refused_while_paused_wake_the_members_on_resume(
    team: tuple[Kernel, FakeSession],
) -> None:
    kernel, session = team
    ready_item(kernel, session)
    provision_team(kernel, paused=True)
    # The item became ready, but the team is paused: refused, not consumed.
    journal.Dispatcher(kernel).tick()
    journal.Dispatcher(kernel).tick()
    developers = ("dev-team.dev.0", "dev-team.dev.1")
    assert [admitted(kernel, d) for d in developers] == [[], []]
    with kernel.database.read() as conn:
        refusals = conn.exec_driver_sql(
            "SELECT trigger_id, code FROM trigger_deliveries WHERE outcome='refused' "
            "AND trigger_id LIKE 'dev-team.dev.%' ORDER BY trigger_id"
        ).all()
    # One refusal each, however many ticks pass while paused.
    assert [tuple(r) for r in refusals] == [
        ("dev-team.dev.0.t0", "principal_disabled"),
        ("dev-team.dev.1.t0", "principal_disabled"),
    ]
    provision_team(kernel, paused=False)
    journal.Dispatcher(kernel).tick()
    assert [len(admitted(kernel, d)) for d in developers] == [1, 1]
    journal.Dispatcher(kernel).tick()  # each kept event wakes its member once
    assert [len(admitted(kernel, d)) for d in developers] == [1, 1]


def test_the_supervisor_oversees_its_members_runs(team: tuple[Kernel, FakeSession]) -> None:
    """A member run's grants hold no task rights; its supervisor still inspects, reads
    events and cancels it under its own authority, and nothing more."""
    kernel, _ = team
    task_id = member_run(kernel, "dev-team.dev.0")
    supervisor = kernel.auth.validate("sup-dev")
    assert kernel.inspect(supervisor, task_id).task.task_id == task_id
    with kernel.database.read() as conn:
        Policy(conn).access("sup-dev", task_id, "events.read")
        with pytest.raises(Fault) as resume:
            Policy(conn).access("sup-dev", task_id, "tasks.resume")
        assert resume.value.code == "task_grant_denied"
        with pytest.raises(Fault) as member:
            Policy(conn).access("dev-team.dev.0", task_id, "tasks.cancel")
        assert member.value.code == "task_grant_denied"  # the member cannot cancel itself
    cancelled = kernel.control(supervisor, task_id, Signal(request_id="stop-1"), "cancel")
    assert cancelled.task.task_id == task_id
    with kernel.database.read() as conn, pytest.raises(Fault) as outsider:
        # Another member of the group is not this run's supervisor.
        Policy(conn).access("dev-team.pe.0", task_id, "tasks.inspect")
    assert outsider.value.code == "task_not_found"


def test_pause_disables_first_even_when_the_kernel_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = kit()
    calls: list[bool] = []

    def unreachable(host: dict[str, Any]) -> dict[str, Any]:
        raise OSError("kernel socket unavailable")

    monkeypatch.setattr(module, "overview", unreachable)
    monkeypatch.setattr(module, "provision", lambda host, paused: calls.append(paused))
    assert module.pause(example()) == [] and calls == [True]
    monkeypatch.setattr(module, "client", lambda host: (_ for _ in ()).throw(OSError("down")))
    runs, problem = module.stop(example())
    assert runs == [] and problem is not None and calls == [True, True]


def test_status_shows_a_paused_team(monkeypatch: pytest.MonkeyPatch) -> None:
    from cardea.service.client import ClientError

    module = kit()

    def disabled(host: dict[str, Any]) -> dict[str, Any]:
        raise ClientError("principal_disabled", 403)

    monkeypatch.setattr(module, "overview", disabled)
    assert module.status(example())[0].startswith("team paused")
    monkeypatch.setattr(
        module, "overview", lambda host: (_ for _ in ()).throw(ClientError("x", 500))
    )
    with pytest.raises(ClientError):
        module.status(example())


def test_status_while_paused_shows_reason_tokens_and_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cardea.service.client import ClientError

    module = kit()
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"principals": [], "grants": [], "platform": []}))
    document = {
        **example(),
        "provisioning": {"base": str(base), "out": str(tmp_path / "provision.json")},
    }
    document["credentials"] = {**document["credentials"], "expires_on": "2027-01-31"}
    failed = {
        "task_id": "t-9",
        "outcome": "failed",
        "failure_code": "attempt_timeout",
        "finished_at": "2026-11-21T10:00:00Z",
    }
    view = {"pools": [{"members": [{"principal_id": "dev-team.dev.0", "last_run": failed}]}]}
    monkeypatch.setattr(module, "overview", lambda host: view)
    monkeypatch.setattr(module, "provision", lambda host, paused: None)
    module.pause(document, "rotating the \x1b[31mtoken\nnow")

    def disabled(host: dict[str, Any]) -> dict[str, Any]:
        raise ClientError("principal_disabled", 403)

    monkeypatch.setattr(module, "overview", disabled)
    text = "\n".join(module.status(document, today=date(2026, 11, 22)))
    assert text.startswith("team paused")
    assert "pause reason since " in text and "rotating the  [31mtoken now" in text
    assert "dev-team.dev.0: attempt_timeout (t-9, 2026-11-21T10:00:00Z)" in text
    assert "github, github-reviewer expire on 2027-01-31: 70 day(s) left" in text
    # The pause record holds no secrets, only the reason, time and failure codes.
    assert set(json.loads(module.pause_record_path(document).read_text())) == {
        "paused_at",
        "reason",
        "failures",
    }
    monkeypatch.setattr(module, "provision", lambda host, paused: None)
    module.resume(document)
    assert "not recorded" in "\n".join(module.status(document))


def test_a_token_within_the_warning_window_is_flagged() -> None:
    module = kit()
    document = example()
    document["credentials"] = {**document["credentials"], "expires_on": "2026-12-01"}
    (soon,) = module.token_lines(document, date(2026, 11, 25))
    assert "EXPIRES SOON, in 6 day(s)" in soon and "github-reviewer" in soon
    (outside,) = module.token_lines(document, date(2026, 11, 22))
    assert "EXPIRES SOON" not in outside and "9 day(s) left" in outside
    document["credentials"]["warn_days"] = 10
    assert "EXPIRES SOON" in module.token_lines(document, date(2026, 11, 22))[0]
    (gone,) = module.token_lines(document, date(2026, 12, 3))
    assert "EXPIRED 2 day(s) ago" in gone
    # Names and dates only: nothing else from the credentials section is printed.
    assert "reviewer-login" not in gone


def test_run_timeouts_must_fit_the_session_wall_time(tmp_path: Path) -> None:
    module = kit()
    workers = tmp_path / "workers.json"
    workers.write_text(json.dumps({"profiles": {"session": {"wall_seconds": 1800}}}))
    document = {**example(), "workers_config": str(workers)}
    problems = module.wall_problems(document)
    assert [p.split(":")[0] for p in problems] == ["dev", "pm"]
    with pytest.raises(module.HostError, match="session wall time"):
        module.check_walls(document)
    fitted = {**document, "run_timeouts": {"dev": 1700, "pm": 1800}}
    assert module.wall_problems(fitted) == []
    group = module.group_document(fitted)
    bounds = {p["name"]: p["run"]["bounds"]["timeout_seconds"] for p in group["spec"]["pools"]}
    assert bounds == {"pm": 1800, "tl": 900, "dev": 1700, "pe": 1800}
    # Without a readable workers file the check cannot run and refuses nothing.
    assert module.wall_problems({**example(), "workers_config": str(tmp_path / "none")}) == []


def test_every_role_may_mail_and_answer_the_supervisor() -> None:
    document = example()
    supervisor = document["supervisor"]["principal_id"]
    group = kit().group_document(document)
    for pool in group["spec"]["pools"]:
        text = pool["run"]["instructions"]
        assert f"mail:{supervisor}" in pool["grants"], pool["name"]
        assert "A message from the supervisor is from the owner" in text, pool["name"]
        assert f'a reply to the supervisor, `{{"instance": "{supervisor}"}}`' in text
    pm = pools_of(document)["pm"]["run"]["instructions"]
    assert "asks for a status report: then email one short report" in pm
    assert "at most 3 emails every 7 days" in pm


def test_roles_are_told_the_addresses_they_may_mail() -> None:
    group = kit().group_document(example())
    for pool in group["spec"]["pools"]:
        granted = sorted(g.removeprefix("mail:") for g in pool["grants"] if g.startswith("mail:"))
        named = sorted(
            part.split('"')[3]
            for part in pool["run"]["instructions"].split("`")
            if part.startswith(('{"pool"', '{"instance"'))
        )
        assert named == granted, pool["name"]
        assert "never to a member" in pool["run"]["instructions"]
    # Nobody mails the whole developer pool: one item's answer goes to its holder only.
    pools = {p["name"]: p for p in group["spec"]["pools"]}
    assert all("mail:dev-team.dev" not in p["grants"] for p in pools.values())
    for name in ("tl", "pe"):
        assert {"mail:dev-team.dev.0", "mail:dev-team.dev.1"} <= set(pools[name]["grants"])
        assert "mail only the developer holding it" in pools[name]["run"]["instructions"]
    assert "stop at once and send no mail" in pools["dev"]["run"]["instructions"]


@pytest.mark.loopback
def test_a_team_lead_answer_about_an_item_wakes_only_its_holder(
    team: tuple[Kernel, FakeSession],
) -> None:
    kernel, session = team
    record_id = ready_item(kernel, session)

    async def start(client: ClientSession, request: SessionRequest) -> SessionResult:
        started = await call(
            client,
            "records-transition",
            collection="work",
            record_id=record_id,
            transition="start",
        )
        assert not started.is_error, started.content
        return SessionResult(text="Started.", is_error=False)

    session.scripts = [start]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.dev.1")) == "succeeded"
    # The item's earlier events wake the developers; let those runs finish first.
    journal.Dispatcher(kernel).tick()

    async def idle(client: ClientSession, request: SessionRequest) -> SessionResult:
        return SessionResult(text="Nothing to do.", is_error=False)

    woken = [*admitted(kernel, "dev-team.dev.0"), *admitted(kernel, "dev-team.dev.1")]
    session.scripts = [idle] * len(woken)
    for task_id in woken:
        AgentRunRuntime(kernel).execute(task_id)
    before = {d: len(admitted(kernel, d)) for d in ("dev-team.dev.0", "dev-team.dev.1")}

    async def answer(client: ClientSession, request: SessionRequest) -> SessionResult:
        to_pool = await call(
            client,
            "mailbox-send",
            client_key="answer-pool",
            to={"pool": "dev-team.dev"},
            parts=[{"text": "Use the existing helper."}],
        )
        assert to_pool.is_error  # the lead holds no grant to mail every developer
        to_holder = await call(
            client,
            "mailbox-send",
            client_key="answer-holder",
            to={"instance": "dev-team.dev.1"},
            parts=[{"text": "Use the existing helper."}],
            record={"collection": "work", "record_id": record_id},
        )
        assert not to_holder.is_error, to_holder.content
        return SessionResult(text="Answered.", is_error=False)

    session.scripts = [answer]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.tl.0")) == "succeeded"
    journal.Dispatcher(kernel).tick()
    after = {d: len(admitted(kernel, d)) for d in before}
    assert after == {
        "dev-team.dev.0": before["dev-team.dev.0"],
        "dev-team.dev.1": before["dev-team.dev.1"] + 1,
    }


def test_status_reports_a_waiver_of_the_host_rules(tmp_path: Path) -> None:
    module = kit()
    registry = tmp_path / "repositories.json"
    document = {**example(), "repositories_config": str(registry)}
    assert module.host_rules_note(document) == []  # unreadable: nothing to report
    entry: dict[str, Any] = {"source": "/srv/mirror"}
    registry.write_text(json.dumps({"repositories": {document["repository"]: entry}}))
    # Rules required, no guard: only the guard's state.
    (line,) = module.host_rules_note(document)
    assert "up-to-date branch required to merge: off" in line
    entry.update(host_rules="waived", waiver_reason="No rulesets on this plan.")
    registry.write_text(json.dumps({"repositories": {document["repository"]: entry}}))
    waived, guard = module.host_rules_note(document)
    assert "WAIVED" in waived and "No rulesets on this plan." in waived
    # A waiver with no guard is loud: nothing else requires an up-to-date branch.
    assert "up-to-date branch required to merge: OFF" in guard and "require_up_to_date" in guard
    entry.update(require_up_to_date=True)
    registry.write_text(json.dumps({"repositories": {document["repository"]: entry}}))
    waived, guard = module.host_rules_note(document)
    assert "WAIVED" in waived and guard.endswith("up-to-date branch required to merge: on")


def test_developers_are_told_what_to_do_when_behind_the_base() -> None:
    module = kit()
    document = module.group_document(example())
    dev = next(p for p in document["spec"]["pools"] if p["name"] == "dev")
    instructions = dev["run"]["instructions"]
    assert "merge_behind_base" in instructions and "resubmit" in instructions
    assert "no rebase, no force-push" in instructions and "rebasing" not in instructions
    assert "record:work/resubmit" in dev["grants"]


def test_inbound_email_for_the_pm_is_opt_in() -> None:
    module = kit()
    assert "inbound" not in module.email_fragment(example())
    pm = next(p for p in module.group_document(example())["spec"]["pools"] if p["name"] == "pm")
    assert "reaches your mailbox" not in pm["run"]["instructions"]
    document = example()
    document["email"] = {
        **document["email"],
        "inbound": {"rate_limit": {"count": 5, "window_days": 1}},
    }
    fragment = module.email_fragment(document)
    assert fragment["inbound"] == {
        "from": [document["email"]["alias"]],
        "deliver_to": {"namespace": document["namespace"], "pool": "dev-team.pm"},
        "rate_limit": {"count": 5, "window_days": 1},
    }
    from cardea_email_ops.config import EmailConfig

    # The fragment fits the email host file's shape.
    EmailConfig.model_validate(
        {
            "transport": {
                "kind": "agentmail",
                "inbox_id": "team@agents.example.invalid",
                "api_key_file": "/etc/cardea/agentmail.key",
            },
            "recipients": {document["email"]["alias"]: "someone@example.invalid"},
            **fragment,
        }
    )
    pm = next(p for p in module.group_document(document)["spec"]["pools"] if p["name"] == "pm")
    assert "also reaches your mailbox as data" in pm["run"]["instructions"]


# Developers keep going, and spread out over the ready items


def test_developers_keep_working_and_move_past_a_lost_start() -> None:
    module = kit()
    pools = {p["name"]: p for p in module.group_document(example())["spec"]["pools"]}
    text = pools["dev"]["run"]["instructions"]
    # Finishing an item does not end the run while one is ready.
    assert "After a merge or a submit, go back to (3)" in text
    # Each developer starts at its own offset and moves on when it loses a race.
    assert "member $member" in text and "position n mod k" in text
    assert "try the next item" in text and "after 3 refused starts" in text
    assert "if start is refused, stop" not in text
    # A developer pushes its own branch per claim, so it is the opener of what it submits.
    assert "`agent/dev-team/<record_id>-<claim_epoch>`" in text
    assert "only the member that opened a pull request may merge it" in text
    # Items whose claim lapsed after review: in progress can be taken over, approved waits
    # for its opener.
    assert "An in-progress item no one holds" in text
    assert "an approved item no one holds" in text and "so leave it" in text
    # One item may wait in review while its developer works on the next.
    claims = module.work_collection(example())["spec"]["claims"]
    assert claims["max_per_holder"] == 2 == module.MAX_ITEMS_PER_DEVELOPER


def test_each_developer_is_told_its_own_member_id(team: tuple[Kernel, FakeSession]) -> None:
    kernel, _ = team
    with kernel.database.read() as conn:
        rows = conn.exec_driver_sql(
            "SELECT trigger_id, definition_json FROM triggers WHERE trigger_id LIKE 'dev-team.%'"
        ).all()
    told = {
        str(trigger).rsplit(".", 1)[0]: json.loads(definition)["template"]["instructions"]
        for trigger, definition in rows
    }
    assert "member dev-team.dev.0;" in told["dev-team.dev.0"]
    assert "member dev-team.dev.1;" in told["dev-team.dev.1"]
    assert not any("$member" in text for text in told.values())
    # Instructions without a placeholder are passed on unchanged.
    tl = next(p for p in kit().group_document(host())["spec"]["pools"] if p["name"] == "tl")
    assert told["dev-team.tl.0"] == tl["run"]["instructions"]


def fake_open_change(kernel: Kernel, monkeypatch: pytest.MonkeyPatch) -> None:
    async def opened(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        # One pull request per branch: `...-<epoch>` opens number 41 + epoch.
        head = str(arguments["head"])
        number = 41 + int(head.rsplit("-", 1)[1])
        url = f"https://git.example/pull/{number}"
        return {"number": number, "url": url, "state": "open", "head": head}

    handler = kernel.operations.get("vcs.open_change").cardea.handler
    monkeypatch.setitem(kernel.operations.handlers, str(handler), opened)


async def start(client: ClientSession, record_id: str) -> tuple[str, dict[str, Any]]:
    """`start` on one item: "" when it succeeded, else the refusal's text."""
    moved = await call(
        client, "records-transition", collection="work", record_id=record_id, transition="start"
    )
    return (text(moved) if moved.is_error else ""), dict(moved.structured_content or {})


@pytest.mark.loopback
def test_a_developer_starts_the_next_item_after_submitting(
    team: tuple[Kernel, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With two items per developer, one run submits an item and starts the next; another
    developer's lost starts are refused one by one and it claims what is left."""
    kernel, session = team
    fake_open_change(kernel, monkeypatch)
    items = [ready_item(kernel, session, title) for title in ("A", "B", "C")]
    refused: list[str] = []

    async def first(client: ClientSession, request: SessionRequest) -> SessionResult:
        started, output = await start(client, items[0])
        assert started == "", started
        epoch = output["claim"]["claim_epoch"]
        made = await call(
            client,
            "vcs-open_change",
            repository="cardea",
            head=f"agent/dev-team/{items[0]}-{epoch}",
            base="main",
            title="A",
            record_id=items[0],
        )
        assert not made.is_error, made.content
        moved = await call(
            client,
            "records-transition",
            collection="work",
            record_id=items[0],
            transition="submit",
            claim_epoch=epoch,
        )
        assert not moved.is_error, moved.content
        assert (await start(client, items[1]))[0] == ""  # the run goes on to the next item
        refused.append((await start(client, items[2]))[0])  # two items is the limit
        return SessionResult(text="Submitted A, started B.", is_error=False)

    session.scripts = [first]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.dev.0")) == "succeeded"
    assert "claim_limit" in refused[0]

    async def second(client: ClientSession, request: SessionRequest) -> SessionResult:
        for record_id in items:
            started, _ = await start(client, record_id)
            refused.append(started)
            if not started:
                break
        return SessionResult(text="Started C.", is_error=False)

    session.scripts = [second]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, "dev-team.dev.1")) == "succeeded"
    assert "transition_not_allowed" in refused[1]  # A is in review
    assert "record_claimed" in refused[2]  # B is in progress, held by the first developer
    assert refused[3] == ""
    with kernel.database.read() as conn:
        held = {
            str(record): str(holder)
            for record, holder in conn.exec_driver_sql(
                "SELECT record_id, holder FROM records WHERE collection='work'"
            )
        }
    assert held == {
        items[0]: "dev-team.dev.0",
        items[1]: "dev-team.dev.0",
        items[2]: "dev-team.dev.1",
    }


# A lapsed claim keeps the review (ADR-034 amendment, 2026-11-22)

LEASE = timedelta(seconds=14400 + 60)


def fake_review_and_merge(kernel: Kernel, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviews state what they asked for. A merge applies `vcs.merge`'s opener rule to the
    kernel's evidence (the oldest `vcs.open_change` receipt for the pull request) and then
    succeeds without a host; the rule itself is tested in test_vcs_merge."""
    states = {"REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}

    async def reviewed(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        number = arguments["number"]
        return {
            "review_id": number,
            "state": states.get(arguments["event"], "COMMENTED"),
            "url": f"https://git.example/pull/{number}#r",
        }

    merge = str(kernel.operations.get("vcs.merge").cardea.handler)

    async def merged(arguments: dict[str, Any], context: Any) -> Any:
        opened = context.evidence.get("opened") or ()
        if not opened or opened[0].get("agent_id") != context.agent_id:
            raise OperationRejected(
                "Only the member that opened the pull request may merge it", "merge_not_opener"
            )
        sha = "a" * 40
        return {"number": arguments["number"], "merged": True, "sha": sha, "head_sha": sha}

    review = str(kernel.operations.get("vcs.review").cardea.handler)
    monkeypatch.setitem(kernel.operations.handlers, review, reviewed)
    monkeypatch.setitem(kernel.operations.handlers, merge, merged)


def item(kernel: Kernel, record_id: str) -> tuple[str, str | None, int, Any]:
    with kernel.database.read() as conn:
        state, holder, epoch, data = conn.exec_driver_sql(
            "SELECT state, holder, claim_epoch, data_json FROM records WHERE record_id=?",
            (record_id,),
        ).one()
    return str(state), holder, int(epoch), json.loads(data).get("change")


def as_member(kernel: Kernel, session: FakeSession, member: str, script: Any) -> None:
    session.scripts = [script]
    assert AgentRunRuntime(kernel).execute(member_run(kernel, member)) == "succeeded"


async def transition(client: ClientSession, record_id: str, name: str, **extra: Any) -> str:
    """One transition: "" when it succeeded, else the refusal's text."""
    moved = await call(
        client,
        "records-transition",
        collection="work",
        record_id=record_id,
        transition=name,
        **extra,
    )
    return text(moved) if moved.is_error else ""


def develop(record_id: str) -> Any:
    """A developer's run: start the item, open its own pull request and submit."""

    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        started, output = await start(client, record_id)
        assert started == "", started
        epoch = output["claim"]["claim_epoch"]
        made = await call(
            client,
            "vcs-open_change",
            repository="cardea",
            head=f"agent/dev-team/{record_id}-{epoch}",
            base="main",
            title="Fix it",
            record_id=record_id,
        )
        assert not made.is_error, made.content
        assert await transition(client, record_id, "submit", claim_epoch=epoch) == ""
        return SessionResult(text="Submitted.", is_error=False)

    return script


def review(kernel: Kernel, record_id: str, event: str, then: str) -> Any:
    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        number = item(kernel, record_id)[3]
        made = await call(
            client,
            "vcs-review",
            repository="cardea",
            number=number,
            event=event,
            body="Reviewed.",
            client_key=f"review-{number}-{event}",
            record_id=record_id,
        )
        assert not made.is_error, made.content
        assert await transition(client, record_id, then) == ""
        return SessionResult(text="Reviewed.", is_error=False)

    return script


def merge(record_id: str, number: int, outcome: list[str], epoch: int | None = None) -> Any:
    """Merge the pull request and move the item to done (with the claim's epoch when the
    caller holds it); records each refusal."""

    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        made = await call(
            client, "vcs-merge", repository="cardea", number=number, record_id=record_id
        )
        outcome.append(text(made) if made.is_error else "")
        held = {} if epoch is None else {"claim_epoch": epoch}
        outcome.append(await transition(client, record_id, "merge", **held))
        return SessionResult(text="Merged.", is_error=False)

    return script


@pytest.mark.loopback
def test_a_lapse_in_progress_returns_the_item_to_ready(
    team: tuple[Kernel, FakeSession],
) -> None:
    kernel, session = team
    record_id = ready_item(kernel, session)

    async def started(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert (await start(client, record_id))[0] == ""
        return SessionResult(text="Started.", is_error=False)

    as_member(kernel, session, "dev-team.dev.0", started)
    assert claims.sweep(kernel, now() + LEASE) == 1
    assert item(kernel, record_id) == ("ready", None, 2, None)


@pytest.mark.loopback
def test_a_lapse_in_review_keeps_the_review_and_another_developer_takes_over(
    team: tuple[Kernel, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In review, a lapsed claim drops only the holder. The PE still reviews; changes it
    requests leave the item in progress with no holder, and another developer starts it,
    opens its own pull request and, approved, merges it as its opener."""
    kernel, session = team
    fake_open_change(kernel, monkeypatch)
    fake_review_and_merge(kernel, monkeypatch)
    record_id = ready_item(kernel, session)
    as_member(kernel, session, "dev-team.dev.0", develop(record_id))
    assert claims.sweep(kernel, now() + LEASE) == 1
    assert item(kernel, record_id) == ("in_review", None, 2, 42)
    as_member(
        kernel,
        session,
        "dev-team.pe.0",
        review(kernel, record_id, "REQUEST_CHANGES", "request_changes"),
    )
    assert item(kernel, record_id) == ("in_progress", None, 2, 42)
    as_member(kernel, session, "dev-team.dev.1", develop(record_id))
    assert item(kernel, record_id) == ("in_review", "dev-team.dev.1", 3, 44)
    as_member(kernel, session, "dev-team.pe.0", review(kernel, record_id, "APPROVE", "approve"))
    outcome: list[str] = []
    as_member(kernel, session, "dev-team.dev.1", merge(record_id, 44, outcome, epoch=3))
    assert outcome == ["", ""]
    assert item(kernel, record_id)[:2] == ("done", None)


@pytest.mark.loopback
def test_a_lapse_when_approved_keeps_the_approval_for_the_opener(
    team: tuple[Kernel, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved, a lapsed claim drops only the holder. Another developer cannot merge the
    pull request (the opener rule), so the item waits, approved, and its opener merges it
    without holding it."""
    kernel, session = team
    fake_open_change(kernel, monkeypatch)
    fake_review_and_merge(kernel, monkeypatch)
    record_id = ready_item(kernel, session)
    as_member(kernel, session, "dev-team.dev.0", develop(record_id))
    as_member(kernel, session, "dev-team.pe.0", review(kernel, record_id, "APPROVE", "approve"))
    assert claims.sweep(kernel, now() + LEASE) == 1
    assert item(kernel, record_id) == ("approved", None, 2, 42)
    outcome: list[str] = []
    as_member(kernel, session, "dev-team.dev.1", merge(record_id, 42, outcome))
    assert "merge_not_opener" in outcome[0] and "receipt_required" in outcome[1]
    assert item(kernel, record_id) == ("approved", None, 2, 42)
    outcome.clear()
    as_member(kernel, session, "dev-team.dev.0", merge(record_id, 42, outcome))
    assert outcome == ["", ""]
    assert item(kernel, record_id)[:2] == ("done", None)


def test_instructions_name_only_transitions_the_work_collection_has() -> None:
    """Agents told a state or a verb wrote fields instead of moving items. Every transition
    an instruction names exists in the rendered collection, and every role names, as an
    exact records.transition call, each transition its grants let it run."""
    import re

    module = kit()
    document = example()
    names = {t["name"] for t in module.work_collection(document)["spec"]["transitions"]}
    assert names == module.TRANSITIONS
    pools = {p["name"]: p for p in module.group_document(document)["spec"]["pools"]}
    named_by: dict[str, set[str]] = {}
    for name, pool in pools.items():
        text = pool["run"]["instructions"]
        named = set(
            re.findall(r'records\.transition with collection "work" and transition "(\w+)"', text)
        )
        assert named <= names, name
        granted = {g.removeprefix("record:work/") for g in pool["grants"]} & names
        assert granted <= named, (name, granted - named)
        named_by[name] = named
    assert named_by["tl"] == {"triage", "cancel"}
    assert named_by["dev"] == {"start", "submit", "resubmit", "merge"}
    assert named_by["pe"] == {"approve", "request_changes"}
    tl = pools["tl"]["run"]["instructions"]
    assert "not the state's" in tl and "records.update never changes an item's state" in tl
    assert "records.query (state ready)" in tl
    with pytest.raises(ValueError, match="no transition 'ready'"):
        module.call("ready")
