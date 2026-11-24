"""Whether a team host works: the kernel's own readiness plus what only the host can see.

Run as root (standard library only):

    python3 -I cardea-ready.py --host /etc/cardea/team.host.json [--json] [--allow-paused]

It checks:

* **the kernel**: `GET /v0.2/readyz` (database, DBOS, host helper, repository mirrors,
  stuck runs, paused groups), asked as the team's supervisor through `teamkit.py ready`
  run as the kernel user, the way `cardea-update.py` reaches the kernel. A paused team's
  supervisor is refused by the kernel; that is reported as `paused`, accepted with
  `--allow-paused`;
* **the egress proxy**: `systemctl is-active squid`;
* **each repository-host token** named in the vcs configuration's `credentials.tokens`:
  not past its `expires_on`, valid, with access to each repository of the registry that
  `vcs.*` acts on, and with the permissions its pools need. The token is read from its
  root-only file in this process and sent only in the `Authorization` header to the
  repository host; it is never printed, logged or passed to another process.

Which token needs what follows the vcs configuration: a pool in `allow_approve` approves
with its token (`pools`, else `default`), which needs `pull_requests: write` (with
`allow_approve: true`, every token does); every other pool pushes with its token, so
`default` and the tokens of pools outside `allow_approve` need `contents: write`.

How permissions are checked: `GET /repos/{owner}/{repo}` must answer 200 (the token is
valid and reaches the repository) and, for a pushing token, report `permissions.push` for
its account. A classic token (`X-OAuth-Scopes` present) must hold the `repo` scope (or
`public_repo` for a public repository). A fine-grained token's own permissions are not
listed by the API, so each needed one is probed with a request that cannot change
anything: an empty-body `POST /repos/{owner}/{repo}/git/refs` (contents) or `POST
/repos/{owner}/{repo}/pulls` (pull requests). The host checks permission before it
validates the body: 422 means the token holds the permission, 403 that it does not.

Prints a report and exits 0 when every check passes, 1 otherwise; `--json` prints one JSON
document instead.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

CHECKOUT = Path("/opt/cardea")
VENV_PYTHON = CHECKOUT / "apps/api/.venv/bin/python"
TEAMKIT = CHECKOUT / "deploy/team-kit/teamkit.py"
KERNEL_USER = "cardea-kernel"
VCS_CONFIG = Path("/opt/cardea-vcs/vcs.json")
REPOSITORIES = Path("/opt/cardea-code/repositories.json")
WORKERS = Path("/etc/cardea-host-workers.json")
CREDENTIALS = Path("/etc/cardea/credentials")
HTTP_SECONDS = 15
KERNEL_SECONDS = 60
MAX_TOKEN_BYTES = 4096

Runner = Callable[..., Any]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


# The kernel


def call_kernel(host: Path, allow_paused: bool, runner: Runner = subprocess.run) -> dict[str, Any]:
    """`teamkit.py ready` as the kernel user: one JSON line describing the outcome."""
    argv = [str(VENV_PYTHON), "-I", str(TEAMKIT), "ready", "--host", str(host)]
    if allow_paused:
        argv.append("--allow-paused")
    try:
        done = runner(
            ["runuser", "-u", KERNEL_USER, "--", *argv],
            capture_output=True,
            text=True,
            timeout=KERNEL_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"reached": False, "error": f"no answer within {KERNEL_SECONDS} s"}
    except OSError as error:
        return {"reached": False, "error": type(error).__name__}
    lines = done.stdout.strip().splitlines()
    try:
        outcome = json.loads(lines[-1]) if lines else None
    except ValueError:
        outcome = None
    if not isinstance(outcome, dict):
        return {"reached": False, "error": f"teamkit ready failed (exit {done.returncode})"}
    return outcome


def kernel_checks(outcome: dict[str, Any], allow_paused: bool) -> list[Check]:
    """What `teamkit.py ready` reported, as checks named `kernel` and `kernel.<check>`."""
    if not outcome.get("reached"):
        return [Check("kernel", False, f"kernel did not answer: {outcome.get('error')}")]
    if "error" in outcome:
        if outcome["error"] == "principal_disabled":
            detail = "team paused: the supervisor is disabled"
            return [Check("kernel", allow_paused, detail + (" (allowed)" if allow_paused else ""))]
        return [
            Check(
                "kernel",
                False,
                f"kernel refused readyz: HTTP {outcome.get('status')} {outcome['error']}",
            )
        ]
    body = outcome.get("body") or {}
    checks = [Check("kernel", True, f"answered (as of {body.get('as_of')})")]
    for item in body.get("checks") or []:
        if isinstance(item, dict):
            checks.append(
                Check(f"kernel.{item.get('name')}", item.get("ok") is True, str(item.get("detail")))
            )
    if body.get("ready") is not True and all(c.ok for c in checks):
        checks.append(Check("kernel.ready", False, "the kernel reports not ready"))
    return checks


# The egress proxy


def squid_check(runner: Runner = subprocess.run) -> Check:
    done = runner(["systemctl", "is-active", "squid"], capture_output=True, text=True, check=False)
    state = done.stdout.strip() or "unknown"
    return Check("squid", state == "active", f"squid is {state}")


# Repository-host tokens


def token_needs(vcs: dict[str, Any]) -> dict[str, set[str]]:
    """Each token name of the vcs configuration and the permissions its pools need."""
    credentials = vcs.get("credentials") or {}
    tokens = credentials.get("tokens") or {}
    default = credentials.get("default")
    pools: dict[str, str] = credentials.get("pools") or {}
    approve = vcs.get("allow_approve", False)
    needs: dict[str, set[str]] = {name: set() for name in tokens}

    def need(name: str | None, permission: str) -> None:
        if name in needs:
            needs[name].add(permission)

    if approve is True:
        for name in {default, *pools.values()}:
            need(name, "pull_requests")
    elif isinstance(approve, list):
        for pool in approve:
            need(pools.get(pool, default), "pull_requests")
    approving = set(approve) if isinstance(approve, list) else set(pools) if approve else set()
    # Every pool outside `allow_approve` pushes with its token. A pool without a token of
    # its own uses `default`, so `default` always needs to push.
    need(default, "contents")
    for pool, name in pools.items():
        if pool not in approving:
            need(name, "contents")
    return needs


def credential_path(name: str, workers: dict[str, Any] | None) -> Path:
    """The token's file: as the worker profiles load it, else `/etc/cardea/credentials`."""
    for profile in ((workers or {}).get("profiles") or {}).values():
        named = (profile or {}).get("credentials") or {}
        if isinstance(named, dict) and isinstance(named.get(name), str):
            return Path(named[name])
    return CREDENTIALS / name


