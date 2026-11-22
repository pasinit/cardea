"""An in-process fake of the GitHub REST shapes the vcs operations use, for tests.

REST requests go through `httpx.MockTransport`, so nothing touches a network. Each fake
repository is a real bare git repository on disk: `vcs.push_branch` pushes to it with git,
and the REST ref endpoint reads it, so the two always agree.

`serve()` puts the same fake behind a loopback HTTP server, for workers in another
process (the `vcs` confined worker in tests). Rulesets are a list of
`{"include": [...], "exclude": [...], "rules": [...]}` with `fnmatch` branch patterns, plus
`~DEFAULT_BRANCH` and `~ALL`, which `rules/branches/<branch>` evaluates.

Identities: `logins` maps a bearer token to the account it acts as (default
`cardea-test-app[bot]`). Pull requests, comments and reviews record their author, a
review records the head it applies to (`commit_id`), and, as on GitHub, an author cannot
approve its own pull request. `checks` holds each commit's check runs. `PUT
.../pulls/<n>/merge` squash-merges only when the head still matches, the pull request is
mergeable, and the base's `pull_request` rule holds: enough approvals of the current head
by accounts other than the author.

`pulls/<n>/files` lists a pull request's changed files as GitHub does, computed with git
from the base and head branches (`patch` holds only the hunks and is omitted for binary
files); `files` overrides it per pull request, for shapes git cannot produce cheaply, such
as a file too large for the host to show.

`fail_after` makes the fake apply a request and then drop the connection, the case that
leaves an effect in doubt: for example `{"create_pull"}` creates the pull request and then
raises `httpx.ConnectError`, as a crash after the host acted would look to the kernel.
"""

