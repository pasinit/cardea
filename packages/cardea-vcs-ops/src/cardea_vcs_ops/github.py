"""The GitHub REST calls the vcs operations need, over the kernel's pinned `httpx`.

Four resources are enough (ADR-037, "Client library"): git refs, pull requests (with
their changed files, for reviewers), issue comments and pull-request reviews. Each request
carries the call's credential in the `Authorization` header and nowhere else.

Errors: a 4xx answer means the host refused the request and nothing changed, so it is a
`HostRefused` the operations turn into a definite failure. A duplicate pull request (422)
is reported as `DuplicatePull` so the caller can find the existing one. Anything else (5xx,
a dropped connection) leaves the effect in doubt and propagates, so the kernel records
`outcome_unknown` and the reconciler reads the host.
"""

from typing import Any
from urllib.parse import quote

import httpx

from cardea_vcs_ops.config import Repository
from cardea_vcs_ops.credentials import Credential

API_VERSION = "2022-11-28"
PAGE = 100
MAX_PAGES = 10


class HostRefused(Exception):
    """The host answered 4xx: nothing changed. The message carries no request detail."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"repository host refused the request ({status}): {reason}")
        self.status, self.reason = status, reason


class DuplicatePull(HostRefused):
    pass


def _reason(response: httpx.Response) -> str:
    """The host's short message, bounded and single-line; never request headers."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase[:120]
    message = str(body.get("message", "")) if isinstance(body, dict) else ""
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        message += ": " + str(errors[0].get("message", errors[0].get("code", "")))
    return " ".join(message.split())[:200]


class GitHub:
    def __init__(
        self,
        repository: Repository,
        credential: Credential,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.repository = repository
        self._client = httpx.AsyncClient(
            base_url=repository.api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "cardea-vcs-ops",
            },
            transport=transport,
            timeout=30.0,
        )

    async def __aenter__(self) -> "GitHub":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    @property
    def _base(self) -> str:
        return f"/repos/{quote(self.repository.owner)}/{quote(self.repository.name)}"

    async def _request(
        self, method: str, path: str, *, missing_ok: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """`missing_ok` lets a read return a 404 as "not there"; on any other call a 404
        is a refusal (nothing changed), never a malformed success."""
        response = await self._client.request(method, self._base + path, **kwargs)
        if response.status_code == 404 and missing_ok:
            return response
        if 400 <= response.status_code < 500:
            reason = _reason(response)
            if response.status_code == 422 and "already exists" in reason.lower():
                raise DuplicatePull(422, reason)
            raise HostRefused(response.status_code, reason)
        if response.status_code >= 500:
            response.raise_for_status()
        return response

    async def _list(self, path: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self._request("GET", path, params={"per_page": PAGE, "page": page})
            items = response.json()
            found.extend(items)
            if len(items) < PAGE:
                break
        return found

    # Git refs

    async def branch_head(self, branch: str) -> str | None:
        response = await self._request(
            "GET", f"/git/ref/heads/{quote(branch, safe='/')}", missing_ok=True
        )
        if response.status_code == 404:
            return None
        return str(response.json()["object"]["sha"])

    # Repository and rules (vcs.check_repository)

    async def metadata(self) -> dict[str, Any]:
        response = await self._client.request("GET", self._base)
        if 400 <= response.status_code < 500:
            raise HostRefused(response.status_code, _reason(response))
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        """The ruleset rules that apply to a branch name, whether or not it exists."""
        return await self._list(f"/rules/branches/{quote(branch, safe='/')}")

    # Pull requests

    async def create_pull(
        self, head: str, base: str, title: str, body: str, draft: bool
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )
        result: dict[str, Any] = response.json()
        return result

    async def find_pull(self, head: str) -> dict[str, Any] | None:
        """The pull request for a head branch, open or closed (the reconciliation read)."""
        response = await self._request(
            "GET",
            "/pulls",
            params={"head": f"{self.repository.owner}:{head}", "state": "all", "per_page": PAGE},
        )
        pulls = response.json()
        return pulls[0] if pulls else None

    async def pull(self, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"/pulls/{number}")
        result: dict[str, Any] = response.json()
        return result

    async def pull_files(self, number: int) -> list[dict[str, Any]]:
        """The changed files with their patches. The host omits `patch` for binary files
        and for very large diffs."""
        return await self._list(f"/pulls/{number}/files")

    # Comments and reviews

    async def create_comment(self, number: int, body: str) -> dict[str, Any]:
        response = await self._request("POST", f"/issues/{number}/comments", json={"body": body})
        result: dict[str, Any] = response.json()
        return result

    async def comments(self, number: int) -> list[dict[str, Any]]:
        return await self._list(f"/issues/{number}/comments")

    async def create_review(self, number: int, event: str, body: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/pulls/{number}/reviews", json={"event": event, "body": body}
        )
        result: dict[str, Any] = response.json()
        return result

    async def reviews(self, number: int) -> list[dict[str, Any]]:
        return await self._list(f"/pulls/{number}/reviews")

    # Checks and merging (vcs.merge)

    async def check_runs(self, sha: str) -> list[dict[str, Any]]:
        """Every check run on a commit, across pages."""
        found: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self._request(
                "GET",
                f"/commits/{quote(sha)}/check-runs",
                params={"per_page": PAGE, "page": page},
            )
            runs = response.json().get("check_runs", [])
            found.extend(runs)
            if len(runs) < PAGE:
                break
        return found

    async def merge(self, number: int, sha: str, title: str, message: str) -> dict[str, Any]:
        """Squash-merge, only if the head is still `sha` (the host answers 409 if not), with
        `title` and `message` as the squash commit's subject and body."""
        response = await self._request(
            "PUT",
            f"/pulls/{number}/merge",
            json={
                "merge_method": "squash",
                "sha": sha,
                "commit_title": title,
                "commit_message": message,
            },
        )
        result: dict[str, Any] = response.json()
        return result

    async def delete_branch(self, branch: str) -> bool:
        """Delete a branch; False if the host has no such branch (already deleted)."""
        response = await self._request(
            "DELETE", f"/git/refs/heads/{quote(branch, safe='/')}", missing_ok=True
        )
        return response.status_code != 404
