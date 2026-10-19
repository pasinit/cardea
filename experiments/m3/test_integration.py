"""Real SPIRE + Debian worker + native MCP; provider fixture unless explicitly live."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cardea.runtime.directory import deliver, snapshot
from cardea.service.client import ClientError
from cardea.service.database import Database
from cardea.service.models import (
    RIGHTS,
    AgentTaskRequest,
    HostBudget,
    ModelBudget,
    ModelRuntimeConfig,
    Permission,
    now,
)
from harness import ROOT, SUPERVISOR, ServiceLab, wait_for
from provider_lab import Lab, eventually


class AgentLab(ServiceLab):
    def start(self, barrier: str = ""):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DBOS_", "OTEL_", "M1_", "OPENROUTER"))
        }
        env.update(
            PYTHONPATH=str(ROOT / "apps/api/src"),
            M1_CONFIG=str(self.config_path),
            M1_BARRIER=barrier,
            M3_FIXTURE="0" if os.environ.get("M3_LIVE_KEY_FILE") else "1",
        )
        with (self.root / f"daemon-{len(self.processes)}.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "experiments/m3/daemon_probe.py")],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.processes.append(process)

        def ready():
            assert process.poll() is None, self.logs()
            assert self.client.request("GET", "/system")["agent_execution"]

        wait_for(ready, 60)
        return process


@pytest.fixture(scope="module")
def provider():
    lab = Lab()
    try:
        lab.start()
        lab.register(SUPERVISOR)
        eventually(lambda: lab.token(SUPERVISOR))
        yield lab
    finally:
        lab.close()


@pytest.fixture
def service(provider, tmp_path):
    live_key = os.environ.get("M3_LIVE_KEY_FILE")
    key = Path(live_key) if live_key else tmp_path / "provider.key"
    if not live_key:
        key.write_bytes(b"fictional-fixture-key")
        key.chmod(0o600)
    lab = AgentLab(
        provider,
        host_execution=True,
        agent_runtime=ModelRuntimeConfig(
            model="z-ai/glm-4.7"
            if os.environ.get("M3_LIVE_KEY_FILE")
            else "fixture/model",
            api_key_file=key,
        ),
    )
    extra = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for r in (
            "models.invoke",
            "models.disclose",
            "tools.incidents",
            "inputs.append",
        )
    )
    lab.document = lab.document.model_copy(
        update={
            "permissions": (*lab.document.permissions, *extra),
            "platform": (
                *lab.document.platform,
                *(p.model_copy(update={"principal_id": "platform"}) for p in extra),
            ),
            "host_budgets": (
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=100000,
                    output_bytes=1000000,
                ),
            ),
            "model_budgets": (
                ModelBudget(
                    principal_id="supervisor",
                    namespace="test",
                    calls=10,
                    tokens=1000000,
                ),
            ),
        }
    )
    lab.apply(lab.document)
    try:
        yield lab
    finally:
        lab.close()


def write_record(directory, identity):
    (directory / f"{identity}.json").write_text(
        json.dumps(
            {
                "id": identity,
                "title": f"Fictional incident {identity}",
                "severity": "low",
            }
        )
    )


def submit(service):
    directory = service.root / "input"
    directory.mkdir(exist_ok=True)
    write_record(directory, "INC-1")
    request = AgentTaskRequest(
        request_id="agent-one",
        agent_id="agent-a",
        namespace="test",
        objective="Use the permitted tool to create the incident index and Markdown summary.",
        inputs=snapshot(directory),
        grants=(
            *RIGHTS,
            "models.invoke",
            "models.disclose",
            "tools.incidents",
            "inputs.append",
        ),
    )
    return service.client.submit_agent(request).task.task_id


def exists(path):
    assert path.exists()


def receipts(service, task):
    return service.client.request("GET", f"/tasks/{task}/agent-receipts")["receipts"]


def test_restart_and_input_during_downtime(service):
    process = service.start()
    task = submit(service)
    service.wait_state(task, "waiting", timeout=90)
    first = service.client.request("GET", f"/tasks/{task}/output")
    assert first["artifact"]["version"] == 1
    service.stop(process)
    write_record(service.root / "input", "INC-2")
    service.start()
    # Restarting the trusted input adapter rescans files missed while the daemon was down.
    deliver(service.client, task, service.root / "input")
    service.wait_state(task, "succeeded", timeout=90)
    final = service.client.request("GET", f"/tasks/{task}/output")
    result = receipts(service, task)
    assert final["artifact"]["version"] == 2
    assert (
        len(final["content"]["index"]) == 2 and "[INC-2]" in final["content"]["summary"]
    )
    assert len(result) == 4 and all(r["state"] == "succeeded" for r in result)
    assert sum(r["usage"]["model_calls"] for r in result) == 2
    deliver(service.client, task, service.root / "input")
    with pytest.raises(ClientError):
        service.client.request(
            "POST",
            f"/tasks/{task}/inputs",
            json={"request_id": "different", "inputs": {"records": []}},
        )

    def cleaned():
        db = Database(service.data)
        try:
            with db.engine.begin() as conn:
                assert (
                    conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM agent_effects WHERE kind='tool' AND cleaned=0"
                    ).scalar_one()
                    == 0
                )
        finally:
            db.close()

    wait_for(cleaned, 30)
    evidence = os.environ.get("M3_RUN_EVIDENCE")
    if evidence:
        Path(evidence).write_text(
            json.dumps(
                {
                    "provider": "live"
                    if os.environ.get("M3_LIVE_KEY_FILE")
                    else "fixture",
                    "first": first,
                    "final": final,
                    "receipts": result,
                    "verified": service.client.inspect(
                        task
                    ).task.verification.model_dump(mode="json"),
                },
                indent=2,
            )
        )


@pytest.mark.parametrize(
    "stage", ["agent.model.settle_committed", "agent.publish_committed"]
)
def test_committed_checkpoint_no_duplicate_calls(service, stage):
    process = service.start(stage)
    task = submit(service)
    wait_for(lambda: exists(service.data / f"{process.pid}.{stage}.entered"), 60)
    service.stop(process)
    service.start()
    service.wait_state(task, "waiting", timeout=90)
    assert len(receipts(service, task)) == 2
    assert sum(r["usage"]["model_calls"] for r in receipts(service, task)) == 1


@pytest.mark.parametrize(
    "stage", ["agent.model.intent_committed", "agent.tool.started"]
)
def test_uncertain_effect_is_not_replayed(service, stage):
    process = service.start(stage)
    task = submit(service)
    wait_for(lambda: exists(service.data / f"{process.pid}.{stage}.entered"), 60)
    service.stop(process)
    service.start()
    service.wait_state(task, "failed", timeout=110)
    result = receipts(service, task)
    assert sum(r["state"] == "outcome_unknown" for r in result) == 1
    assert service.client.inspect(task).task.verification is None


def test_disclosure_revoked_before_dispatch(service):
    process = service.start("agent.model.intent_committed")
    task = submit(service)
    wait_for(
        lambda: exists(
            service.data / f"{process.pid}.agent.model.intent_committed.entered"
        ),
        45,
    )
    service.apply(
        service.document.model_copy(
            update={
                "permissions": tuple(
                    p
                    for p in service.document.permissions
                    if p.operation != "models.disclose"
                )
            }
        )
    )
    (service.data / f"{process.pid}.agent.model.intent_committed.release").touch()

    # Administrative inspection verifies the denied task while its normal ceiling is withdrawn.
    def failed():
        db = Database(service.data)
        try:
            with db.engine.begin() as conn:
                assert (
                    conn.exec_driver_sql(
                        "SELECT state FROM tasks WHERE task_id=?", (task,)
                    ).scalar_one()
                    == "failed"
                )
                assert (
                    conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM agent_effects WHERE kind='tool'"
                    ).scalar_one()
                    == 0
                )
        finally:
            db.close()

    wait_for(failed, 110)