def read_token(path: Path) -> str:
    with path.open("rb") as stream:
        token = stream.read(MAX_TOKEN_BYTES + 1)
    if len(token) > MAX_TOKEN_BYTES:
        raise ValueError("token file is too large")
    return token.decode().strip()


def hosted(registry: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """`(alias, api_url, owner, name)` of each repository `vcs.*` acts on."""
    found = []
    for alias, entry in sorted((registry.get("repositories") or {}).items()):
        if all(isinstance(entry.get(k), str) for k in ("api_url", "owner", "name")):
            found.append((alias, entry["api_url"].rstrip("/"), entry["owner"], entry["name"]))
    return found


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: Any


def https(method: str, url: str, token: str, body: bytes | None = None) -> Answer:
    """One request to the repository host with the token as its bearer credential."""
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cardea-ready",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_SECONDS) as response:
            status, headers, raw = response.status, dict(response.headers), response.read(262144)
    except urllib.error.HTTPError as error:
        status, headers, raw = error.code, dict(error.headers or {}), error.read(262144)
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return Answer(status, {k.lower(): v for k, v in headers.items()}, parsed)


Http = Callable[[str, str, str, bytes | None], Answer]

PROBES = {
    "contents": ("git/refs", "contents: write"),
    "pull_requests": ("pulls", "pull_requests: write"),
}


def repository_check(
    name: str, token: str, needs: set[str], repository: tuple[str, str, str, str], http: Http
) -> Check:
    alias, api, owner, repo = repository
    label = f"token:{name}:{alias}"
    base = f"{api}/repos/{owner}/{repo}"
    answer = http("GET", base, token, None)
    if answer.status == 401:
        return Check(label, False, "the repository host rejects the token (invalid or revoked)")
    if answer.status in (403, 404):
        return Check(label, False, f"the token cannot read {owner}/{repo} (HTTP {answer.status})")
    if answer.status != 200 or not isinstance(answer.body, dict):
        return Check(label, False, f"unexpected answer reading {owner}/{repo}: {answer.status}")
    role = answer.body.get("permissions") or {}
    missing = []
    if "contents" in needs and role.get("push") is not True:
        missing.append("push access for its account")
    scopes = answer.headers.get("x-oauth-scopes")
    if scopes is not None:
        held = {s.strip() for s in scopes.split(",") if s.strip()}
        wanted = "public_repo" if answer.body.get("private") is False else "repo"
        if needs and "repo" not in held and wanted not in held:
            missing.append(f"the {wanted} scope")
    else:
        for need in sorted(needs):
            path, permission = PROBES[need]
            probe = http("POST", f"{base}/{path}", token, b"{}")
            if probe.status == 403:
                missing.append(permission)
            elif probe.status != 422:
                missing.append(f"{permission} (probe answered HTTP {probe.status})")
    if missing:
        return Check(label, False, "lacks " + ", ".join(missing))
    granted = ", ".join(PROBES[n][1] for n in sorted(needs)) or "read"
    return Check(label, True, f"valid for {owner}/{repo} with {granted}")


def token_checks(
    vcs: dict[str, Any],
    registry: dict[str, Any],
    workers: dict[str, Any] | None,
    http: Http = https,
    today: date | None = None,
) -> list[Check]:
    checks = []
    tokens = (vcs.get("credentials") or {}).get("tokens") or {}
    repositories = hosted(registry)
    day = today or datetime.now(UTC).date()
    for name, needs in sorted(token_needs(vcs).items()):
        label = f"token:{name}"
        expires = (tokens.get(name) or {}).get("expires_on")
        try:
            if expires and date.fromisoformat(expires) <= day:
                checks.append(Check(label, False, f"expired on {expires} (vcs configuration)"))
                continue
        except ValueError:
            checks.append(Check(label, False, "expires_on is not a date"))
            continue
        path = credential_path(name, workers)
        try:
            token = read_token(path)
        except (OSError, ValueError, UnicodeDecodeError) as error:
            checks.append(Check(label, False, f"cannot read {path} ({type(error).__name__})"))
            continue
        if not token:
            checks.append(Check(label, False, f"{path} is empty"))
            continue
        if not repositories:
            checks.append(Check(label, True, "no repository for vcs.* in the registry"))
            continue
        for repository in repositories:
            try:
                check = repository_check(name, token, needs, repository, http)
            except (OSError, ValueError) as error:
                reason = f"{type(error).__name__}: {error}"
                detail = redact(f"repository host unreachable ({reason})", token)
                check = Check(f"{label}:{repository[0]}", False, detail[:300])
            checks.append(Check(check.name, check.ok, redact(check.detail, token)))
    return checks


# The report


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def readiness(
    host: Path,
    *,
    allow_paused: bool = False,
    vcs_path: Path = VCS_CONFIG,
    repositories_path: Path = REPOSITORIES,
    workers_path: Path = WORKERS,
    runner: Runner = subprocess.run,
    http: Http = https,
) -> dict[str, Any]:
    checks = kernel_checks(call_kernel(host, allow_paused, runner), allow_paused)
    checks.append(squid_check(runner))
    try:
        vcs = load_json(vcs_path)
        if vcs is not None:
            registry = load_json(repositories_path) or {}
            checks += token_checks(vcs, registry, load_json(workers_path), http)
    except (OSError, ValueError) as error:
        checks.append(Check("tokens", False, f"cannot read the vcs setup ({type(error).__name__})"))
    return {
        "ready": all(check.ok for check in checks),
        "checks": [check.document() for check in checks],
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def regressions(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """Checks that passed in `before` and fail in `after` (a check absent from either is
    not compared): what a deploy broke, as opposed to what was already failing."""
    if before is None:
        return []
    passed = {c["name"] for c in before.get("checks", []) if c.get("ok")}
    return [c["name"] for c in after.get("checks", []) if c["name"] in passed and not c["ok"]]


def report(result: dict[str, Any]) -> str:
    lines = [f"ready: {'yes' if result['ready'] else 'NO'} (as of {result['as_of']})"]
    width = max((len(c["name"]) for c in result["checks"]), default=0)
    for check in result["checks"]:
        mark = "ok  " if check["ok"] else "FAIL"
        lines.append(f"  {mark}  {check['name']:<{width}}  {check['detail']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", type=Path, required=True, help="the private host file")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    parser.add_argument("--allow-paused", action="store_true", help="a paused team is ok")
    parser.add_argument("--vcs", type=Path, default=VCS_CONFIG)
    parser.add_argument("--repositories", type=Path, default=REPOSITORIES)
    args = parser.parse_args(argv)
    result = readiness(
        args.host,
        allow_paused=args.allow_paused,
        vcs_path=args.vcs,
        repositories_path=args.repositories,
    )
    print(json.dumps(result) if args.json else report(result))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
