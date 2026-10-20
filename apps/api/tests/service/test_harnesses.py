"""Installed harness registry, selection and authority (ADR-033)."""

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client.session import ClientSession
from pydantic import ValidationError
from test_agent_runs import GRANTS, FakeSession, Tools, call, descriptor, routine
from test_operations import Auth, FakeClient

from cardea.runtime.agent_session import ClaudeAgentSession, SessionRequest, SessionResult
from cardea.service import agent_run_runtime, harnesses
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication
from cardea.service.database import Database, Fault, borrow
from cardea.service.harnesses import Catalog, HarnessEntry, Harnesses, HarnessFile
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    AgentRunRequest,
    Permission,
    Principal,
    Provisioning,
    ServiceConfig,
    TriggerSpec,
    now,
)
from cardea.service.operations import Registry
from cardea.service.server import start_harnesses

PRICE = {"price": {"input_usd_per_mtok": 0.5, "output_usd_per_mtok": 2.0}}


def entry(**changes: Any) -> dict[str, Any]:
    base = {
        "id": "claude",
        "kind": "claude-agent-sdk",
        "package": {"name": "claude-agent-sdk", "version": "0.2.163"},
        "credential": {"file": "/etc/cardea/credentials/anthropic", "kind": "api_key"},
        "default_model": "claude-sonnet-4-5",
        "models": {"claude-sonnet-4-5": {}, "claude-haiku-4-5": {}},
    }
    return {**base, **changes}


def open_entry(**changes: Any) -> dict[str, Any]:
    base = entry(
        id="glm",
        kind="opencode",
        package={
            "name": "opencode-ai",
            "version": "1.18.35",
            "binary": "/opt/cardea/opencode/bin/opencode",
            "sha256": "0" * 64,
        },
        provider={"base_url": "https://openrouter.ai/api/v1"},
        default_model="z-ai/glm-4.6",
        models={
            "z-ai/glm-4.6": PRICE,
            "qwen3:8b": {"price": {"input_usd_per_mtok": 0, "output_usd_per_mtok": 0}},
        },
    )
    return {**base, **changes}


def harness_file(*entries: dict[str, Any], **changes: Any) -> HarnessFile:
    return HarnessFile.model_validate(
        {"harnesses": list(entries), "default": entries[0]["id"], **changes}
    )


# Registry validation


