"""code.session's gates on the host: agent-written code cannot forge results or the change.

Run by `coding_run.sh` (as root, which installs the fixture) as the kernel user:

    sudo sh experiments/debian-acceptance/coding_run.sh gate_isolation.py [WORK_DIR]

Each case is one `code.session` call through the real root helper, systemd and bubblewrap
under the installed `coding` profile. The harness writes `a.txt`. The gates are hostile
programs, as code in a checkout can be (cardea.hosts.isolation, ADR-037):

- **forge**: a gate opens `/proc/<worker>/fd/1` to write a forged result. The worker is
  not dumpable, so this is refused, and the reported result is the honest one.
- **detach**: a gate leaves a `setsid` grandchild that edits the change 1.5 s later, and
  a second gate waits 3 s. The grandchild is killed when its gate ends: the call succeeds,
  the gate record counts one stray killed, and the bundle holds the harness's `a.txt`.
- **kill**: a gate kills the worker. Nothing is reported: the call does not succeed and
  stores no object.
- **edit**: a gate rewrites `a.txt`. The call fails with `gate_changed_checkout`, and
  nothing is stored.

It also records which signals and system calls a gate can use against the worker in this
profile: the seccomp filter blocks `ptrace` (`@debug`), not `kill`.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coding_fixture import (  # noqa: E402
    FIXTURE,
    GATES_DIR,
    PYTHON,
    WRITE_HARNESS,
    entry,
    harness_config,
    merged_registry,
)
from common import Evidence, add_common_arguments, claim, rows, run_script, use_kernel  # noqa: E402

NAME = "gate-isolation"
NAMESPACE = "acc"

GATES: dict[str, str] = {
    "forge.py": (
        "import json, os\n"
        "forged = json.dumps({'output': {'gates_passed': True, 'forged': True}})\n"
        "try:\n"
        "    with open(f'/proc/{os.getppid()}/fd/1', 'w') as stream:\n"
        "        stream.write(forged)\n"
        "    print('forge: written')\n"
        "except OSError as error:\n"
        "    print('forge: denied', type(error).__name__)\n"
        "try:\n"
        "    open(f'/proc/{os.getppid()}/mem', 'rb').read(1)\n"
        "    print('mem: read')\n"
        "except OSError as error:\n"
        "    print('mem: denied', type(error).__name__)\n"
    ),
    "detach.py": (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        time.sleep(1.5)\n"
        "        with open('a.txt', 'a') as stream:\n"
        "            stream.write('tampered by a stray process\\n')\n"
        "        time.sleep(5)\n"
        "        os._exit(0)\n"
        "    os._exit(0)\n"
        "os.wait()\n"
        "print('detached a child')\n"
    ),
    "pause.py": "import time; time.sleep(3); print('second gate')\n",
    "kill.py": "import os, signal; os.kill(os.getppid(), signal.SIGKILL)\n",
    "edit.py": "open('a.txt', 'w').write('rewritten by a gate\\n'); print('edited')\n",
}
CASES: dict[str, list[str]] = {
    "acc-forge": ["forge.py"],
    "acc-detach": ["detach.py", "pause.py"],
    "acc-kill": ["kill.py"],
    "acc-edit": ["edit.py"],
}


def gate(name: str) -> dict[str, Any]:
    return {
        "name": name.removesuffix(".py"),
        "argv": [PYTHON, f"{GATES_DIR}/{name}"],
        "timeout_seconds": 60,
    }


def registry_entries() -> dict[str, Any]:
    return {alias: entry([gate(g) for g in gates]) for alias, gates in CASES.items()}


def prepare(store: Path) -> None:
    from datetime import timedelta

    from cardea.service.admin import initialize
    from cardea.service.database import Database
    from cardea.service.kernel import provision
    from cardea.service.models import RIGHTS, Permission, Principal, Provisioning, now
    from cardea.service.operations import Registry
    from long_call import descriptor

    initialize(store)
    rights = (*RIGHTS, "code.session", *(f"repo:{alias}" for alias in CASES))
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace=NAMESPACE,
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(days=1),
        )
        for r in rights
    )
    principals = (
        Principal(
            principal_id="runtime",
            kind="service",
            auth_kind="spiffe",
            issuer="acceptance",
            subject="runtime",
        ),
        Principal(
            principal_id="supervisor",
            kind="human",
            auth_kind="spiffe",
            issuer="acceptance",
            subject="supervisor",
        ),
        Principal(principal_id="agent", kind="agent", runtime_id="runtime"),
    )
    database = Database(store)
    try:
        provision(
            database,
            Provisioning(
                principals=principals,
                permissions=granted,
                platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
            ),
            Registry((descriptor(),), {}),
        )
    finally:
        database.close()


def call(store: Path, alias: str) -> tuple[str, Any]:
    """One code.session call on `alias`, run to its end by the kernel's operation runtime."""
    from cardea.service.database import borrow
    from cardea.service.models import OperationTaskRequest
    from cardea.service.operation_runtime import OperationRuntime
    from long_call import kernel

    instance = kernel(store)
    try:
        request = OperationTaskRequest.model_validate(
            {
                "request_id": f"gate-{alias}-{uuid.uuid4().hex[:8]}",
                "agent_id": "agent",
                "namespace": NAMESPACE,
                "objective": f"Hostile gates ({alias})",
                "operation": "code.session",
                "arguments": {
                    "repository": alias,
                    "base_ref": "main",
                    "task": json.dumps({"write": {"a.txt": "written by the harness\n"}}),
                    "task_key": f"{alias}-{uuid.uuid4().hex[:8]}",
                },
                "grants": ["objects.read", "objects.write", "code.session", f"repo:{alias}"],
                "timeout_seconds": 900,
            }
        )
        task_id = instance.submit(instance.auth.validate("supervisor"), request).task.task_id
        state = OperationRuntime(instance).execute(task_id)
        with instance.database.read() as conn, borrow(conn) as tx:
            settled = tx.get_operation(task_id, "execute")
        return state, settled
    finally:
        instance.database.close()


