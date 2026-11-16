"""The `vcs.*` operations and their reconcilers (ADR-037 slices 4 and 5).

Each effect declares an ADR-026 idempotency scope. After an interruption the kernel calls
its reconciler, which reads the host and never sends again:

| operation         | reconciler reads                                   |
|-------------------|----------------------------------------------------|
| `vcs.push_branch` | the branch's head equals the named commit          |
| `vcs.open_change` | the pull request for the head branch               |
| `vcs.comment`     | a comment carrying the marker `cardea:<effect key>` |
| `vcs.review`      | a review carrying the same marker                  |

`vcs.check_repository` is a read: it reports whether the repository's rules are what
the agent team relies on, and enforces nothing.

Rules checked here, before any network call:
- the repository is an alias from host configuration (its `repo:<alias>` grant is checked
  by the kernel, from the descriptor's `resource`);
- a pushed branch, and a pull request's head, start with the caller's group prefix;
- `APPROVE` reviews are refused unless the host owner allows them;
- the change and every text that leaves the host pass the kernel's content gate first
  (`OperationContext.check_content`, ADR-037 slice 6), which fails closed.
"""

import asyncio
import os
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import JsonValue

from cardea.service import repositories
from cardea.service.operations import OperationContext, OperationRejected, Reconciliation
from cardea_vcs_ops.config import Repository, VcsConfig
from cardea_vcs_ops.credentials import (
    Credential,
    CredentialUnavailable,
    Permission,
    RepositoryCredentials,
    provider,
)
from cardea_vcs_ops.git import BundleInvalid, Pusher, PushRejected, Workspace, push
from cardea_vcs_ops.github import DuplicatePull, GitHub, HostRefused

BRANCH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
MAX_BODY = 4096
MAX_REVIEW_BODY = 500
REVIEW_EVENTS = ("COMMENT", "REQUEST_CHANGES", "APPROVE")
# A branch name probed for the rules on each side of the agent prefix; never created.
PROBE = "cardea-readiness-probe"
BLOCKS_PUSH = ("creation", "update")


def marker(context: OperationContext) -> str:
    """Hidden in a comment or review so the reconciler can find it again."""
    return f"<!-- cardea:{context.effect_key} -->"


def _bounded(text: object, limit: int) -> tuple[str, bool]:
    value = "" if text is None else str(text)
    return value[:limit], len(value) > limit


