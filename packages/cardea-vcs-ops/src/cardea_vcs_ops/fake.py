"""An in-process fake of the GitHub REST shapes the vcs operations use, for tests.

REST requests go through `httpx.MockTransport`, so nothing touches a network. Each fake
repository is a real bare git repository on disk: `vcs.push_branch` pushes to it with git,
and the REST ref endpoint reads it, so the two always agree.

`fail_after` makes the fake apply a request and then drop the connection, the case that
leaves an effect in doubt: for example `{"create_pull"}` creates the pull request and then
raises `httpx.ConnectError`, as a crash after the host acted would look to the kernel.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cardea.service.repositories import DEFAULT_PREFIX, Registry
from cardea.service.repositories import Repository as RegistryEntry
from cardea_vcs_ops.config import Repository

API = "https://api.github.test"


@dataclass
class FakeRepository:
    owner: str
    name: str
    path: Path
    pulls: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    def head(self, branch: str) -> str | None:
        found = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return found.stdout.strip() or None


@dataclass
class FakeGitHub:
    root: Path
    repositories: dict[str, FakeRepository] = field(default_factory=dict)
    fail_after: set[str] = field(default_factory=set)
    # Every Authorization header seen, to show the token reached the host and only there.
    authorizations: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    next_id: int = 1000

    def add(self, owner: str, name: str) -> Repository:
        path = self.root / f"{owner}-{name}.git"
        subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
        self.repositories[f"{owner}/{name}"] = FakeRepository(owner, name, path)
        return Repository(owner=owner, name=name, api_url=API, git_url=str(path))

    def registry(self, **branch_prefixes: str) -> Registry:
        """The host's repository registry for these fake repositories, aliased by name: each
        bare repository is both the read-only `source` and where `vcs.*` pushes."""
        entries = {}
        for repo in self.repositories.values():
            entries[repo.name] = RegistryEntry(
                source=str(repo.path),
                owner=repo.owner,
                name=repo.name,
                api_url=API,
                git_url=str(repo.path),
                branch_prefix=branch_prefixes.get(repo.name, DEFAULT_PREFIX),
            )
        return Registry(repositories=entries)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _done(self, name: str, response: httpx.Response) -> httpx.Response:
        self.calls.append(name)
        if name in self.fail_after:
            self.fail_after.discard(name)
            raise httpx.ConnectError("connection dropped after the host applied the request")
        return response

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.authorizations.append(request.headers.get("Authorization", ""))
        parts = request.url.path.strip("/").split("/")
        if parts[0] != "repos" or len(parts) < 4:
            return httpx.Response(404, json={"message": "Not Found"})
        repo = self.repositories.get(f"{parts[1]}/{parts[2]}")
        if repo is None:
            return httpx.Response(404, json={"message": "Not Found"})
        rest, method = parts[3:], request.method
        body = json.loads(request.content) if request.content else {}
        if rest[:3] == ["git", "ref", "heads"] and method == "GET":
            sha = repo.head("/".join(rest[3:]))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": "/".join(rest[1:]), "object": {"sha": sha}})
        if rest == ["pulls"] and method == "POST":
            return self._create_pull(repo, body)
        if rest == ["pulls"] and method == "GET":
            head = request.url.params.get("head", "")
            branch = head.split(":", 1)[1] if ":" in head else head
            return httpx.Response(200, json=[p for p in repo.pulls if p["head"]["ref"] == branch])
        if len(rest) == 2 and rest[0] == "pulls" and method == "GET":
            pull = next((p for p in repo.pulls if p["number"] == int(rest[1])), None)
            if pull is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=pull)
        if len(rest) == 3 and rest[0] in {"issues", "pulls"} and rest[2] in {"comments", "reviews"}:
            number = int(rest[1])
            if not any(p["number"] == number for p in repo.pulls):
                return httpx.Response(404, json={"message": "Not Found"})
            store = repo.comments if rest[2] == "comments" else repo.reviews
            if method == "GET":
                page = int(request.url.params.get("page", "1"))
                per = int(request.url.params.get("per_page", "30"))
                items = store.get(number, [])
                return httpx.Response(200, json=items[(page - 1) * per : page * per])
            item: dict[str, Any] = {
                "id": self._id(),
                "body": body["body"],
                "html_url": f"https://github.test/{repo.owner}/{repo.name}/pull/{number}#c",
                "user": {"login": "cardea-test-app[bot]"},
            }
            if rest[2] == "reviews":
                item["state"] = {"REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}.get(
                    body["event"], "COMMENTED"
                )
            store.setdefault(number, []).append(item)
            kind = "create_comment" if rest[2] == "comments" else "create_review"
            return self._done(kind, httpx.Response(200, json=item))
        return httpx.Response(404, json={"message": "Not Found"})

    def _create_pull(self, repo: FakeRepository, body: dict[str, Any]) -> httpx.Response:
        head, base = body["head"], body["base"]
        if repo.head(head) is None:
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}],
                },
            )
        if any(p["head"]["ref"] == head and p["state"] == "open" for p in repo.pulls):
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [
                        {
                            "resource": "PullRequest",
                            "code": "custom",
                            "message": f"A pull request already exists for {repo.owner}:{head}.",
                        }
                    ],
                },
            )
        number = len(repo.pulls) + 1
        pull = {
            "number": number,
            "title": body["title"],
            "body": body.get("body", ""),
            "state": "open",
            "draft": bool(body.get("draft")),
            "merged": False,
            "html_url": f"https://github.test/{repo.owner}/{repo.name}/pull/{number}",
            "head": {"ref": head, "sha": repo.head(head)},
            "base": {"ref": base},
        }
        repo.pulls.append(pull)
        return self._done("create_pull", httpx.Response(201, json=pull))