def test_registry_accepts_priced_open_harnesses() -> None:
    file = harness_file(entry(), open_entry(), namespace_defaults={"research": "glm"})
    assert [h.id for h in file.harnesses] == ["claude", "glm"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"default_model": "other"}, "default_model must be one of its models"),
        ({"provider": {"base_url": "https://example.invalid/v1"}}, "takes no provider"),
        ({"credential": None}, "needs a credential or a confined profile"),
        (
            {"confined": {"profile": "session", "worker": "agent"}},
            "credential is its profile's",
        ),
        ({"models": {}, "default_model": None, "id": "Bad"}, "String should match pattern"),
        ({"package": {"name": "x", "version": "1", "binary": "/x"}}, "pinned by its SHA-256"),
    ],
)
def test_registry_refuses_incoherent_entries(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        HarnessEntry.model_validate(entry(**changes))


def test_open_harness_models_must_all_be_priced() -> None:
    with pytest.raises(ValidationError, match=r"needs a price .*: z-ai/glm-4.6"):
        HarnessEntry.model_validate(open_entry(models={"z-ai/glm-4.6": {}}))
    with pytest.raises(ValidationError, match="needs a provider base_url"):
        HarnessEntry.model_validate(open_entry(provider=None))


def test_registry_refuses_duplicate_ids_and_unknown_defaults() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        harness_file(entry(), entry())
    with pytest.raises(ValidationError, match="Default harness nope"):
        harness_file(entry(), namespace_defaults={"test": "nope"})


def write(path: Path, document: object, mode: int = 0o644) -> Path:
    path.write_text(json.dumps(document))
    os.chmod(path, mode)
    return path


def test_registry_file_errors_name_the_field(tmp_path: Path) -> None:
    path = write(
        tmp_path / "harnesses.json",
        {"harnesses": [open_entry(models={"z-ai/glm-4.6": {}})], "default": "glm"},
    )
    with pytest.raises(Fault) as invalid:
        harnesses.load(path)
    assert invalid.value.code == "harness_config_invalid"
    assert "harnesses.0" in invalid.value.message and "z-ai/glm-4.6" in invalid.value.message


def test_registry_file_must_not_be_writable_by_others(tmp_path: Path) -> None:
    document = {"harnesses": [entry()], "default": "claude"}
    with pytest.raises(Fault) as unsafe:
        harnesses.load(write(tmp_path / "group.json", document, 0o664))
    assert unsafe.value.code == "unsafe_file"
    link = tmp_path / "link.json"
    link.symlink_to(write(tmp_path / "real.json", document))
    with pytest.raises(Fault) as linked:
        harnesses.load(link)
    assert linked.value.code == "harness_config_invalid"
    assert harnesses.load(tmp_path / "real.json").default == "claude"


def test_pins_are_checked_at_start(tmp_path: Path) -> None:
    binary = tmp_path / "opencode"
    binary.write_bytes(b"pinned build")
    digest = hashlib.sha256(b"pinned build").hexdigest()
    pinned = HarnessEntry.model_validate(
        open_entry(
            package={
                "name": "opencode-ai",
                "version": "1.18.35",
                "binary": str(binary),
                "sha256": digest,
            }
        )
    )
    harnesses.check_pin(pinned)
    binary.write_bytes(b"another build")
    with pytest.raises(Fault) as changed:
        harnesses.check_pin(pinned)
    assert changed.value.code == "harness_pin_mismatch"
    binary.unlink()
    with pytest.raises(Fault) as missing:
        harnesses.check_pin(pinned)
    assert missing.value.code == "harness_not_installed"
    stale = HarnessEntry.model_validate(
        entry(package={"name": "claude-agent-sdk", "version": "0.0.1"})
    )
    with pytest.raises(Fault) as old:
        harnesses.check_pin(stale)
    assert old.value.code == "harness_pin_mismatch" and "0.0.1" in old.value.message


def test_build_refuses_an_unavailable_kind() -> None:
    with pytest.raises(Fault) as refused:
        harnesses.build(harness_file(entry()), {})
    assert refused.value.code == "harness_not_installed"


def test_service_config_takes_one_harness_source(tmp_path: Path) -> None:
    base = {
        "data_dir": str(tmp_path),
        "spiffe_socket": "unix:///tmp/agent.sock",
        "trust_domain": "example.org",
        "runtime_spiffe_id": "spiffe://example.org/runtime",
        "harnesses_file": str(tmp_path / "h.json"),
    }
    session = {"model": "m", "api_key_file": str(tmp_path / "key")}
    with pytest.raises(ValidationError, match="not both"):
        ServiceConfig.model_validate({**base, "agent_session": session})
    legacy = ServiceConfig.model_validate(
        {**base, "harnesses_file": None, "agent_session": session}
    )
    catalog = harnesses.catalog(legacy)
    assert catalog is not None and catalog.implicit == "claude-sdk" == catalog.default
    assert catalog.resolve("test", None, None) == ("claude-sdk", "m")


# Selection


def catalog() -> Catalog:
    return Catalog.from_file(
        harness_file(entry(), open_entry(), namespace_defaults={"research": "glm"})
    )


def test_selection_defaults_and_refusals() -> None:
    chosen = catalog()
    assert chosen.resolve("test", None, None) == ("claude", "claude-sonnet-4-5")
    assert chosen.resolve("research", None, None) == ("glm", "z-ai/glm-4.6")
    assert chosen.resolve("test", "glm", "qwen3:8b") == ("glm", "qwen3:8b")
    with pytest.raises(Fault) as unknown:
        chosen.resolve("test", "codex", None)
    assert (unknown.value.code, unknown.value.status) == ("harness_not_installed", 422)
    with pytest.raises(Fault) as model:
        chosen.resolve("test", "claude", "z-ai/glm-4.6")
    assert (model.value.code, model.value.status) == ("model_not_allowed", 422)


def test_implicit_harness_needs_no_grant_and_allows_only_its_model() -> None:
    legacy = Harnesses.single(FakeSession(), "claude-sonnet-4-5")
    legacy.check_grant("claude-sdk", ())
    assert legacy.resolve("test", None, "claude-sonnet-4-5")[1] == "claude-sonnet-4-5"
    with pytest.raises(Fault):
        legacy.resolve("test", None, "claude-opus-4-1")
    with pytest.raises(Fault) as ungranted:
        catalog().check_grant("glm", GRANTS)
    assert (ungranted.value.code, ungranted.value.status) == ("harness_not_granted", 403)


# Admission and runs


HARNESS_GRANTS = (*GRANTS, "harness:claude", "harness:glm")


@pytest.fixture
def sessions() -> dict[str, FakeSession]:
    return {"claude": FakeSession(), "glm": FakeSession()}


@pytest.fixture
def kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: dict[str, FakeSession],
) -> Iterator[Kernel]:
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
        for right in (*HARNESS_GRANTS, "tasks.schedule")
    )
    installed = Harnesses(cast(dict[str, Any], sessions), catalog())
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
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=installed)
    try:
        yield instance
    finally:
        db.close()


