"""ADR-038 on the host: `env.try` in the real `try` profile. Not yet run.

Run by `env_try.sh` (as root, which installs the fixture and its catalog) as the kernel user:

    env_try.py run [--work DIR]

Each call is `env.try` from `cardea-env-ops` under the installed `try` profile, through the
real root helper, systemd and bubblewrap, on a fixture repository with three commits: one
whose scenarios pass, one whose `try` fails, and a hostile one. Phases (the ADR's
acceptance tests):

1. **Pass and fail.** The good commit passes `try` and `teams`; the bad one fails `try`
   with its exit status, a bounded log tail and its artifact as an object. No unit and no
   job directory of the calls is left.
2. **Same receipt.** The same commit and scenarios return the first receipt (`cached`)
   without starting a unit; `rerun: true` runs again, linked to it; a fourth forced rerun
   of one commit in a day is refused (`env_rerun_limit`).
3. **Confinement.** The hostile commit's scenario tries to read the store, reach the helper
   and the kernel's loopback, open other jobs' staging, resolve DNS, reach the metadata
   address, reach an unlisted host through the proxy and fill the disk. It reports each
   attempt; every one must fail, and the host must be unaffected.
4. **Caps.** A scenario that outlives its time limit is `timeout`; the remaining scenarios
   still report.
5. **Concurrency.** With the profile's concurrency of 1, an identical call made while the
   first runs is refused with `env_run_in_progress` and starts nothing.

The calls run in this script's own process on its own store, exactly the step the daemon's
workflow runs (`OperationRuntime.execute`).
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Evidence,
    add_common_arguments,
    claim,
    rows,
    run_script,
    use_kernel,
)

NAME = "env-try"
NAMESPACE = "acc"
ALIAS = "env-acceptance"
# The scenario script in the fixture: its behaviour comes from the commit's `mode` file.
SCENARIO = r"""
import json, os, pathlib, socket, sys, time, urllib.request
mode = pathlib.Path("mode").read_text().strip()
name = sys.argv[1]
print("scenario", name, "mode", mode)
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/report.json").write_text(json.dumps({"scenario": name, "mode": mode}))
if name == "slow":
    time.sleep(3600)
if mode == "bad" and name == "try":
    sys.exit(3)
if mode != "hostile":
    sys.exit(0)
tried = {}
def attempt(label, action):
    try:
        action()
        tried[label] = "reached"
    except Exception as error:
        tried[label] = type(error).__name__
attempt("store", lambda: open("/var/lib/cardea/kernel.sqlite3", "rb").read(1))
attempt("helper_spool", lambda: os.listdir("/var/lib/cardea-host"))
attempt("credentials", lambda: os.listdir("/etc/cardea/credentials"))
attempt("kernel_loopback", lambda: socket.create_connection(("127.0.0.1", 8443), 3).close())
attempt("dns", lambda: socket.getaddrinfo("example.com", 443))
attempt("metadata", lambda: socket.create_connection(("169.254.169.254", 80), 3).close())
attempt("unlisted_host", lambda: urllib.request.urlopen("https://example.com/", timeout=10))
def fill():
    with open("fill.bin", "wb") as stream:
        for _ in range(1 << 16):
            stream.write(b"\0" * (1 << 20))
attempt("fill_disk", fill)
print("HOSTILE", json.dumps(tried, sort_keys=True))
sys.exit(0)
"""


def catalog(source: str) -> dict[str, Any]:
    """The registry entry `env_try.sh` installs: the fixture and its scenarios, which run
    the commit's own `scenario.py` (its code is the untrusted part)."""
    run = ["/usr/bin/python3", "-I", "scenario.py"]
    return {
        "repositories": {
            ALIAS: {
                "source": source,
                "environment": {
                    "tiers": ["process"],
                    "scenarios": {
                        "try": {
                            "argv": [*run, "try"],
                            "timeout_seconds": 600,
                            "artifacts": ["out/report.json"],
                        },
                        "teams": {"argv": [*run, "teams"], "timeout_seconds": 600},
                        "slow": {"argv": [*run, "slow"], "timeout_seconds": 20},
                    },
                },
            }
        }
    }


