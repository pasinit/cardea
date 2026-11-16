"""ADR-034's worked examples (`make teams`) against a real daemon instead of in-process.

    python3 teams.py [--work DIR]

The same files as `examples/teams` (collections, groups, the broker's fill trigger) and
the same fake operations (`packages/cardea-reference-ops`), applied over the daemon's
Unix socket. Each agent step is a run the daemon executes on its own queue, with the
scripted harness of `plan.py` making the step's tool calls over the run's MCP endpoint.

Software team: product files a work item; the lead triages it to the developers; a
developer claims it, opens a change and submits it; the principal approves it to `done`.
Then the group is restructured by its manifest alone (four developers, then three again),
and the supervisor reads the overview.

Trading desk: an analyst proposes an order; the risk agent's `approve` transition waits
for the human desk approver, who approves it; execution places the order, crashes, and its
retry is deduplicated; the broker's signed fill webhook wakes execution, which records the
fill; execution reconciles the order.

Identities are the development identity (`experiments/try`), not SPIFFE or OIDC.
"""

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    REPOSITORY,
    Evidence,
    Processes,
    add_common_arguments,
    claim,
    rows,
    run_script,
    use_kernel,
    wait_for,
)
from plan import set_plan, transcript  # noqa: E402
from stack import Stack, people, platform, rights  # noqa: E402

NAME = "teams"
EXAMPLES = REPOSITORY / "examples" / "teams"
RUN = ("objects.read", "objects.write", "models.invoke")
# A Standard Webhooks secret for the fake broker; generated per run, never a real one.
SECRET_BYTES = 32
APPROVER_SUBJECT = "acceptance-desk-approver"


def document(path: str) -> dict[str, Any]:
    return dict(json.loads((EXAMPLES / path).read_text()))


def groups() -> dict[str, dict[str, Any]]:
    return {name: document(f"{name}/group.json") for name in ("software-team", "trading-desk")}