def objects_of(store: Path, receipt_id: str) -> int:
    found = rows(
        store / "kernel.sqlite3",
        "SELECT COUNT(*) AS n FROM objects WHERE resource_id LIKE ?",
        f"{receipt_id}.%",
    )
    return int(found[0]["n"])


def bundle_file(store: Path, artifact: dict[str, Any], name: str) -> str:
    """A file as the stored bundle has it, applied to a fresh clone of the fixture."""
    from cardea.service import artifacts
    from cardea.service.database import Database

    database = Database(store)
    try:
        content = artifacts.read(database, artifact)
    finally:
        database.close()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "c.bundle").write_bytes(content)
        subprocess.run(["git", "clone", "--quiet", FIXTURE, str(root / "check")], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root / "check"),
                "fetch",
                "--quiet",
                str(root / "c.bundle"),
                "HEAD:refs/heads/c",
            ],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(root / "check"), "show", f"c:{name}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout


def run(args: argparse.Namespace) -> int:
    use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    store = work / "store"

    def body() -> None:
        prepare(store)
        state, settled = call(store, "acc-forge")
        output = (settled.result or {}).get("output") or {}
        tail = (output.get("gates") or [{}])[0].get("output_tail", "")
        evidence.check(
            "forge_through_proc_refused",
            state == "succeeded" and "forge: denied" in tail and "forged" not in json.dumps(output),
            gate_output=tail,
            isolation=output.get("isolation"),
        )
        evidence.check("worker_memory_unreadable", "mem: denied" in tail, gate_output=tail)

        state, settled = call(store, "acc-detach")
        output = (settled.result or {}).get("output") or {}
        gates = output.get("gates") or []
        killed = gates[0].get("stray_processes_killed") if gates else None
        content = (
            bundle_file(store, output["artifacts"][0], "a.txt") if output.get("artifacts") else None
        )
        evidence.check(
            "detached_child_killed_before_it_tampers",
            state == "succeeded"
            and isinstance(killed, int)
            and killed >= 1
            and content == "written by the harness\n",
            killed=killed,
            bundle_a_txt=content,
            gates=gates,
        )

        state, settled = call(store, "acc-kill")
        evidence.check(
            "killed_worker_reports_nothing",
            state != "succeeded"
            and not (settled.result or {}).get("output")
            and objects_of(store, settled.receipt_id) == 0,
            state=state,
            receipt_state=str(settled.state),
        )

        state, settled = call(store, "acc-edit")
        code = settled.failure.code if settled.failure else None
        evidence.check(
            "gate_editing_the_checkout_refused",
            state == "failed"
            and code == "gate_changed_checkout"
            and objects_of(store, settled.receipt_id) == 0,
            state=state,
            code=code,
        )

    return run_script(evidence, body)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add_common_arguments(commands.add_parser("run", help="run every case"), NAME)
    commands.add_parser("config", help="print the code.session configuration")
    registry = commands.add_parser("registry", help="print the host registry plus the cases")
    registry.add_argument("existing", type=Path)
    gates = commands.add_parser("gates", help="write the hostile gate files")
    gates.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.command == "config":
        print(json.dumps(harness_config(WRITE_HARNESS, timeout=300), indent=2))
        return 0
    if args.command == "registry":
        print(json.dumps(merged_registry(args.existing, registry_entries()), indent=2))
        return 0
    if args.command == "gates":
        for name, code in GATES.items():
            (args.directory / name).write_text(code)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