class Auth:
    def runtime_proof(self) -> Any:
        from cardea.service.auth import Proof

        return Proof("spiffe", "acceptance", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Any:
        from cardea.service.auth import Proof

        return Proof("spiffe", "acceptance", token, time.time() + 60)


class NoQueue:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


def kernel(store: Path) -> Any:
    from cardea.hosts import launcher
    from cardea.hosts.debian import DebianHost
    from cardea.service import kernel as handlers
    from cardea.service.confined import Workers
    from cardea.service.database import Database
    from cardea.service.kernel import Kernel
    from cardea.service.operations import Registry
    from cardea_env_ops import descriptors

    handlers.DBOSClient = NoQueue  # type: ignore[assignment,misc]
    host = DebianHost()
    host.probe()
    instance = Kernel(
        Database(store),
        Auth(),  # type: ignore[arg-type]
        operations=Registry(descriptors(), {}),
        workers=Workers(host),
    )
    instance.endpoint_host = launcher.ENDPOINT_ADDRESS
    return instance


def prepare(store: Path) -> None:
    from cardea.service.admin import initialize
    from cardea.service.database import Database
    from cardea.service.kernel import provision
    from cardea.service.models import RIGHTS, Permission, Principal, Provisioning, now
    from cardea.service.operations import Registry
    from cardea_env_ops import descriptors

    initialize(store)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace=NAMESPACE,
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(days=1),
        )
        for right in (*RIGHTS, "env.try", f"repo:{ALIAS}")
    )
    principals = (
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="acceptance",
            subject=p,
        )
        for p in ("runtime", "supervisor")
    )
    database = Database(store)
    try:
        provision(
            database,
            Provisioning(
                principals=(
                    *principals,
                    Principal(principal_id="agent", kind="agent", runtime_id="runtime"),
                ),
                permissions=granted,
                platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
            ),
            Registry(descriptors(), {}),
        )
    finally:
        database.close()


def call(store: Path, commit: str, scenarios: list[str], rerun: bool = False) -> dict[str, Any]:
    """One `env.try` call; its task state and receipt, or the admission refusal."""
    from cardea.service.database import Fault, borrow
    from cardea.service.models import OperationTaskRequest
    from cardea.service.operation_runtime import OperationRuntime

    instance = kernel(store)
    try:
        request = OperationTaskRequest.model_validate(
            {
                "request_id": f"env-{uuid.uuid4().hex[:12]}",
                "agent_id": "agent",
                "namespace": NAMESPACE,
                "objective": "Try a commit",
                "operation": "env.try",
                "arguments": {
                    "repo": ALIAS,
                    "commit": commit,
                    "scenarios": scenarios,
                    **({"rerun": True} if rerun else {}),
                },
                "grants": ["objects.read", "objects.write", "env.try", f"repo:{ALIAS}"],
                "timeout_seconds": 3600,
            }
        )
        task_id = instance.submit(instance.auth.validate("supervisor"), request).task.task_id
        state = OperationRuntime(instance).execute(task_id)
        with instance.database.read() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            try:
                receipt = tx.get_operation(task_id, "execute")
            except Exception:
                receipt = None
        return {
            "state": state,
            "failure": task.failure.code if task.failure else None,
            "receipt_id": receipt.receipt_id if receipt else None,
            "output": (receipt.result or {}).get("output") if receipt else None,
            "receipt_failure": receipt.failure.code if receipt and receipt.failure else None,
        }
    except Fault as refused:
        return {"state": "refused", "failure": refused.code}
    finally:
        instance.database.close()


