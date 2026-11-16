"""Walk through ADR-034's two worked examples on a throwaway kernel: `make teams`.

Everything is configuration from this directory, plus the fake operations package in
`packages/cardea-reference-ops`. The kernel is real: collections, records, state machines,
claims, agent groups, approval waits and event triggers, with every call receipted. What
is not real, so that it runs anywhere in a few seconds:

- each agent is a short script that calls its tools over the run's real MCP endpoint,
  instead of a model;
- identities are fixed test credentials, not SPIFFE or OIDC;
- runs execute in this process one step at a time, instead of on the daemon's queue.
"""

import base64
import json
import logging
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from math import floor
from pathlib import Path
from typing import Any, cast

import httpx2
from cardea_reference_ops import Fakes, descriptors
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from standardwebhooks.webhooks import Webhook

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import events
from cardea.service import kernel as kernel_module
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault
from cardea.service.groups import apply as apply_group
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    Provisioning,
    TriggerSpec,
)
from cardea.service.operations import Registry
from cardea.service.records import CollectionDocument, apply_collection

HERE = Path(__file__).resolve().parent
RUN = ("objects.read", "objects.write", "models.invoke")
PEOPLE = ("runtime", "sup-eng", "sup-desk", "desk-approver")
# A Standard Webhooks secret for the fake broker's fill webhook; not a real credential.
BROKER_SECRET = "whsec_" + base64.b64encode(b"broker-fills-example-key-32bytes").decode()
Script = Callable[[ClientSession, SessionRequest], Awaitable[Any]]


class TestCredentials:
    """Fixed identities for the walkthrough; never use this outside a throwaway store."""

    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "example", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in PEOPLE:
            raise Fault("invalid_credential", "Unknown example credential", 401)
        return Proof("spiffe", "example", token, time.time() + 60)


class ScriptedAgents:
    """A harness that plays the next script over the run's MCP endpoint."""

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
        return SessionResult(text="Done.", is_error=False, turns=1)


