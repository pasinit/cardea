"""A deployable agent dev team: render, apply, pause, resume, stop and status (ADR-034).

    python3 -I deploy/team-kit/teamkit.py render  --host HOST.json --out DIR
    python3 -I deploy/team-kit/teamkit.py provision --host HOST.json [--paused]
    python3 -I deploy/team-kit/teamkit.py apply   --host HOST.json
    python3 -I deploy/team-kit/teamkit.py status  --host HOST.json
    python3 -I deploy/team-kit/teamkit.py pause   --host HOST.json
    python3 -I deploy/team-kit/teamkit.py resume  --host HOST.json
    python3 -I deploy/team-kit/teamkit.py stop    --host HOST.json

Everything host-specific comes from the private host file (`team.host.example.json`
shows its shape): the namespace, the supervisor's identity, the repository alias and its
default branch (`default_branch`, default `main`), the credential names, the email alias, the
model and the budgets. The kit itself is generic. Roles that use repository operations are
told the alias and branch in their instructions, since they are granted that alias only.

What each command does:
- `render` writes the documents the team is made of, for review: the `work` and
  `decisions` collections, the agent group (pools, grants, triggers, spend ceilings), the
  provisioning fragment for the supervisor, and the fragments of the vcs and email host
  files that name this team's pools.
- `provision` merges the team's principals and grants into the host owner's base
  provisioning document and runs `cardea provision` with it (as root). `--paused` writes the
  supervisor disabled: the kill switch, because every member run's authority is checked
  against its supervisor's live authority.
- `apply` applies the collections and the group as the supervisor, over the kernel socket.
- `status` prints the team overview: members and their runs, work by state and claim,
  mail, approvals and the spend and run ceilings.
- `pause` cancels every member's unfinished run, then provisions the supervisor disabled.
  Nothing starts again until `resume`, which provisions it enabled.
- `stop` pauses, then deletes the group (its members drain; the collections and records
  stay, for the record).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

POOLS = ("pm", "tl", "dev", "pe")
KIT_PYTHON = ("/opt/cardea/apps/api/.venv/bin/python",)
# Rights every team supervisor needs besides the pools' grants (ADR-034, ADR-027).
SUPERVISOR_RIGHTS = (
    "groups.manage",
    "models.invoke",
    "objects.read",
    "objects.write",
    "tasks.inspect",
    "tasks.cancel",
    "tasks.resume",
    "tasks.submit",
    "events.read",
)
DEFAULT_SCHEDULES = {"pm": ["0 9 * * 1-5", "0 15 * * 1-5"], "tl": ["30 8 * * 1-5"]}
DEFAULT_BUDGETS: dict[str, Any] = {
    "team": {"runs": 30, "usd": 20.0},
    "roles": {"pm": {"runs": 4}, "tl": {"runs": 6}, "dev": {"runs": 14}, "pe": {"runs": 8}},
    "max_cost_usd_per_run": 1.0,
}


DEFAULT_BRANCH = "main"


class HostError(ValueError):
    """The host file is missing something the kit needs."""


def load_host(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise HostError("the host file is not a JSON object")
    for key in ("namespace", "supervisor", "repository"):
        if key not in document:
            raise HostError(f"the host file needs {key!r}")
    supervisor = document["supervisor"]
    if not isinstance(supervisor, dict) or "principal_id" not in supervisor:
        raise HostError("supervisor needs principal_id and its identity")
    branch = document.get("default_branch", DEFAULT_BRANCH)
    if not isinstance(branch, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch
    ):
        raise HostError("default_branch must be a branch name")
    return document


def repository_note(host: dict[str, Any], operations: list[str]) -> str:
    """Names the repository and its default branch for roles whose operations need them.

    Repository-scoped operations are granted for one alias only (`repo:<alias>`), so a role
    that guesses the name is refused; the instructions state it.
    """
    if not any(op.startswith("vcs.") or op in ("code.session", "env.try") for op in operations):
        return ""
    repo, branch = str(host["repository"]), str(host.get("default_branch", DEFAULT_BRANCH))
    argument = "`repository`" + (" (`repo` for env.try)" if "env.try" in operations else "")
    return (
        f" The repository is `{repo}`: pass exactly that as {argument}. "
        f"Its default branch is `{branch}`."
    )


def _names(host: dict[str, Any]) -> tuple[str, str, str, str]:
    group = str(host.get("group", "dev-team"))
    return str(host["namespace"]), group, str(host["supervisor"]["principal_id"]), group


def _mail(group: str, *pools: str) -> list[str]:
    return [f"mail:{group}.{pool}" for pool in pools]


def work_collection(host: dict[str, Any]) -> dict[str, Any]:
    namespace, group, supervisor, _ = _names(host)

    def pool(name: str) -> str:
        return f"'{group}.{name}'"

    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "Collection",
        "metadata": {"name": "work", "namespace": namespace},
        "spec": {
            "supervisor": supervisor,
            "schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "change": {"type": "integer"},
                },
                "required": ["title"],
            },
            # One claim per item and one item per developer at a time.
            "claims": {"lease_seconds": 14400, "max_per_holder": 1, "max_followups": 6},
            "states": [
                "open",
                "ready",
                "in_progress",
                "in_review",
                "approved",
                "done",
                "cancelled",
            ],
            "initial": "open",
            "terminal": ["done", "cancelled"],
            "transitions": [
                {
                    "name": "triage",
                    "from": ["open"],
                    "to": "ready",
                    "guard": f"caller.pool == {pool('tl')}",
                },
                {
                    "name": "start",
                    "from": ["ready"],
                    "to": "in_progress",
                    "claim": "acquire",
                    "on_claim_expiry": "ready",
                    "guard": f"caller.pool == {pool('dev')}",
                },
                {
                    "name": "submit",
                    "from": ["in_progress"],
                    "to": "in_review",
                    "claim": "holder",
                    "requires_receipt": {"operation": "vcs.open_change", "in": "same_task"},
                },
                {
                    "name": "request_changes",
                    "from": ["in_review"],
                    "to": "in_progress",
                    "guard": f"caller.pool == {pool('pe')}",
                },
                {
                    "name": "approve",
                    "from": ["in_review"],
                    "to": "approved",
                    "guard": f"caller.pool == {pool('pe')}",
                    # The kernel checks the cited review approved (ADR-034 amendment).
                    "requires_receipt": {
                        "operation": "vcs.review",
                        "in": "same_task",
                        "result": "result.state == 'APPROVED'",
                    },
                },
                {
                    "name": "merge",
                    "from": ["approved"],
                    "to": "done",
                    "claim": "release",
                    "requires_receipt": {"operation": "vcs.merge", "in": "same_task"},
                },
                {
                    "name": "cancel",
                    "from": ["open", "ready", "in_progress", "in_review", "approved"],
                    "to": "cancelled",
                    "claim": "release",
                    "guard": f"caller.pool == {pool('tl')}",
                },
            ],
        },
    }


def decisions_collection(host: dict[str, Any]) -> dict[str, Any]:
    namespace, _, supervisor, _ = _names(host)
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "Collection",
        "metadata": {"name": "decisions", "namespace": namespace},
        "spec": {
            "supervisor": supervisor,
            "schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "decision": {"type": "string"}},
                "required": ["title", "decision"],
            },
        },
    }


def _mail_trigger() -> dict[str, Any]:
    return {"event": {"types": ["cardea.mailbox.sent"], "subjects": ["$member", "$pool"]}}


def _state_trigger(state: str | None = None, transition: str | None = None) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [{"prefix": {"subject": "work/"}}]
    if state is not None:
        filters.append({"exact": {"recordstate": state}})
    if transition is not None:
        filters.append({"exact": {"transition": transition}})
    return {"event": {"types": ["cardea.record.transitioned"], "filters": filters}}


def group_document(host: dict[str, Any]) -> dict[str, Any]:
    namespace, group, supervisor, _ = _names(host)
    repo = str(host["repository"])
    branch = str(host.get("default_branch", DEFAULT_BRANCH))
    zone = str(host.get("timezone", "UTC"))
    budgets = {**DEFAULT_BUDGETS, **(host.get("budgets") or {})}
    roles = {**DEFAULT_BUDGETS["roles"], **(budgets.get("roles") or {})}
    per_run = float(budgets.get("max_cost_usd_per_run", 1.0))
    schedules = {**DEFAULT_SCHEDULES, **(host.get("schedules") or {})}
    scenarios = list((host.get("env_try") or {}).get("scenarios", ["try"]))
    email = bool((host.get("email") or {}).get("alias"))
    base = ["objects.read", "objects.write", "models.invoke", "mailbox.send", "mailbox.inbox"]
    harness = host.get("harness")
    if harness:
        base.append(f"harness:{harness}")

    def run(instructions: str, operations: list[str], timeout: int, **extra: Any) -> dict[str, Any]:
        template: dict[str, Any] = {
            "instructions": instructions + repository_note(host, operations),
            "operations": [*operations, "mailbox.send"],
            "context": [
                {"operation": "mailbox.inbox"},
                {"operation": "records.summary", "arguments": {"collection": "work"}},
                *extra.pop("context", []),
            ],
            "bounds": {
                "timeout_seconds": timeout,
                "run_timeout_seconds": timeout,
                "attempts": 1,
                "max_cost_usd": per_run,
            },
            **extra,
        }
        if host.get("model"):
            template["model"] = host["model"]
        if harness:
            template["harness"] = harness
        return template

    def spend(role: str) -> dict[str, Any]:
        account: dict[str, Any] = {"window": "day", "timezone": zone}
        account.update({k: v for k, v in roles[role].items() if k in ("runs", "usd")})
        return account

    def schedule(cron: str) -> dict[str, Any]:
        return {"schedule": {"cron": cron, "timezone": zone, "misfire": "skip"}}

    pm_ops = ["vcs.read_branch", "env.try", "records.create", "records.query"]
    if email:
        pm_ops.append("email.send")
    pools = [
        {
            "name": "pm",
            "replicas": 1,
            "grants": [
                *base,
                "record:work/create",
                "record:work/read",
                "vcs.read_branch",
                "env.try",
                f"repo:{repo}",
                *_mail(group, "tl"),
                *(["email.send"] if email else []),
            ],
            "run": run(
                f"You are the product manager. Read the head of {branch} with vcs.read_branch, "
                "try it with "
                f"env.try on the scenarios {', '.join(scenarios)} (pass the head commit), and "
                "file every failure or rough edge you find as a work item with what you saw. "
                "Do not file duplicates of open items. Tell the team lead what you filed.",
                pm_ops,
                3600,
            ),
            "triggers": [*(schedule(c) for c in schedules["pm"]), _mail_trigger()],
            "spend": spend("pm"),
        },
        {
            "name": "tl",
            "replicas": 1,
            "grants": [
                *base,
                "record:work/create",
                "record:work/read",
                "record:work/update",
                "record:work/triage",
                "record:work/cancel",
                "record:decisions/create",
                "record:decisions/read",
                *_mail(group, "dev", "pm", "pe"),
            ],
            "run": run(
                "You are the team lead. Plan: keep the open work prioritised, split work that is "
                "too large, cancel what is not worth doing, and triage the next item for the "
                "developers when they have nothing ready. Record decisions. Answer mail.",
                ["records.query", "records.create", "records.update", "records.transition"],
                900,
            ),
            "triggers": [
                *(schedule(c) for c in schedules["tl"]),
                {
                    "event": {
                        "types": ["cardea.record.created"],
                        "filters": [{"prefix": {"subject": "work/"}}],
                    }
                },
                _mail_trigger(),
            ],
            "spend": spend("tl"),
        },
        {
            "name": "dev",
            "replicas": int(host.get("developers", 2)),
            "grants": [
                *base,
                "record:work/read",
                "record:work/start",
                "record:work/submit",
                "record:work/merge",
                "code.session",
                "vcs.push_branch",
                "vcs.open_change",
                "vcs.read_change",
                "vcs.merge",
                f"repo:{repo}",
                *_mail(group, "tl", "pe"),
            ],
            "run": run(
                "You are a developer. If you hold an item that is approved, merge its change with "
                "vcs.merge and move the item to done with the merge transition. If you hold an "
                "item with changes requested, read the review, change the code with a coding "
                "session, push and submit again. Otherwise start one ready item (only one "
                "developer gets its claim; if start is refused, stop), implement it with a "
                "coding session, push the change, open a pull request and submit the item. "
                "If you are blocked, tell the team lead.",
                [
                    "records.query",
                    "records.transition",
                    "code.session",
                    "vcs.push_branch",
                    "vcs.open_change",
                    "vcs.read_change",
                    "vcs.merge",
                ],
                3600,
                context=[{"operation": "records.claimed", "arguments": {"collection": "work"}}],
            ),
            "triggers": [
                _state_trigger(state="ready"),
                _state_trigger(state="approved"),
                _state_trigger(transition="request_changes"),
                _mail_trigger(),
            ],
            "spend": spend("dev"),
        },
        {
            "name": "pe",
            "replicas": 1,
            "grants": [
                *base,
                "record:work/read",
                "record:work/approve",
                "record:work/request_changes",
                "record:decisions/create",
                "record:decisions/read",
                "vcs.read_change",
                "vcs.review",
                "vcs.comment",
                f"repo:{repo}",
                *_mail(group, "dev", "tl"),
            ],
            "run": run(
                "You are the principal engineer. Review each item in review: read its pull "
                "request, then submit a review. Approve (vcs.review APPROVE, then the approve "
                "transition) only when the change is correct, tested and in scope; otherwise "
                "request changes with concrete reasons. Record architecture decisions.",
                [
                    "records.query",
                    "records.transition",
                    "records.create",
                    "vcs.read_change",
                    "vcs.review",
                    "vcs.comment",
                ],
                1800,
            ),
            "triggers": [_state_trigger(state="in_review"), _mail_trigger()],
            "spend": spend("pe"),
        },
    ]
    team = {k: v for k, v in budgets["team"].items() if k in ("runs", "usd")}
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": group, "namespace": namespace},
        "spec": {
            "supervisor": supervisor,
            "collections": ["work", "decisions"],
            "spend": {**team, "window": "day", "timezone": zone},
            "pools": pools,
        },
    }


def provisioning_fragment(host: dict[str, Any], *, paused: bool = False) -> dict[str, Any]:
    """The supervisor principal and its grants in the team's namespace, with the platform
    ceiling for them."""
    namespace, _, supervisor, _ = _names(host)
    group = group_document(host)
    rights = sorted({*SUPERVISOR_RIGHTS, *(g for p in group["spec"]["pools"] for g in p["grants"])})
    expires = (datetime.now(UTC) + timedelta(days=int(host.get("grant_days", 14)))).isoformat()
    grant = {"namespace": namespace, "expires_at": expires}
    principal = {"kind": "human", **host["supervisor"], "disabled": paused}
    return {
        "principals": [principal],
        "permissions": [
            {"principal_id": supervisor, "operation": r, "delegate": True, **grant} for r in rights
        ],
        "platform": [{"principal_id": "platform", "operation": r, **grant} for r in rights],
    }


def merge_provisioning(
    base: dict[str, Any], fragment: dict[str, Any], namespace: str
) -> dict[str, Any]:
    """The host owner's document with the team's rows replaced (provisioning is a whole
    document: anything absent is removed)."""
    (principal,) = fragment["principals"]
    supervisor = principal["principal_id"]
    ours = {r["operation"] for r in fragment["platform"]}
    merged = dict(base)
    merged["principals"] = [
        p for p in base.get("principals", []) if p.get("principal_id") != supervisor
    ] + [principal]
    merged["permissions"] = [
        p
        for p in base.get("permissions", [])
        if not (p.get("principal_id") == supervisor and p.get("namespace") == namespace)
    ] + fragment["permissions"]
    merged["platform"] = [
        p
        for p in base.get("platform", [])
        if not (p.get("namespace") == namespace and p.get("operation") in ours)
    ] + fragment["platform"]
    return merged


def vcs_fragment(host: dict[str, Any]) -> dict[str, Any]:
    """The part of the host's vcs.json that names this team's credentials (cardea-vcs-ops)."""
    _, group, _, _ = _names(host)
    names = host.get("credentials") or {}
    author, reviewer = names.get("author"), names.get("reviewer")
    if not author or not reviewer:
        raise HostError("credentials needs author and reviewer names")
    expires = names.get("expires_on", "YYYY-MM-DD")
    fragment: dict[str, Any] = {
        "credentials": {
            "kind": "personal_access_tokens",
            "tokens": {author: {"expires_on": expires}, reviewer: {"expires_on": expires}},
            "default": author,
            "pools": {f"{group}.pe": reviewer},
        },
        "allow_approve": [f"{group}.pe"],
    }
    if names.get("reviewer_login"):
        fragment["merge"] = {"reviewer_login": names["reviewer_login"]}
    return fragment


def email_fragment(host: dict[str, Any]) -> dict[str, Any]:
    """The `roles` entry of the host's email.json for the PM (cardea-email-ops), if any."""
    _, group, _, _ = _names(host)
    email = host.get("email") or {}
    if not email.get("alias"):
        return {}
    limit = email.get("rate_limit", {"count": 3, "window_days": 7})
    return {"roles": {f"{group}.pm": {"recipients": [email["alias"]], "rate_limit": limit}}}