def leftovers() -> dict[str, Any]:
    units = subprocess.run(
        ["/usr/bin/systemctl", "list-units", "--all", "--no-legend", "cardea-host-*"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return {"units": [u for u in units if u.startswith("cardea-host-")]}


def run(args: argparse.Namespace) -> int:
    use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    store = work / "store"
    commits = json.loads(Path(args.commits).read_text())

    def body() -> None:
        prepare(store)
        before = leftovers()
        # 1. Pass and fail.
        good = call(store, commits["good"], ["try", "teams"])
        output = good.get("output") or {}
        evidence.check("good_commit_passes", output.get("passed") is True, **good)
        bad = call(store, commits["bad"], ["try"])
        scenario = ((bad.get("output") or {}).get("scenarios") or [{}])[0]
        evidence.check(
            "bad_commit_fails_with_its_status",
            (bad.get("output") or {}).get("passed") is False and scenario.get("exit_code") == 3,
            scenario=scenario,
        )
        listed = (good.get("output") or {}).get("artifacts") or []
        evidence.check("artifact_stored_as_object", len(listed) == 1, artifacts=listed)
        evidence.check("nothing_left_on_the_host", leftovers() == before, left=leftovers())

        # 2. Same receipt, reruns.
        again = call(store, commits["good"], ["teams", "try"])
        cached = again.get("output") or {}
        evidence.check(
            "same_inputs_same_receipt",
            cached.get("cached") is True and cached.get("cached_receipt_id") == good["receipt_id"],
            **again,
        )
        forced = [call(store, commits["good"], ["try", "teams"], rerun=True) for _ in range(3)]
        evidence.check(
            "three_forced_reruns_run",
            all((f.get("output") or {}).get("cached") is False for f in forced),
        )
        fourth = call(store, commits["good"], ["try", "teams"], rerun=True)
        evidence.check("fourth_rerun_refused", fourth.get("failure") == "env_rerun_limit", **fourth)

        # 3. Confinement.
        hostile = call(store, commits["hostile"], ["try"])
        tail = str((((hostile.get("output") or {}).get("scenarios") or [{}])[0]).get("log_tail"))
        line = next((x for x in tail.splitlines() if x.startswith("HOSTILE ")), "")
        tried = json.loads(line.removeprefix("HOSTILE ")) if line else {}
        evidence.observe("hostile_attempts", tried)
        evidence.check(
            "every_hostile_attempt_fails",
            bool(tried) and all(v != "reached" for v in tried.values()),
            tried=tried,
        )
        evidence.check("nothing_left_after_hostile", leftovers() == before, left=leftovers())

        # 4. Caps.
        slow = call(store, commits["good"], ["slow", "try"])
        outcomes = [s.get("outcome") for s in (slow.get("output") or {}).get("scenarios", [])]
        evidence.check("time_limit_is_timeout", outcomes == ["timeout", "pass"], outcomes=outcomes)

        # 5. Concurrency: an identical call while the first runs.
        results: dict[str, Any] = {}
        first = threading.Thread(
            target=lambda: results.update(first=call(store, commits["bad"], ["slow"]))
        )
        first.start()
        time.sleep(5)
        results["second"] = call(store, commits["bad"], ["slow"])
        first.join()
        evidence.check(
            "identical_call_in_progress_refused",
            results["second"].get("failure") == "env_run_in_progress",
            **results,
        )
        runs = rows(
            store / "kernel.sqlite3", "SELECT state, COUNT(*) AS n FROM env_runs GROUP BY state"
        )
        evidence.observe("env_runs", runs)

    return run_script(evidence, body)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run", help="run every phase and write the evidence")
    add_common_arguments(running, NAME)
    running.add_argument("--commits", required=True, help="the fixture's commits (JSON)")
    registry = commands.add_parser("repositories", help="print the registry entry")
    registry.add_argument("source")
    commands.add_parser("scenario", help="print the fixture's scenario script")
    args = parser.parse_args(argv)
    if args.command == "repositories":
        print(json.dumps(catalog(args.source), indent=2))
        return 0
    if args.command == "scenario":
        sys.stdout.write(SCENARIO)
        return 0
    os.umask(0o077)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