class Vcs:
    def __init__(
        self,
        config: VcsConfig,
        credentials: RepositoryCredentials | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pusher: Pusher = push,
        registry: repositories.Registry | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        """`registry` is the host's repository registry; by default it is read from
        `CARDEA_REPOSITORIES`, and without one the config's deprecated `repositories` apply.
        A config that still lists repositories and disagrees with the registry is refused."""
        self.config = config
        self.credentials = credentials or provider(config.credentials)
        self.transport = transport
        self.pusher = pusher
        if registry is None:
            legacy = {a: r.model_dump(mode="json") for a, r in config.repositories.items()}
            registry = repositories.resolve(
                "vcs",
                legacy or None,
                legacy_prefix=config.branch_prefix if legacy else None,
                environ=environ,
            )
        self.registry = registry

    # Shared checks

    def repository(self, alias: object) -> Repository:
        if self.registry is None:
            found = self.config.repositories.get(str(alias))
        else:
            entry = self.registry.repositories.get(str(alias))
            found = (
                Repository(
                    owner=str(entry.owner),
                    name=str(entry.name),
                    api_url=str(entry.api_url),
                    git_url=str(entry.git_url),
                )
                if entry is not None and entry.hosted
                else None
            )
        if found is None:
            raise OperationRejected("No such repository alias on this host", "repository_unknown")
        return found

    def branch_prefix(self, alias: object) -> str:
        if self.registry is None:
            return self.config.branch_prefix
        return self.registry.repositories[str(alias)].branch_prefix

    def check_branch(self, branch: str, context: OperationContext, alias: object) -> None:
        """Agent branches live under the caller's group prefix (ADR-037 open question 3),
        which the registry sets per repository."""
        if context.group is None:
            raise OperationRejected(
                "Only agent-group members push branches or open changes", "branch_not_allowed"
            )
        prefix = self.branch_prefix(alias).format(group=context.group)
        if (
            not branch.startswith(prefix)
            or len(branch) <= len(prefix)
            or not BRANCH.fullmatch(branch)
            or ".." in branch
            or branch.endswith((".lock", "."))
        ):
            raise OperationRejected(f"Branches must be named {prefix}<name>", "branch_not_allowed")

    @staticmethod
    def gate(
        context: OperationContext,
        operation: str,
        *,
        diff: str | None = None,
        texts: dict[str, str] | None = None,
    ) -> None:
        """The kernel's content gate (ADR-037 slice 6), before anything is sent.

        It raises `OperationRejected` (`content_refused`, or the gate's own code when it is
        unavailable) on a finding. A call the kernel did not bind a gate to is refused too:
        the gate fails closed.
        """
        if context.check_content is None:
            raise OperationRejected(
                "The content gate is not available to this call", "content_gate_unavailable"
            )
        context.check_content(operation=operation, diff=diff, texts=texts)

    async def credential(
        self, repository: Repository, permissions: tuple[Permission, ...]
    ) -> Credential:
        try:
            return await self.credentials.credential(repository, permissions)
        except CredentialUnavailable as missing:
            # Nothing was sent: a definite refusal, not an uncertain effect.
            raise OperationRejected(str(missing), "credential_unavailable") from None

    async def client(self, repository: Repository, *permissions: Permission) -> GitHub:
        credential = await self.credential(repository, permissions)
        return GitHub(repository, credential, self.transport)

    # vcs.push_branch

    async def push_branch(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        repository = self.repository(arguments["repository"])
        branch, head = str(arguments["branch"]), str(arguments["head"])
        self.check_branch(branch, context, arguments["repository"])
        bundle = context.inputs.get("/change")
        if bundle is None:
            raise OperationRejected("The change bundle was not staged", "change_invalid")
        credential = await self.credential(repository, ("contents:write",))
        done: dict[str, JsonValue] = {
            "repository": str(arguments["repository"]),
            "branch": branch,
            "head": head,
        }
        with Workspace(bundle, head, repository.git_url, credential) as workspace:
            try:
                # Reads only: the host's heads (a thin bundle's base) and the bundle itself.
                await asyncio.to_thread(workspace.prepare)
                diff, messages = await asyncio.to_thread(workspace.content)
            except BundleInvalid as invalid:
                raise OperationRejected(str(invalid), "change_invalid") from None
            # The new commits' added lines and messages, before anything is sent.
            self.gate(context, "vcs.push_branch", diff=diff, texts=messages)
            try:
                async with GitHub(repository, credential, self.transport) as host:
                    current = await host.branch_head(branch)
                if current == head:
                    # Already there (an earlier attempt or another task): nothing to send.
                    return done
                expected = arguments.get("expected_head")
                if expected is not None and current != expected:
                    raise OperationRejected(
                        "The branch moved since the caller read it", "branch_moved"
                    )
                await self.pusher(workspace, branch)
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
            except PushRejected as rejected:
                raise OperationRejected(str(rejected), "push_rejected") from None
        return done

    async def find_branch(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        repository = self.repository(arguments["repository"])
        branch, head = str(arguments["branch"]), str(arguments["head"])
        async with await self.client(repository, "contents:write") as host:
            current = await host.branch_head(branch)
        if current != head:
            return Reconciliation(applied=False)
        output: dict[str, JsonValue] = {
            "repository": str(arguments["repository"]),
            "branch": branch,
            "head": head,
        }
        return Reconciliation(applied=True, output=output)

    # vcs.open_change

    @staticmethod
    def _change(pull: dict[str, Any]) -> dict[str, JsonValue]:
        return {
            "number": int(pull["number"]),
            "url": str(pull.get("html_url", "")),
            "state": str(pull.get("state", "")),
            "head": str(pull["head"]["ref"]),
        }

    async def open_change(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        repository = self.repository(arguments["repository"])
        head, base = str(arguments["head"]), str(arguments["base"])
        self.check_branch(head, context, arguments["repository"])
        title, body = str(arguments["title"]), str(arguments.get("body") or "")
        self.gate(context, "vcs.open_change", texts={"title": title, "body": body})
        async with await self.client(repository, "pull_requests:write") as host:
            try:
                pull = await host.create_pull(head, base, title, body, bool(arguments.get("draft")))
            except DuplicatePull:
                # The host refuses a second pull request for a head (422): find the first.
                existing = await host.find_pull(head)
                if existing is None:
                    raise OperationRejected(
                        "The host reports a duplicate it does not list", "host_refused"
                    ) from None
                pull = existing
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
        return self._change(pull)

    async def find_change(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        repository = self.repository(arguments["repository"])
        async with await self.client(repository, "pull_requests:write") as host:
            pull = await host.find_pull(str(arguments["head"]))
        if pull is None:
            return Reconciliation(applied=False)
        return Reconciliation(applied=True, output=self._change(pull))

    # vcs.comment

    async def comment(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        repository = self.repository(arguments["repository"])
        body = str(arguments["body"])
        self.gate(context, "vcs.comment", texts={"comment": body})
        async with await self.client(repository, "issues:write") as host:
            try:
                made = await host.create_comment(
                    int(str(arguments["number"])), f"{body}\n\n{marker(context)}"
                )
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
        return {"comment_id": int(made["id"]), "url": str(made.get("html_url", ""))}

    async def find_comment(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        repository = self.repository(arguments["repository"])
        async with await self.client(repository, "issues:write") as host:
            comments = await host.comments(int(str(arguments["number"])))
        found = next((c for c in comments if marker(context) in str(c.get("body", ""))), None)
        if found is None:
            return Reconciliation(applied=False)
        output: dict[str, JsonValue] = {
            "comment_id": int(found["id"]),
            "url": str(found.get("html_url", "")),
        }
        return Reconciliation(applied=True, output=output)

    # vcs.review

    async def review(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        repository = self.repository(arguments["repository"])
        event, body = str(arguments["event"]), str(arguments["body"])
        if event == "APPROVE" and not self.config.allow_approve:
            # An app's approval could satisfy the repository's review rule: an unattended
            # merge path (ADR-037 open question 2). Refused unless the host owner allows it.
            raise OperationRejected(
                "APPROVE reviews are not allowed on this host", "approve_refused"
            )
        self.gate(context, "vcs.review", texts={"review": body})
        async with await self.client(repository, "pull_requests:write") as host:
            try:
                made = await host.create_review(
                    int(str(arguments["number"])), event, f"{body}\n\n{marker(context)}"
                )
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
        return {
            "review_id": int(made["id"]),
            "state": str(made.get("state", "")),
            "url": str(made.get("html_url", "")),
        }

    async def find_review(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        repository = self.repository(arguments["repository"])
        async with await self.client(repository, "pull_requests:write") as host:
            reviews = await host.reviews(int(str(arguments["number"])))
        found = next((r for r in reviews if marker(context) in str(r.get("body", ""))), None)
        if found is None:
            return Reconciliation(applied=False)
        output: dict[str, JsonValue] = {
            "review_id": int(found["id"]),
            "state": str(found.get("state", "")),
            "url": str(found.get("html_url", "")),
        }
        return Reconciliation(applied=True, output=output)

    # vcs.read_change

    async def read_change(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        repository = self.repository(arguments["repository"])
        number = int(str(arguments["number"]))
        async with await self.client(repository, "pull_requests:read") as host:
            try:
                pull = await host.pull(number)
                reviews = await host.reviews(number)
                comments = await host.comments(number)
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
        body, truncated = _bounded(pull.get("body"), MAX_BODY)
        return {
            "number": number,
            "title": str(pull.get("title", "")),
            "state": str(pull.get("state", "")),
            "draft": bool(pull.get("draft")),
            "merged": bool(pull.get("merged")),
            "head": str(pull["head"]["ref"]),
            "head_sha": str(pull["head"]["sha"]),
            "base": str(pull["base"]["ref"]),
            "url": str(pull.get("html_url", "")),
            "body": body,
            "body_truncated": truncated,
            "reviews": [
                {
                    "state": str(r.get("state", "")),
                    "author": str((r.get("user") or {}).get("login", "")),
                    "body": _bounded(r.get("body"), MAX_REVIEW_BODY)[0],
                }
                for r in reviews[-20:]
            ],
            "comments": len(comments),
        }

    # vcs.check_repository

    async def check_repository(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        """Whether the host's rules hold what the team relies on (ADR-037): a human review
        on the default branch, and pushes only under the agent prefix. Reports only."""
        repository = self.repository(arguments["repository"])
        prefix = self.branch_prefix(arguments["repository"])
        root = prefix.split("{group}", 1)[0] or "agent/"
        inside = prefix.format(group=context.group or "readiness") + PROBE
        async with await self.client(repository, "metadata:read") as host:
            try:
                default = str((await host.metadata()).get("default_branch") or "main")
                on_default = await host.branch_rules(default)
                outside = await host.branch_rules(PROBE)
                on_agent = await host.branch_rules(inside)
            except HostRefused as refused:
                raise OperationRejected(str(refused), "host_refused") from None
        checks = readiness(default, root, on_default, outside, on_agent)
        missing: list[JsonValue] = [c["name"] for c in checks if c["required"] and not c["ok"]]
        return {
            "repository": str(arguments["repository"]),
            "default_branch": default,
            "ready": not missing,
            "checks": list(checks),
            "missing": missing,
            "not_checked": [
                "bypass actors: the App or token must not be able to bypass these rules",
                "classic branch protection, which this read does not list",
            ],
        }

    def handlers(self) -> dict[str, Any]:
        """Entry-point name to handler, bound to this instance."""
        return {
            "vcs.push_branch": self.push_branch,
            "vcs.find_branch": self.find_branch,
            "vcs.open_change": self.open_change,
            "vcs.find_change": self.find_change,
            "vcs.comment": self.comment,
            "vcs.find_comment": self.find_comment,
            "vcs.review": self.review,
            "vcs.find_review": self.find_review,
            "vcs.read_change": self.read_change,
            "vcs.check_repository": self.check_repository,
        }


def _rule(rules: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((r for r in rules if r.get("type") == kind), None)


def readiness(
    default: str,
    root: str,
    on_default: list[dict[str, Any]],
    outside: list[dict[str, Any]],
    on_agent: list[dict[str, Any]],
) -> list[dict[str, JsonValue]]:
    """The checks `vcs.check_repository` reports, from the rules that apply to the default
    branch, to a branch outside the agent prefix and to one inside it."""
    review = _rule(on_default, "pull_request")
    parameters = (review or {}).get("parameters") or {}
    approvals = parameters.get("required_approving_review_count", 0)
    count = approvals if isinstance(approvals, int) else 0
    blocked = [kind for kind in BLOCKS_PUSH if _rule(outside, kind) is None]
    open_inside = [kind for kind in BLOCKS_PUSH if _rule(on_agent, kind) is not None]

    def check(name: str, required: bool, ok: bool, detail: str) -> dict[str, JsonValue]:
        return {"name": name, "required": required, "ok": ok, "detail": detail}

    return [
        check(
            "default_branch_requires_review",
            True,
            review is not None and count >= 1,
            f"{default}: a pull request with at least one approving review"
            + ("" if review is not None else " (no pull_request rule applies)")
            + (f" (requires {count})" if review is not None else ""),
        ),
        check(
            "pushes_outside_agent_prefix_blocked",
            True,
            not blocked,
            f"branches outside {root}* may not be created or updated"
            + (f" (missing: {', '.join(blocked)})" if blocked else ""),
        ),
        check(
            "agent_prefix_pushable",
            True,
            not open_inside,
            f"branches under {root}* may be created and updated by the agent team"
            + (f" (blocked by: {', '.join(open_inside)})" if open_inside else ""),
        ),
        check(
            "default_branch_last_push_approval",
            False,
            parameters.get("require_last_push_approval") is True,
            "the most recent push must be approved by someone other than its pusher",
        ),
        check(
            "agent_prefix_no_force_push",
            False,
            _rule(on_agent, "non_fast_forward") is not None,
            f"branches under {root}* may not be force-pushed",
        ),
    ]
