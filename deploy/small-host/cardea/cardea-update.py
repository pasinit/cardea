"""Bring a team host's /opt/cardea checkout to the newest main commit, safely and unattended.

Run as root, usually from `cardea-update.timer`:

    python3 -I cardea-update.py --host /etc/cardea/team.host.json [--require-ci] [--approve]
        [--target SHA] [--dry-run]

It deploys a commit only when it can do so without stranding the team's runs:

* **Hot update** (team kit, role instructions, dashboard pages, operation workers, docs):
  check out, reinstall the ops packages installed as copies, add new descriptors, worker
  profiles and workers, re-apply the team. No kernel restart, no run is cancelled.
* **Restart update** (kernel code, its dependencies, operation descriptors): first `drain`
  (no new runs are admitted, the running ones finish), then the same steps plus a kernel
  restart, then `apply` (which admits again).
* **Owner approval**: a change to what runs as root on the host (the host helper and the
  deploy files) is not deployed until it is run with `--approve`.

After either update it checks the kernel, the host helper and the team's status; if a check
fails it goes back to the previous commit the same way and exits 1. With `--require-ci` a
commit is deployed only when every GitHub check run on it succeeded (the repository is
public, so no token is needed). Every run appends one JSON line to /var/log/cardea-update.log.
"""

import argparse
import fcntl
import json
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

CHECKOUT = Path("/opt/cardea")
VENV_PYTHON = CHECKOUT / "apps/api/.venv/bin/python"
TEAMKIT = CHECKOUT / "deploy/team-kit/teamkit.py"
HELPER = Path("/usr/libexec/cardea-host")
LAUNCHER = "apps/api/src/cardea/hosts/launcher.py"
WORKERS = Path("/etc/cardea-host-workers.json")
WORKERS_SOURCE = "deploy/small-host/cardea/cardea-host-workers.json"
OPERATIONS = Path("/etc/cardea/operations")
LOG = Path("/var/log/cardea-update.log")
LOCK = Path("/run/cardea-update.lock")
KERNEL_USER = "cardea-kernel"

# What runs as root on the host: deployed only with --approve.
ROOT_PATHS = (LAUNCHER, "deploy/small-host/*", "packaging/debian/*")
# What the long-running kernel process loads: needs a drained restart.
RESTART_PATHS = (
    "apps/api/src/cardea/*",
    "apps/api/pyproject.toml",
    "apps/api/uv.lock",
    "packages/*/descriptors/*.json",
)
# Run in a fresh process per call, so a new checkout is picked up without a restart.
HOT_EXCEPTIONS = ("apps/api/src/cardea/hosts/operation_worker.py",)
# Host data the update syncs itself (new entries only), so they need no approval.
SYNCED_PATHS = (WORKERS_SOURCE,)


def run(*argv: str, check: bool = True, user: str | None = None) -> str:
    command = ["runuser", "-u", user, "--", *argv] if user else list(argv)
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {done.stderr.strip()[-400:]}"
        )
    return done.stdout


def git(*args: str) -> str:
    return run("git", "-c", f"safe.directory={CHECKOUT}", "-C", str(CHECKOUT), *args).strip()


def teamkit(host: Path, *args: str, check: bool = True) -> str:
    return run(
        str(VENV_PYTHON),
        "-I",
        str(TEAMKIT),
        *args,
        "--host",
        str(host),
        check=check,
        user=KERNEL_USER,
    )


def classify(paths: list[str]) -> dict[str, list[str]]:
    """Which of the changed paths need the owner's approval, and which a restart."""

    def matches(path: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch(path, pattern) for pattern in patterns)

    root = [p for p in paths if matches(p, ROOT_PATHS) and not matches(p, SYNCED_PATHS)]
    restart = [
        p
        for p in paths
        if (matches(p, RESTART_PATHS) and not matches(p, HOT_EXCEPTIONS)) or p == LAUNCHER
    ]
    return {"root": root, "restart": restart}


def checks_passed(sha: str) -> bool:
    """Every GitHub check run on the commit completed successfully (at least one ran)."""
    remote = git("remote", "get-url", "origin")
    slug = remote.removesuffix(".git").split("github.com")[-1].lstrip(":/")
    url = f"https://api.github.com/repos/{slug}/commits/{sha}/check-runs?per_page=100"
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "cardea-update"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        runs = json.load(response).get("check_runs", [])
    return bool(runs) and all(r.get("conclusion") == "success" for r in runs)


def install(target: str, old: str) -> list[str]:
    """Check out `target` and bring the host's copies in line. Returns what it did."""
    did = []
    git("checkout", "-q", "--detach", target)
    site = next((CHECKOUT / "apps/api/.venv/lib").glob("python3*/site-packages"))
    for package in sorted((CHECKOUT / "packages").iterdir()):
        module = package.name.replace("-", "_")
        if (site / module).is_dir():  # installed as a copy, not editable
            run(
                "/usr/local/bin/uv",
                "pip",
                "install",
                "-q",
                "--python",
                str(VENV_PYTHON),
                "--no-deps",
                "--reinstall",
                str(package),
            )
            did.append(f"{package.name} reinstalled")
    for source in sorted(CHECKOUT.glob("packages/cardea-*-ops/src/*/descriptors/*.json")):
        if "reference" in source.parts[-4]:
            continue
        installed = OPERATIONS / source.name
        relative = str(source.relative_to(CHECKOUT))
        if not installed.exists():
            did.append(f"descriptor {source.stem} installed")
        elif installed.read_bytes() == source.read_bytes():
            continue
        elif installed.read_bytes() not in _history(relative, old):
            did.append(f"descriptor {source.stem} LOCALLY CHANGED, left as is")
            continue
        else:
            did.append(f"descriptor {source.stem} updated")
        run(
            "install",
            "-m",
            "0600",
            "-o",
            KERNEL_USER,
            "-g",
            KERNEL_USER,
            str(source),
            str(installed),
        )
    did += _sync_workers()
    if git("rev-parse", f"{old}:{LAUNCHER}") != git("rev-parse", f"{target}:{LAUNCHER}"):
        run(
            "install",
            "-m",
            "0755",
            "-o",
            "root",
            "-g",
            "root",
            str(CHECKOUT / LAUNCHER),
            str(HELPER),
        )
        did.append("host helper installed")
    return did