import fnmatch
import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    default_branch: str = "main"
    rulesets: list[dict[str, Any]] = field(default_factory=list)
    # Commit SHA to its check runs: `{"name", "status", "conclusion"}`.
    checks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Pull request number to its changed files, overriding the git-computed list.
    files: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # A plan without rulesets (a private repository on a free plan): the rules API is 403.
    rules_unavailable: bool = False

    def pass_checks(self, sha: str, *names: str) -> None:
        self.checks[sha] = [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in names or ("test",)
        ]

    def pull(self, number: int) -> dict[str, Any] | None:
        """A pull request as the host shows it now: an open one follows its branch head."""
        pull = next((p for p in self.pulls if p["number"] == number), None)
        if pull is not None and not pull["merged"]:
            pull["head"]["sha"] = self.head(pull["head"]["ref"])
        return pull

    def rules_for(self, branch: str) -> list[dict[str, Any]]:
        def matches(pattern: str) -> bool:
            if pattern == "~ALL":
                return True
            if pattern == "~DEFAULT_BRANCH":
                return branch == self.default_branch
            return fnmatch.fnmatchcase(branch, pattern.removeprefix("refs/heads/"))

        found: list[dict[str, Any]] = []
        for number, ruleset in enumerate(self.rulesets, start=1):
            if any(map(matches, ruleset.get("include", ()))) and not any(
                map(matches, ruleset.get("exclude", ()))
            ):
                found += [{**rule, "ruleset_id": number} for rule in ruleset["rules"]]
        return found

    def changed_files(self, number: int) -> list[dict[str, Any]]:
        if number in self.files:
            return self.files[number]
        pull = self.pull(number)
        assert pull is not None
        span = f"refs/heads/{pull['base']['ref']}...{pull['head']['sha']}"

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(self.path), *args], capture_output=True, text=True, check=True
            ).stdout

        statuses = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}
        found = []
        for line in git("diff", "--no-renames", "--numstat", span).splitlines():
            added, deleted, path = line.split("\t", 2)
            status = git("diff", "--no-renames", "--name-status", span, "--", path).split("\t")[0]
            entry: dict[str, Any] = {
                "filename": path,
                "status": statuses.get(status[:1], "modified"),
                "additions": 0 if added == "-" else int(added),
                "deletions": 0 if deleted == "-" else int(deleted),
            }
            entry["changes"] = entry["additions"] + entry["deletions"]
            if added != "-":
                text = git("diff", "--no-renames", span, "--", path)
                entry["patch"] = text[text.index("@@") :].rstrip("\n") if "@@" in text else ""
            found.append(entry)
        return found

    def compare(self, base: str, head: str) -> dict[str, Any] | None:
        """The host's compare of `base...head`, counted with git, or None if either is
        unknown."""
        base_sha = self.head(base) or self.commit(base)
        head_sha = self.head(head) or self.commit(head)
        if base_sha is None or head_sha is None:
            return None
        counted = subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "rev-list",
                "--left-right",
                "--count",
                f"{base_sha}...{head_sha}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        behind, ahead = int(counted[0]), int(counted[1])
        status = (
            "identical"
            if not (ahead or behind)
            else ("diverged" if ahead and behind else ("ahead" if ahead else "behind"))
        )
        return {
            "status": status,
            "ahead_by": ahead,
            "behind_by": behind,
            "base_commit": {"sha": base_sha},
        }

    def commit(self, sha: str) -> str | None:
        found = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--verify", "-q", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return found.stdout.strip() or None

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
    # Bearer token to the account it acts as.
    logins: dict[str, str] = field(default_factory=dict)

    def login(self, request: httpx.Request) -> str:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return self.logins.get(token, "cardea-test-app[bot]")

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
        if parts[0] != "repos" or len(parts) < 3:
            return httpx.Response(404, json={"message": "Not Found"})
        repo = self.repositories.get(f"{parts[1]}/{parts[2]}")
        if repo is None:
            return httpx.Response(404, json={"message": "Not Found"})
        rest, method = parts[3:], request.method
        if not rest and method == "GET":
            full = f"{repo.owner}/{repo.name}"
            return httpx.Response(
                200, json={"full_name": full, "default_branch": repo.default_branch}
            )
        if rest[:2] == ["rules", "branches"] and method == "GET":
            if repo.rules_unavailable:
                return httpx.Response(
                    403,
                    json={
                        "message": "Upgrade to GitHub Pro or make this repository public to "
                        "enable this feature."
                    },
                )
            page = int(request.url.params.get("page", "1"))
            rules = repo.rules_for("/".join(rest[2:])) if page == 1 else []
            return httpx.Response(200, json=rules)
        body = json.loads(request.content) if request.content else {}
        if rest[:3] == ["git", "refs", "heads"] and method == "DELETE":
            branch = "/".join(rest[3:])
            if repo.head(branch) is None:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            subprocess.run(
                ["git", "-C", str(repo.path), "update-ref", "-d", f"refs/heads/{branch}"],
                check=True,
            )
            return self._done("delete_branch", httpx.Response(204))
        if rest[:3] == ["git", "ref", "heads"] and method == "GET":
            sha = repo.head("/".join(rest[3:]))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": "/".join(rest[1:]), "object": {"sha": sha}})
        if rest == ["pulls"] and method == "POST":
            return self._create_pull(repo, body, self.login(request))
        if rest == ["pulls"] and method == "GET":
            head = request.url.params.get("head", "")
            branch = head.split(":", 1)[1] if ":" in head else head
            found = [repo.pull(p["number"]) for p in repo.pulls if p["head"]["ref"] == branch]
            return httpx.Response(200, json=found)
        if len(rest) == 2 and rest[0] == "pulls" and method == "GET":
            pull = repo.pull(int(rest[1]))
            if pull is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=pull)
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "files" and method == "GET":
            number = int(rest[1])
            if repo.pull(number) is None:
                return httpx.Response(404, json={"message": "Not Found"})
            page = int(request.url.params.get("page", "1"))
            per = int(request.url.params.get("per_page", "30"))
            items = repo.changed_files(number)
            return httpx.Response(200, json=items[(page - 1) * per : page * per])
        if len(rest) >= 2 and rest[0] == "compare" and method == "GET":
            base, _, head = "/".join(rest[1:]).partition("...")
            compared = repo.compare(base, head) if head else None
            if compared is None:
                return httpx.Response(404, json={"message": "Not Found"})
            self.calls.append("compare")
            return httpx.Response(200, json=compared)
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "check-runs" and method == "GET":
            page = int(request.url.params.get("page", "1"))
            runs = repo.checks.get(rest[1], []) if page == 1 else []
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "merge" and method == "PUT":
            return self._merge(repo, int(rest[1]), body, self.login(request))
        if len(rest) == 3 and rest[0] in {"issues", "pulls"} and rest[2] in {"comments", "reviews"}:
            number = int(rest[1])
            if not any(p["number"] == number for p in repo.pulls):
                return httpx.Response(404, json={"message": "Not Found"})
            store = repo.comments if rest[2] == "comments" else repo.reviews
            pull = repo.pull(number)
            assert pull is not None
            if method == "GET":
                page = int(request.url.params.get("page", "1"))
                per = int(request.url.params.get("per_page", "30"))
                items = store.get(number, [])
                return httpx.Response(200, json=items[(page - 1) * per : page * per])
            item: dict[str, Any] = {
                "id": self._id(),
                "body": body["body"],
                "html_url": f"https://github.test/{repo.owner}/{repo.name}/pull/{number}#c",
                "user": {"login": self.login(request)},
            }
            if rest[2] == "reviews":
                if body["event"] == "APPROVE" and item["user"] == pull["user"]:
                    return httpx.Response(
                        422,
                        json={
                            "message": "Unprocessable Entity",
                            "errors": ["Review Can not approve your own pull request"],
                        },
                    )
                item["state"] = {"REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}.get(
                    body["event"], "COMMENTED"
                )
                item["commit_id"] = body.get("commit_id") or pull["head"]["sha"]
            store.setdefault(number, []).append(item)
            kind = "create_comment" if rest[2] == "comments" else "create_review"
            return self._done(kind, httpx.Response(200, json=item))
        return httpx.Response(404, json={"message": "Not Found"})

    def _merge(
        self, repo: FakeRepository, number: int, body: dict[str, Any], login: str
    ) -> httpx.Response:
        pull = repo.pull(number)
        if pull is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if pull["merged"] or pull["state"] != "open" or pull.get("mergeable") is not True:
            return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
        head = pull["head"]["sha"]
        if body.get("sha") != head:
            return httpx.Response(
                409, json={"message": "Head branch was modified. Review and try the merge again."}
            )
        for rule in repo.rules_for(pull["base"]["ref"]):
            if rule.get("type") != "pull_request":
                continue
            needed = int((rule.get("parameters") or {}).get("required_approving_review_count", 0))
            approvers = {
                r["user"]["login"]
                for r in repo.reviews.get(number, [])
                if r.get("state") == "APPROVED"
                and r.get("commit_id") == head
                and r["user"] != pull["user"]
            }
            if len(approvers) < needed:
                return httpx.Response(
                    405, json={"message": "Repository rule violations found: approval required"}
                )
        subprocess.run(
            ["git", "-C", str(repo.path), "update-ref", f"refs/heads/{pull['base']['ref']}", head],
            check=True,
        )
        pull.update(
            merged=True,
            state="closed",
            merge_commit_sha=head,
            merged_by={"login": login},
            # The squash commit's subject and body as the merge request named them.
            commit_title=body.get("commit_title"),
            commit_message=body.get("commit_message"),
        )
        answer = {"sha": head, "merged": True, "message": "Pull Request successfully merged"}
        return self._done("merge", httpx.Response(200, json=answer))

    def _create_pull(
        self, repo: FakeRepository, body: dict[str, Any], login: str
    ) -> httpx.Response:
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
            "mergeable": True,
            "merge_commit_sha": None,
            "user": {"login": login},
            "html_url": f"https://github.test/{repo.owner}/{repo.name}/pull/{number}",
            "head": {"ref": head, "sha": repo.head(head)},
            "base": {"ref": base},
        }
        repo.pulls.append(pull)
        return self._done("create_pull", httpx.Response(201, json=pull))


@contextmanager
def serve(fake: FakeGitHub) -> Iterator[str]:
    """The fake on a loopback HTTP server; yields its base URL. A `fail_after` request is
    applied and then the connection is closed without an answer."""

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            request = httpx.Request(
                self.command,
                f"http://fake{self.path}",
                headers=dict(self.headers.items()),
                content=body,
            )
            try:
                response = fake.handle(request)
            except httpx.ConnectError:
                self.close_connection = True
                return
            content = response.content
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = do_POST = do_PATCH = do_PUT = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