def run_request(**changes: Any) -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "request_id": "routine-0830",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Daily listings recap",
            "instructions": "Search listings, then email the user one recap.",
            "operations": ["listings.search", "mail.send"],
            "grants": list(HARNESS_GRANTS),
            "attempt_timeout_seconds": 30,
            **changes,
        }
    )


def submit(kernel: Kernel, **changes: Any) -> str:
    return kernel.submit(kernel.auth.validate("supervisor"), run_request(**changes)).task.task_id


def output(kernel: Kernel, task_id: str) -> dict[str, Any]:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        assert task.verification is not None
        return cast(dict[str, Any], json.loads(tx.read_object(task.verification.artifacts[0])))


def test_a_run_uses_the_selected_harness_and_model(
    kernel: Kernel, sessions: dict[str, FakeSession]
) -> None:
    sessions["glm"].scripts = [routine]
    task_id = submit(kernel, harness="glm", model="qwen3:8b")
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    (sent,) = sessions["glm"].requests
    assert sent.model == "qwen3:8b" and sent.max_tool_calls == 100
    assert sessions["claude"].requests == []
    document = output(kernel, task_id)
    assert (document["harness"], document["model"]) == ("glm", "qwen3:8b")


def test_a_run_without_a_harness_gets_the_namespace_default(
    kernel: Kernel, sessions: dict[str, FakeSession]
) -> None:
    sessions["claude"].scripts = [routine]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert sessions["claude"].requests[0].model == "claude-sonnet-4-5"


@pytest.mark.parametrize(
    ("changes", "code", "status"),
    [
        ({"harness": "codex"}, "harness_not_installed", 422),
        ({"harness": "glm", "model": "claude-sonnet-4-5"}, "model_not_allowed", 422),
        ({"harness": "glm", "grants": [*GRANTS, "harness:claude"]}, "harness_not_granted", 403),
        ({"grants": [*GRANTS, "harness:codex"]}, "harness_not_installed", 422),
    ],
)
def test_admission_refuses_unknown_or_ungranted_selections(
    kernel: Kernel, changes: dict[str, Any], code: str, status: int
) -> None:
    with pytest.raises(Fault) as refused:
        submit(kernel, **changes)
    assert (refused.value.code, refused.value.status) == (code, status)