def _history(path: str, old: str) -> list[bytes]:
    """The file as it was at `old` and in its earlier versions: an installed copy equal to
    one of them was never edited on the host, so it may be replaced."""
    versions = []
    for commit in git("log", "--format=%H", "-30", old, "--", path).split():
        shown = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={CHECKOUT}",
                "-C",
                str(CHECKOUT),
                "show",
                f"{commit}:{path}",
            ],
            capture_output=True,
            check=False,
        )
        if shown.returncode == 0:
            versions.append(shown.stdout)
    return versions


def _sync_workers() -> list[str]:
    """Adds the profiles and workers the repository has and the host lacks; never changes
    a tuned entry. A worker naming a missing profile would make the helper refuse the file."""
    current = json.loads(WORKERS.read_text())
    source = json.loads((CHECKOUT / WORKERS_SOURCE).read_text())
    added = []
    for section in ("profiles", "workers"):
        for name, entry in source[section].items():
            if name not in current[section]:
                current[section][name] = entry
                added.append(f"{section[:-1]} {name} added")
    if added:
        staged = WORKERS.with_suffix(".new")
        staged.write_text(json.dumps(current, indent=2) + "\n")
        staged.chmod(0o644)
        staged.replace(WORKERS)
    return added


def helper_ok() -> bool:
    done = subprocess.run(
        ["runuser", "-u", KERNEL_USER, "--", "sudo", "-n", str(HELPER)],
        input='{"action": "profiles"}',
        capture_output=True,
        text=True,
        check=False,
    )
    return '"profiles"' in done.stdout


def healthy(host: Path) -> tuple[bool, str]:
    for _ in range(40):
        status = teamkit(host, "status", check=False)
        if "as of" in status:
            break
        time.sleep(3)
    else:
        return False, "kernel did not answer status"
    if run("systemctl", "is-active", "cardea-kernel", check=False).strip() != "active":
        return False, "cardea-kernel is not active"
    if not helper_ok():
        return False, "host helper refuses its worker configuration"
    return True, "ok"


def deploy(host: Path, target: str, old: str, restart: bool) -> list[str]:
    did = install(target, old)
    if not helper_ok():
        raise RuntimeError("host helper refuses its worker configuration")
    if restart:
        run("systemctl", "restart", "cardea-kernel")
        did.append("kernel restarted")
    run("systemctl", "try-restart", "cardea-dashboard", check=False)
    for _ in range(40):
        if "as of" in teamkit(host, "status", check=False):
            break
        time.sleep(3)
    teamkit(host, "apply")
    did.append("team applied")
    return did


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--target", default="origin/main")
    parser.add_argument("--require-ci", action="store_true")
    parser.add_argument(
        "--approve", action="store_true", help="deploy a change to what runs as root on the host"
    )
    parser.add_argument("--drain-wait", type=float, default=2700.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another update is running")
            return 0
        return update(args)


def update(args: argparse.Namespace) -> int:
    git("fetch", "-q", "origin", "main")
    old, target = git("rev-parse", "HEAD"), git("rev-parse", args.target)
    entry: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "from": old[:7], "to": target[:7]}
    if old == target:
        print(f"up to date at {old[:7]}")
        return 0
    paths = git("diff", "--name-only", old, target).split()
    kinds = classify(paths)
    entry.update(kinds)
    restart = bool(kinds["restart"])
    plan = f"{old[:7]} -> {target[:7]}: {'restart' if restart else 'hot'} update"
    if args.require_ci and not checks_passed(target):
        print(f"{plan}: waiting for every check on {target[:7]} to pass")
        return 0
    if kinds["root"] and not args.approve:
        print(f"{plan}: needs the owner's approval (--approve) for {', '.join(kinds['root'])}")
        _log({**entry, "result": "needs_approval"})
        return 0
    print(plan)
    if args.dry_run:
        print("\n".join(f"  {k}: {v}" for k, v in kinds.items()))
        return 0
    if restart:
        left = teamkit(args.host, "drain", "--wait", str(args.drain_wait), check=False)
        print(left.strip())
    try:
        did = deploy(args.host, target, old, restart)
        ok, why = healthy(args.host)
        if not ok:
            raise RuntimeError(why)
    except (RuntimeError, OSError, StopIteration) as failed:
        print(f"FAILED: {failed}; going back to {old[:7]}")
        try:
            deploy(args.host, old, target, restart=True)
            back, why = healthy(args.host)
        except (RuntimeError, OSError) as again:
            back, why = False, str(again)
        _log(
            {
                **entry,
                "result": "rolled_back" if back else "rollback_failed",
                "error": str(failed)[:300],
            }
        )
        print("rolled back" if back else f"ROLLBACK FAILED: {why}")
        return 1
    print("\n".join(f"  {line}" for line in did))
    print(f"deployed {target[:7]}")
    _log({**entry, "result": "deployed", "did": did})
    return 0


def _log(entry: dict[str, Any]) -> None:
    with LOG.open("a") as log:
        log.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