class NoQueue:
    """Runs are executed in-process below, so nothing is enqueued for the daemon."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


def document(path: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((HERE / path).read_text()))


def groups() -> dict[str, AgentGroup]:
    return {
        name: AgentGroup.model_validate(document(f"{name}/group.json"))
        for name in ("software-team", "trading-desk")
    }


def provisioning(triggers: tuple[TriggerSpec, ...] = ()) -> Provisioning:
    """Principals and policy: each supervisor may delegate what its pools are granted."""
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    permissions: list[dict[str, Any]] = []
    platform: list[dict[str, Any]] = []
    for group in groups().values():
        rights = {*RIGHTS, *RUN, "groups.manage", *(g for p in group.spec.pools for g in p.grants)}
        for right in sorted(rights):
            grant = {
                "namespace": group.metadata.namespace,
                "operation": right,
                "expires_at": expires,
            }
            permissions.append({"principal_id": group.spec.supervisor, "delegate": True, **grant})
            approve = right == "record:orders/approve"
            platform.append({"principal_id": "platform", "approve": approve, **grant})
    # A human desk approver may decide the `approve` transition of orders, nothing else.
    permissions.append(
        {
            "principal_id": "desk-approver",
            "namespace": "desk",
            "allow": False,
            "approve": True,
            "operation": "record:orders/approve",
            "expires_at": expires,
        }
    )
    principals = [
        {
            "principal_id": p,
            "kind": "service" if p == "runtime" else "human",
            "auth_kind": "spiffe",
            "issuer": "example",
            "subject": p,
        }
        for p in PEOPLE
    ]
    return Provisioning.model_validate(
        {
            "principals": principals,
            "permissions": permissions,
            "platform": platform,
            "triggers": [t.model_dump(mode="json") for t in triggers],
        }
    )


@contextmanager
def kernel(directory: Path) -> Iterator[tuple[Kernel, ScriptedAgents, Fakes]]:
    setattr(kernel_module, "DBOSClient", NoQueue)  # noqa: B010
    fakes = Fakes()
    registry = Registry(descriptors(), fakes.handlers())
    database = Database(directory / "store")
    database.migrate()
    provision(database, provisioning(), registry)
    agents = ScriptedAgents()
    entry = implicit_entry("example-model")
    catalog = Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    harnesses = Harnesses({IMPLICIT_ID: agents}, catalog)
    instance = Kernel(
        database,
        cast(Authentication, TestCredentials()),
        operations=registry,
        harnesses=harnesses,
    )
    try:
        yield instance, agents, fakes
    finally:
        database.close()


def configure(kernel: Kernel, example: str) -> None:
    """`PUT /v0.2/collections/{name}` and `PUT /v0.2/groups/{name}`, in process."""
    group = groups()[example]
    for path in sorted((HERE / example).glob("collection.*.json")):
        collection = CollectionDocument.model_validate(json.loads(path.read_text()))
        with kernel.database.engine.begin() as conn:
            apply_collection(conn, group.spec.supervisor, collection, dry_run=False)
    result = apply_group(
        kernel, kernel.auth.validate(group.spec.supervisor), group.metadata.name, group, False
    )
    members = ", ".join(m["principal_id"] for m in result["group"]["status"]["members"])
    print(f"  applied {group.metadata.name} (generation {result['generation']}): {members}")


async def call(client: ClientSession, tool: str, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        return {"error": str(getattr(result.content[0], "text", ""))}
    return result.structured_content


def run(
    kernel: Kernel,
    agents: ScriptedAgents,
    member: str,
    operations: list[str],
    script: Script,
    *,
    attempts: int = 1,
) -> tuple[str, Any]:
    """Start one run of a group member and play its script; returns state and result."""
    group = next(g for g in groups().values() if member.startswith(g.metadata.name + "."))
    pool = next(p for p in group.spec.pools if member.split(".")[1] == p.name)
    agents.scripts = [script] * attempts
    agents.results = []
    request = AgentRunRequest.model_validate(
        {
            "request_id": f"{member}.{time.monotonic_ns()}",
            "agent_id": member,
            "namespace": group.metadata.namespace,
            "objective": f"One step of {group.metadata.name}",
            "instructions": pool.run.instructions,
            "operations": operations,
            "grants": list(pool.grants),
            "max_attempts": attempts,
            "attempt_timeout_seconds": 60,
        }
    )
    proof = kernel.auth.validate(group.spec.supervisor)
    task_id = kernel.submit(proof, request).task.task_id
    state = AgentRunRuntime(kernel).execute(task_id)
    return state, agents.results[-1] if agents.results else None


def move(collection: str, record_id: str, name: str, **extra: Any) -> Script:
    async def script(client: ClientSession, request: SessionRequest) -> Any:
        return await call(
            client,
            "records-transition",
            collection=collection,
            record_id=record_id,
            transition=name,
            **extra,
        )

    return script


def software_team(kernel: Kernel, agents: ScriptedAgents) -> str:
    print("Software team (examples/teams/software-team)")
    configure(kernel, "software-team")

    async def file_work(client: ClientSession, request: SessionRequest) -> Any:
        return await call(
            client,
            "records-create",
            collection="work",
            client_key="login-slow",
            data={"title": "Login page is slow", "body": "p95 is 4 s"},
        )

    _, made = run(kernel, agents, "eng-team.product.0", ["records.create"], file_work)
    item = made["record"]["record_id"]
    print(f"  product filed {item}: open")

    async def assign(client: ClientSession, request: SessionRequest) -> Any:
        await call(
            client,
            "records-update",
            collection="work",
            record_id=item,
            expected_version=1,
            data={"title": "Login page is slow", "assignee_pool": "eng-team.dev"},
        )
        return await move("work", item, "triage", args={"assignee_pool": "eng-team.dev"})(
            client, request
        )

    _, out = run(
        kernel, agents, "eng-team.lead.0", ["records.update", "records.transition"], assign
    )
    print(f"  lead assigned it to the developers and triaged it: {out['record']['state']}")
    start = move("work", item, "start")
    _, out = run(kernel, agents, "eng-team.dev.1", ["records.transition"], start)
    epoch = out["claim"]["claim_epoch"]
    print(f"  eng-team.dev.1 claimed it (epoch {epoch}): {out['record']['state']}")

    async def implement(client: ClientSession, request: SessionRequest) -> Any:
        change = await call(client, "vcs-open_change", record_id=item, title="Cache sessions")
        print(f"  eng-team.dev.1 opened {change['change_id']} (fake code host)")
        return await move("work", item, "submit", claim_epoch=epoch)(client, request)

    _, out = run(
        kernel,
        agents,
        "eng-team.dev.1",
        ["vcs.open_change", "records.transition"],
        implement,
    )
    print(f"  submitted with the change's receipt as evidence: {out['record']['state']}")
    _, out = run(
        kernel,
        agents,
        "eng-team.principal.0",
        ["records.transition"],
        move("work", item, "approve"),
    )
    print(f"  the principal approved it: {out['record']['state']}")
    show_overview(kernel, "eng-team", "eng", "sup-eng")
    return str(out["record"]["state"])


def show_overview(kernel: Kernel, group: str, namespace: str, supervisor: str) -> None:
    """`GET /v0.2/groups/{name}/overview`, as the supervisor sees it."""
    response = TestClient(create_app(kernel)).get(
        f"/v0.2/groups/{group}/overview",
        params={"namespace": namespace, "events": 5},
        headers={"Authorization": f"Bearer {supervisor}", "X-Cardea-Auth": "spiffe"},
    )
    assert response.status_code == 200, response.json()
    view = response.json()
    print(f"  Overview of {group} (generation {view['generation']}, as of {view['as_of']}):")
    for pool in view["pools"]:
        runs = [
            f"{m['principal_id'].rsplit('.', 1)[1]}:{(m['last_run'] or {}).get('outcome', '-')}"
            for m in pool["members"]
        ]
        print(f"    pool {pool['name']:<9} last runs {' '.join(runs)}")
    for collection in view["collections"]:
        counts = ", ".join(f"{k} {v}" for k, v in collection["by_state"].items()) or "empty"
        claimed = len(collection["claimed"])
        print(f"    collection {collection['name']}: {counts}; {claimed} claimed")
    mail = sum(a["consumed"] for a in view["mailbox"]["addresses"])
    print(f"    mail consumed {mail}; open approvals {len(view['approvals']['items'])}")
    for event in view["journal"]["events"]:
        print(f"    journal #{event['cursor']} {event['type']} by {event['producer_id']}")


def fill_event(order: str) -> tuple[dict[str, str], bytes]:
    body = json.dumps(
        {
            "specversion": "1.0",
            "id": f"fill-{order}",
            "source": "broker",
            "type": "fill",
            "subject": order,
            "datacontenttype": "application/json",
            "data": {"order_ref": order, "filled_qty": 10},
        }
    ).encode()
    stamp = floor(time.time())
    signature = Webhook(BROKER_SECRET).sign(
        f"msg-{order}", datetime.fromtimestamp(stamp, tz=UTC), body.decode()
    )
    headers = {
        "webhook-id": f"msg-{order}",
        "webhook-timestamp": str(stamp),
        "webhook-signature": signature,
        "content-type": "application/cloudevents+json",
    }
    return headers, body


def trading_desk(kernel: Kernel, agents: ScriptedAgents, fakes: Fakes, directory: Path) -> str:
    print("Trading desk (examples/teams/trading-desk)")
    configure(kernel, "trading-desk")

    async def propose(client: ClientSession, request: SessionRequest) -> Any:
        risk = await call(client, "risk-check")
        print(f"  analyst checked risk: exposure {risk['exposure']} of {risk['limit']}")
        return await call(
            client,
            "records-create",
            collection="orders",
            client_key="xyz-1",
            data={
                "instrument": "XYZ",
                "side": "buy",
                "qty": 10,
                "limit_price": 10.5,
                "rationale": "Mean reversion",
            },
        )

    _, made = run(kernel, agents, "desk.analyst.0", ["records.create", "risk.check"], propose)
    order = made["record"]["record_id"]
    print(f"  analyst proposed {order}: proposed")

    state, held = run(
        kernel, agents, "desk.risk.0", ["records.transition"], move("orders", order, "approve")
    )
    print(f"  risk asked to approve it; the run is {state}: {held['error'].split(':')[0]}")
    with kernel.database.read() as conn:
        request_id, digest, task_id = conn.exec_driver_sql(
            "SELECT request_id,intent_digest,task_id FROM approval_requests "
            "ORDER BY created_at DESC LIMIT 1"
        ).one()
    decided = TestClient(create_app(kernel)).post(
        f"/v0.2/approvals/{request_id}/decision",
        json={"decision_id": "d-1", "decision": "approve", "intent_digest": digest},
        headers={"Authorization": "Bearer desk-approver", "X-Cardea-Auth": "spiffe"},
    )
    assert decided.status_code == 200, decided.json()

    async def resumed(client: ClientSession, request: SessionRequest) -> Any:
        return "resumed"

    agents.scripts = [resumed]
    AgentRunRuntime(kernel).execute(str(task_id))
    print("  the desk approver approved; the held transition ran: approved")

    async def place(client: ClientSession, request: SessionRequest) -> Any:
        placed = await call(client, "broker-place_order", order_ref=order, record_id=order, qty=10)
        if "This is attempt 2" not in request.prompt:
            print("  execution's first attempt placed the order, then its process died")
            raise ConnectionError("the agent's process died after placing the order")
        print(f"  the retry found the placement already done: {placed['broker_order_id']}")
        return await move("orders", order, "place")(client, request)

    _, out = run(
        kernel,
        agents,
        "desk.execution.0",
        ["broker.place_order", "records.transition"],
        place,
        attempts=2,
    )
    epoch = out["claim"]["claim_epoch"]
    print(f"  execution placed it once across a crash ({fakes.placements} placement): placed")

    secret = directory / "broker-fills.secret"
    secret.write_text(BROKER_SECRET + "\n")
    secret.chmod(0o600)
    fills = document("trading-desk/trigger.fills.json")
    fills["event"]["secret_file"] = str(secret)
    provision(
        kernel.database, provisioning((TriggerSpec.model_validate(fills),)), kernel.operations
    )
    headers, body = fill_event(order)
    woken = events.deliver(kernel, "desk-fills", headers, body)
    agents.scripts = [move("orders", order, "fill", claim_epoch=epoch)]
    agents.results = []
    AgentRunRuntime(kernel).execute(str(woken["task_id"]))
    filled = agents.results[-1]["record"]["state"]
    print(f"  the broker's signed fill webhook woke execution: {filled}")

    async def reconcile(client: ClientSession, request: SessionRequest) -> Any:
        await call(client, "broker-fills", order_ref=order, record_id=order)
        return await move("orders", order, "reconcile", claim_epoch=epoch)(client, request)

    _, out = run(
        kernel, agents, "desk.execution.0", ["broker.fills", "records.transition"], reconcile
    )
    print(f"  execution read the fills and reconciled: {out['record']['state']}")
    return str(out["record"]["state"])


def main(directory: Path | None = None) -> dict[str, str]:
    logging.disable(logging.CRITICAL)  # the narrative below, not the kernel's log
    try:
        with tempfile.TemporaryDirectory() as scratch:
            base = directory or Path(scratch)
            with kernel(base) as (instance, agents, fakes):
                outcome = {
                    "software-team": software_team(instance, agents),
                    "trading-desk": trading_desk(instance, agents, fakes, base),
                }
    finally:
        logging.disable(logging.NOTSET)
    print("Both teams ran on the same kernel with no code of their own.")
    return outcome


if __name__ == "__main__":
    sys.exit(0 if main() == {"software-team": "done", "trading-desk": "reconciled"} else 1)