def provisioning(issuer: str, triggers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """As the walkthrough's: each supervisor may delegate what its pools are granted.

    The desk approver is a human with an OIDC identity at the run's test issuer.
    """
    from cardea.service.models import RIGHTS

    permissions: list[Any] = []
    for manifest in groups().values():
        spec = manifest["spec"]
        granted = {*RIGHTS, *RUN, "groups.manage", *(g for p in spec["pools"] for g in p["grants"])}
        permissions += rights(
            spec["supervisor"], manifest["metadata"]["namespace"], sorted(granted)
        )
    platform_rows = platform(permissions)
    for row in platform_rows:
        if row["operation"] == "record:orders/approve":
            row["approve"] = True
    # A human desk approver may decide the `approve` transition of orders, nothing else.
    permissions += rights(
        "desk-approver", "desk", ["record:orders/approve"], allow=False, approve=True
    )
    return {
        "principals": [
            *people("runtime", "sup-eng", "sup-desk"),
            {
                "principal_id": "desk-approver",
                "kind": "human",
                "auth_kind": "oidc",
                "issuer": issuer,
                "subject": APPROVER_SUBJECT,
            },
        ],
        "permissions": permissions,
        "platform": platform_rows,
        "triggers": triggers or [],
    }


class Teams:
    def __init__(self, stack: Stack, evidence: Evidence, issuer: Any) -> None:
        self.stack = stack
        self.evidence = evidence
        self.issuer = issuer

    def approver(self, **mint: Any) -> tuple[str, str]:
        """An OIDC access token for the desk approver, from the run's test issuer."""
        return ("oidc", self.issuer.mint(mint.pop("subject", APPROVER_SUBJECT), **mint))

    def oidc_refusals(self) -> None:
        """Tokens the kernel must refuse: wrong audience, expired, no `openid`, unknown."""
        tries = {
            "wrong_audience": self.approver(audience="another-service"),
            "expired": self.approver(seconds=-30),
            "no_openid_scope": self.approver(scope="profile"),
            "unknown_subject": self.approver(subject="nobody"),
            "forged": ("oidc", self.approver()[1][:-4] + "AAAA"),
        }
        results = {
            name: self.stack.attempt(
                "desk-approver",
                "GET",
                "/approvals",
                credential=credential,
                params={"namespace": "desk", "state": "pending"},
            )
            for name, credential in tries.items()
        }
        self.evidence.check(
            "oidc_bad_tokens_refused",
            all(not r["ok"] and r["status"] in {401, 403} for r in results.values()),
            results=results,
        )

    def configure(self, example: str) -> dict[str, Any]:
        manifest = groups()[example]
        supervisor = manifest["spec"]["supervisor"]
        for path in sorted((EXAMPLES / example).glob("collection.*.json")):
            body = json.loads(path.read_text())
            self.stack.request(
                supervisor, "PUT", f"/collections/{body['metadata']['name']}", json=body
            )
        applied = self.stack.request(
            supervisor, "PUT", f"/groups/{manifest['metadata']['name']}", json=manifest
        )
        self.evidence.observe(f"{example}_applied", applied.get("plan"))
        return manifest

    def step(
        self,
        manifest: dict[str, Any],
        member: str,
        operations: list[str],
        actions: list[dict[str, Any]],
        attempts: int = 1,
    ) -> tuple[str, list[dict[str, Any]]]:
        """One run of a member, playing `actions` (one per attempt or continuation)."""
        pool = next(p for p in manifest["spec"]["pools"] if member.split(".")[1] == p["name"])
        set_plan(self.stack.work, member, actions)
        played = len(transcript(self.stack.work))
        task_id = self.stack.agent_run(
            manifest["spec"]["supervisor"],
            member,
            manifest["metadata"]["namespace"],
            objective=f"One step of {manifest['metadata']['name']}",
            instructions=pool["run"]["instructions"],
            operations=operations,
            grants=list(pool["grants"]),
            max_attempts=attempts,
            attempt_timeout_seconds=60,
        )
        state = self._settled(manifest["spec"]["supervisor"], task_id)
        return state, [t for t in transcript(self.stack.work)[played:] if t["task_id"] == task_id]

    def _settled(self, supervisor: str, task_id: str) -> str:
        def settled() -> str | None:
            state = str(self.stack.task(supervisor, task_id)["task"]["state"])
            return state if state in {"succeeded", "failed", "cancelled", "waiting"} else None

        return wait_for(settled, 300, 1.0)

    @staticmethod
    def result(played: list[dict[str, Any]], tool: str) -> Any:
        for attempt in reversed(played):
            for call in reversed(attempt["calls"]):
                if call["tool"] == tool:
                    return call["structured"] if not call["is_error"] else {"error": call["text"]}
        return None

    def state_of(self, namespace: str, record_id: str) -> str | None:
        found = rows(
            self.stack.database,
            "SELECT state FROM records WHERE namespace=? AND record_id=?",
            namespace,
            record_id,
        )
        return found[0]["state"] if found else None

    @staticmethod
    def move(collection: str, record_id: str, name: str, **extra: Any) -> dict[str, Any]:
        arguments = {"collection": collection, "record_id": record_id, "transition": name, **extra}
        return {"tool": "records.transition", "arguments": arguments}

    def software_team(self) -> None:
        ev = self.evidence
        team = self.configure("software-team")
        _, played = self.step(
            team,
            "eng-team.product.0",
            ["records.create"],
            [
                {
                    "calls": [
                        {
                            "tool": "records.create",
                            "arguments": {
                                "collection": "work",
                                "client_key": "login-slow",
                                "data": {"title": "Login page is slow", "body": "p95 is 4 s"},
                            },
                        }
                    ]
                }
            ],
        )
        item = self.result(played, "records.create")["record"]["record_id"]
        assign = [
            {
                "tool": "records.update",
                "arguments": {
                    "collection": "work",
                    "record_id": item,
                    "expected_version": 1,
                    "data": {"title": "Login page is slow", "assignee_pool": "eng-team.dev"},
                },
            },
            self.move("work", item, "triage", args={"assignee_pool": "eng-team.dev"}),
        ]
        self.step(
            team, "eng-team.lead.0", ["records.update", "records.transition"], [{"calls": assign}]
        )
        _, played = self.step(
            team,
            "eng-team.dev.1",
            ["records.transition"],
            [{"calls": [self.move("work", item, "start")]}],
        )
        epoch = self.result(played, "records.transition")["claim"]["claim_epoch"]
        _, played = self.step(
            team,
            "eng-team.dev.1",
            ["vcs.open_change", "records.transition"],
            [
                {
                    "calls": [
                        {
                            "tool": "vcs.open_change",
                            "arguments": {"record_id": item, "title": "Cache sessions"},
                        },
                        self.move("work", item, "submit", claim_epoch=epoch),
                    ]
                }
            ],
        )
        ev.check(
            "software_team_change_opened",
            bool((self.result(played, "vcs.open_change") or {}).get("change_id")),
        )
        self.step(
            team,
            "eng-team.principal.0",
            ["records.transition"],
            [{"calls": [self.move("work", item, "approve")]}],
        )
        ev.check(
            "software_team_item_done",
            self.state_of("eng", item) == "done",
            history=rows(
                self.stack.database,
                "SELECT kind, actor, at FROM record_events WHERE record_id=? ORDER BY event_id",
                item,
            ),
        )

        # Restructure by the manifest alone: a fourth developer, then back to three.
        for replicas in (4, 3):
            manifest = json.loads(json.dumps(team))
            next(p for p in manifest["spec"]["pools"] if p["name"] == "dev")["replicas"] = replicas
            applied = self.stack.request("sup-eng", "PUT", "/groups/eng-team", json=manifest)

            def converged() -> dict[str, Any] | None:
                view = self.stack.request(
                    "sup-eng", "GET", "/groups/eng-team", params={"namespace": "eng"}
                )
                status = view["status"]
                done = status["observedGeneration"] == view["metadata"]["generation"]
                return view if done else None

            view = wait_for(converged, 600, 5)
            active = [
                m["principal_id"]
                for m in view["status"]["members"]
                if m["pool"] == "dev" and m["state"] == "active"
            ]
            ev.check(
                f"software_team_restructured_to_{replicas}_developers",
                len(active) == replicas,
                plan=applied.get("plan"),
                active=active,
            )
        overview = self.stack.request(
            "sup-eng", "GET", "/groups/eng-team/overview", params={"namespace": "eng", "events": 5}
        )
        ev.check(
            "software_team_overview_reads",
            {p["name"] for p in overview["pools"]} >= {"dev", "lead", "principal", "product"},
            generation=overview["generation"],
            as_of=overview["as_of"],
        )

    def trading_desk(self) -> None:
        ev, st = self.evidence, self.stack
        desk = self.configure("trading-desk")
        _, played = self.step(
            desk,
            "desk.analyst.0",
            ["records.create", "risk.check"],
            [
                {
                    "calls": [
                        {"tool": "risk.check", "arguments": {}},
                        {
                            "tool": "records.create",
                            "arguments": {
                                "collection": "orders",
                                "client_key": "xyz-1",
                                "data": {
                                    "instrument": "XYZ",
                                    "side": "buy",
                                    "qty": 10,
                                    "limit_price": 10.5,
                                    "rationale": "Mean reversion",
                                },
                            },
                        },
                    ]
                }
            ],
        )
        order = self.result(played, "records.create")["record"]["record_id"]
        state, _ = self.step(
            desk,
            "desk.risk.0",
            ["records.transition"],
            [{"calls": [self.move("orders", order, "approve")]}, {"calls": []}],
        )
        ev.check("trading_desk_approve_waits", state == "waiting", state=state)
        self.oidc_refusals()
        before = self.issuer.introspections
        pending = st.request(
            "desk-approver",
            "GET",
            "/approvals",
            credential=self.approver(),
            params={"namespace": "desk", "state": "pending"},
        )["approvals"]
        ev.check(
            "oidc_approver_token_introspected_by_the_kernel",
            self.issuer.introspections > before,
            introspections=self.issuer.introspections - before,
        )
        ev.check("trading_desk_approver_sees_one_request", len(pending) == 1)
        request = pending[0]
        decided = st.request(
            "desk-approver",
            "POST",
            f"/approvals/{request['request_id']}/decision",
            credential=self.approver(),
            json={
                "decision_id": "d-1",
                "decision": "approve",
                "intent_digest": request["intent_digest"],
            },
        )
        wait_for(lambda: self.state_of("desk", order) == "approved", 300, 2)
        ev.check("trading_desk_held_transition_ran", self.state_of("desk", order) == "approved")
        decisions = rows(
            st.database,
            "SELECT approver_id, decision FROM approval_decisions WHERE request_id=?",
            request["request_id"],
        )
        ev.check(
            "oidc_human_decision_recorded",
            [(d["approver_id"], d["decision"]) for d in decisions]
            == [("desk-approver", "approve")],
            decided=decided,
            decisions=decisions,
        )

        place = {
            "tool": "broker.place_order",
            "arguments": {"order_ref": order, "record_id": order, "qty": 10},
        }
        _, played = self.step(
            desk,
            "desk.execution.0",
            ["broker.place_order", "records.transition"],
            [
                {"calls": [place], "outcome": "crash"},
                {"calls": [place, self.move("orders", order, "place")]},
            ],
            attempts=2,
        )
        placed = [
            c["structured"] for a in played for c in a["calls"] if c["tool"] == "broker.place_order"
        ]
        ids = {(p or {}).get("broker_order_id") for p in placed}
        ev.check(
            "trading_desk_placement_deduplicated_across_crash",
            len(placed) == 2 and len(ids) == 1 and self.state_of("desk", order) == "placed",
            placements=placed,
        )
        epoch = self.result(played, "records.transition")["claim"]["claim_epoch"]

        secret = "whsec_" + base64.b64encode(_random(SECRET_BYTES)).decode()
        path = st.work / "broker-fills.secret"
        path.write_text(secret + "\n")
        path.chmod(0o600)
        fills = document("trading-desk/trigger.fills.json")
        fills["event"]["secret_file"] = str(path)
        st.provision(provisioning(self.issuer.url, [fills]))
        set_plan(
            st.work,
            "desk.execution.0",
            [{"calls": [self.move("orders", order, "fill", claim_epoch=epoch)]}],
        )
        delivered = deliver(st, "desk-fills", secret, order)
        ev.check(
            "trading_desk_fill_webhook_admitted",
            delivered.get("outcome") == "admitted",
            delivery=delivered,
        )
        if delivered.get("task_id"):
            self._settled("sup-desk", str(delivered["task_id"]))
        ev.check("trading_desk_fill_recorded", self.state_of("desk", order) == "filled")
        self.step(
            desk,
            "desk.execution.0",
            ["broker.fills", "records.transition"],
            [
                {
                    "calls": [
                        {
                            "tool": "broker.fills",
                            "arguments": {"order_ref": order, "record_id": order},
                        },
                        self.move("orders", order, "reconcile", claim_epoch=epoch),
                    ]
                }
            ],
        )
        ev.check("trading_desk_reconciled", self.state_of("desk", order) == "reconciled")


def _random(size: int) -> bytes:
    import os

    return os.urandom(size)


def sign(secret: str, message_id: str, stamp: int, body: bytes) -> str:
    """A Standard Webhooks v1 signature, computed here rather than with the kernel's library."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{message_id}.{stamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def deliver(stack: Stack, trigger: str, secret: str, order: str) -> dict[str, Any]:
    """POST the broker's fill to the daemon's Unix socket, with no bearer credential."""
    import httpx

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
    stamp = int(time.time())
    headers = {
        "webhook-id": f"msg-{order}",
        "webhook-timestamp": str(stamp),
        "webhook-signature": sign(secret, f"msg-{order}", stamp, body),
        "content-type": "application/cloudevents+json",
    }
    transport = httpx.HTTPTransport(uds=str(stack.store / "kernel.sock"))
    with httpx.Client(transport=transport, base_url="http://cardea", timeout=30) as client:
        response = client.post(f"/v0.2/events/{trigger}", content=body, headers=headers)
    return {"status": response.status_code, **response.json()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_arguments(parser, NAME)
    args = parser.parse_args(argv)
    lib = use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    processes = Processes(work)

    def body() -> None:
        from oidc_issuer import Issuer

        with Issuer() as issuer:
            secret = work / "oidc-client-secret"
            secret.write_text(issuer.client_secret + "\n")
            secret.chmod(0o600)
            oidc = {
                "issuer": issuer.url,
                "client_id": issuer.client_id,
                "client_secret_file": str(secret),
                "allow_loopback_http": True,
            }
            evidence.observe("oidc_issuer", issuer.metadata())
            stack = Stack(work, processes, lib, reference_ops=True, oidc=oidc)
            stack.provision(provisioning(issuer.url))
            stack.start()
            teams = Teams(stack, evidence, issuer)
            for example in (teams.software_team, teams.trading_desk):
                try:
                    example()
                except Exception as error:  # noqa: BLE001 - one example must not hide the other
                    evidence.check(
                        f"{example.__name__}_completed",
                        False,
                        error=f"{type(error).__name__}: {error}",
                    )
            stack.stop()

    try:
        return run_script(evidence, body)
    finally:
        processes.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