def render(host: dict[str, Any], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    documents = {
        "collection.work.json": work_collection(host),
        "collection.decisions.json": decisions_collection(host),
        "group.json": group_document(host),
        "provisioning.team.json": provisioning_fragment(host),
        "vcs.team.json": vcs_fragment(host),
        "email.team.json": email_fragment(host),
    }
    written = []
    for name, document in documents.items():
        path = out / name
        path.write_text(json.dumps(document, indent=2) + "\n")
        path.chmod(0o600)
        written.append(path)
    return written


# Commands against a running kernel


def client(host: dict[str, Any]) -> Any:
    from cardea.service.client import KernelClient
    from cardea.service.login import session_credentials

    kernel = host.get("kernel") or {}
    # The kernel's private Unix socket, `<data_dir>/kernel.sock`.
    socket = Path(kernel.get("socket", "/var/lib/cardea/kernel.sock"))
    if kernel.get("session"):
        session = Path(kernel["session"])
        return KernelClient(socket, lambda: session_credentials(session))
    spiffe = kernel.get("spiffe") or {}
    if not spiffe:
        raise HostError("kernel needs a session file or a spiffe identity for the supervisor")
    return KernelClient.for_workload(
        socket,
        spiffe["socket"],
        spiffe["id"],
        spiffe.get("audience", "cardea-kernel"),
    )


def placeholders(value: Any, path: str = "") -> list[str]:
    """Host-file values still holding the example's placeholders: any string ending in
    `.invalid` or `example` (for example `reviewer.example`, `issuer.example.invalid`)."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in placeholders(v, f"{path}/{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in placeholders(v, f"{path}/{i}")]
    if isinstance(value, str) and value.rstrip("/").endswith((".invalid", "example")):
        return [path or "/"]
    return []


def check_placeholders(host: dict[str, Any]) -> None:
    found = placeholders(host)
    if found:
        raise HostError("the host file still holds placeholder values at " + ", ".join(found))


def provision(host: dict[str, Any], *, paused: bool) -> Path:
    check_placeholders(host)
    namespace = str(host["namespace"])
    settings = host.get("provisioning") or {}
    base = Path(settings.get("base", "/etc/cardea/provision.base.json"))
    out = Path(settings.get("out", "/etc/cardea/provision.json"))
    document = merge_provisioning(
        json.loads(base.read_text()), provisioning_fragment(host, paused=paused), namespace
    )
    staged = out.with_name(f".{out.name}.{os.getpid()}")
    staged.write_text(json.dumps(document, indent=2) + "\n")
    staged.chmod(0o600)
    os.replace(staged, out)
    # The kernel's own CLI, from the kit's environment (`python -I -m cardea.service`).
    cardea = list(settings.get("cardea", [*KIT_PYTHON, "-I", "-m", "cardea.service"]))
    config = settings.get("config", "/etc/cardea/config.json")
    subprocess.run([*cardea, "provision", "--config", config, str(out)], check=True)
    return out


def apply(host: dict[str, Any]) -> list[str]:
    check_placeholders(host)
    namespace, group, _, _ = _names(host)
    done = []
    kernel = client(host)
    try:
        for document in (work_collection(host), decisions_collection(host)):
            name = document["metadata"]["name"]
            kernel.request("PUT", f"/collections/{name}", json=document)
            done.append(f"collection {name}")
        kernel.request("PUT", f"/groups/{group}", json=group_document(host))
        done.append(f"group {group} in {namespace}")
    finally:
        kernel.close()
    return done


def overview(host: dict[str, Any]) -> dict[str, Any]:
    namespace, group, _, _ = _names(host)
    kernel = client(host)
    try:
        result: dict[str, Any] = kernel.request(
            "GET", f"/groups/{group}/overview", params={"namespace": namespace}
        )
        return result
    finally:
        kernel.close()


def summarise(view: dict[str, Any]) -> list[str]:
    """A short, plain status from a group overview."""
    lines = [f"as of {view.get('as_of')}"]
    for pool in view.get("pools", []):
        for member in pool.get("members", []):
            active = member.get("active_task_id") or "idle"
            last = (member.get("last_run") or {}).get("outcome", "-")
            lines.append(f"  {member.get('principal_id')}: {active} (last {last})")
    for collection in view.get("collections", []):
        counts = ", ".join(
            f"{k} {v}" for k, v in sorted((collection.get("by_state") or {}).items())
        )
        lines.append(f"  {collection.get('name')}: {counts or 'empty'}")
    spend = view.get("spend") or {}
    for account in spend.get("accounts", []):
        runs = f", runs {account['used_runs']}/{account['runs']}" if account.get("runs") else ""
        usd = f"usd {account['used_usd']}/{account['usd']}" if account.get("usd") else ""
        lines.append(f"  spend {account['account']} {account['window']}: {usd}{runs}")
    return lines


def pause(host: dict[str, Any]) -> list[str]:
    """Cancel every unfinished member run, then disable the supervisor (the kill switch)."""
    cancelled = cancel_runs(host)
    provision(host, paused=True)
    return cancelled


def cancel_runs(host: dict[str, Any]) -> list[str]:
    """Cancel every member's unfinished run, while the supervisor still has authority."""
    cancelled = []
    view = overview(host)
    kernel = client(host)
    try:
        for pool in view.get("pools", []):
            for member in pool.get("members", []):
                task = member.get("active_task_id")
                if task:
                    kernel.request(
                        "POST", f"/tasks/{task}/cancel", json={"request_id": str(uuid.uuid4())}
                    )
                    cancelled.append(str(task))
    finally:
        kernel.close()
    return cancelled


def stop(host: dict[str, Any]) -> list[str]:
    """Cancel the runs, delete the group (its members drain), then disable the supervisor."""
    _, group, _, _ = _names(host)
    namespace = str(host["namespace"])
    cancelled = cancel_runs(host)
    kernel = client(host)
    try:
        kernel.request("DELETE", f"/groups/{group}", params={"namespace": namespace})
    finally:
        kernel.close()
    provision(host, paused=True)
    return cancelled


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "provision", "apply", "status", "pause", "resume", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--host", type=Path, required=True, help="the private host file")
        if name == "render":
            command.add_argument("--out", type=Path, required=True)
        if name == "provision":
            command.add_argument("--paused", action="store_true")
    args = parser.parse_args(argv)
    host = load_host(args.host)
    if args.command == "render":
        for path in render(host, args.out):
            print(path)
    elif args.command == "provision":
        print(provision(host, paused=args.paused))
    elif args.command == "apply":
        print("\n".join(apply(host)))
    elif args.command == "status":
        print("\n".join(summarise(overview(host))))
    elif args.command == "pause":
        print(f"cancelled {len(pause(host))} run(s); the supervisor is disabled")
    elif args.command == "resume":
        provision(host, paused=False)
        print("the supervisor is enabled; triggers admit again")
    elif args.command == "stop":
        print(f"group deleted; cancelled {len(stop(host))} run(s); the supervisor is disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