def test_harness_grant_needs_supervisor_policy(kernel: Kernel) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM policies WHERE operation='harness:glm' AND principal_id='supervisor'"
        )
    with pytest.raises(Fault) as denied:
        submit(kernel, harness="glm")
    assert denied.value.code == "authority_denied"


def test_revoking_the_harness_stops_the_run(
    kernel: Kernel, sessions: dict[str, FakeSession]
) -> None:
    async def revoked(client: ClientSession, request: SessionRequest) -> SessionResult:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM policies WHERE operation='harness:glm'")
        refused = await call(client, "listings-search")
        assert refused.is_error
        return SessionResult(text="", is_error=True, stop_reason="error_max_turns")

    sessions["glm"].scripts = [revoked]
    task_id = submit(kernel, harness="glm")
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    # The retry was refused at the attempt's authority check: one session only.
    assert len(sessions["glm"].requests) == 1


def provisioning(rule: Permission) -> Provisioning:
    return Provisioning(principals=(), permissions=(), platform=(rule,))


def test_provisioning_refuses_grants_for_uninstalled_harnesses(tmp_path: Path) -> None:
    db = Database(tmp_path / "data")
    db.migrate()
    rule = Permission(
        principal_id="platform",
        namespace="test",
        operation="harness:codex",
        expires_at=now() + timedelta(hours=1),
    )
    try:
        with pytest.raises(Fault) as refused:
            provision(db, provisioning(rule), harnesses=catalog())
        assert refused.value.code == "harness_not_installed"
        # Without a loaded registry the grant is accepted; admission checks it later.
        provision(db, provisioning(rule))
    finally:
        db.close()


def test_trigger_templates_carry_and_check_the_selection(kernel: Kernel) -> None:
    template = run_request(harness="glm", model="qwen3:8b").model_dump(
        mode="json", exclude={"request_id", "idempotency_context", "slot_time"}
    )
    spec = TriggerSpec.model_validate(
        {
            "trigger_id": "daily-recap",
            "supervisor_id": "supervisor",
            "schedule": "30 8 * * *",
            "misfire": "skip",
            "timezone": "Europe/London",
            "template_kind": "agent-run",
            "template": template,
        }
    )
    request = spec.request("daily-recap.20261020T073000Z")
    assert isinstance(request, AgentRunRequest) and request.harness == "glm"
    assert kernel.schedule(kernel.auth.validate("supervisor"), spec)["change"] == "installed"
    bad = spec.model_copy(
        update={"trigger_id": "bad", "template": {**template, "model": "claude-sonnet-4-5"}}
    )
    with pytest.raises(Fault) as refused:
        kernel.schedule(kernel.auth.validate("supervisor"), bad)
    assert refused.value.code == "model_not_allowed"


def test_service_start_builds_registry_harnesses(tmp_path: Path) -> None:
    key = tmp_path / "anthropic"
    key.write_text("sk-test\n")
    os.chmod(key, 0o600)
    path = write(
        tmp_path / "harnesses.json",
        {
            "harnesses": [
                entry(credential={"file": str(key), "kind": "api_key"}),
                entry(id="confined", credential=None, confined={"profile": "s", "worker": "a"}),
            ],
            "default": "claude",
            "concurrent_sessions": 3,
        },
    )
    config = ServiceConfig.model_validate(
        {
            "data_dir": str(tmp_path),
            "spiffe_socket": "unix:///tmp/agent.sock",
            "trust_domain": "example.org",
            "runtime_spiffe_id": "spiffe://example.org/runtime",
            "harnesses_file": str(path),
        }
    )
    with pytest.raises(Fault) as unconfined:
        start_harnesses(config, None)
    assert unconfined.value.code == "unsupported_host"
    write(path, {"harnesses": [entry(credential={"file": str(key)})], "default": "claude"})
    started = start_harnesses(config, None)
    assert started is not None and started.implicit is None
    claude = started.get("claude")
    assert isinstance(claude, ClaudeAgentSession)
    assert (claude.api_key, claude.model) == ("sk-test", "claude-sonnet-4-5")
