"""ADR-023 amendment on the host: long confined calls, lease renewal, fencing and artifacts.

Run by `long_call.sh` (as root, which installs the fixture) as the kernel user:

    long_call.py run [--work DIR] [--long-seconds 1200] [--kill-after 600]

The call is `code.session` from `cardea-coding-ops` under the installed `coding` profile,
through the real root helper, systemd and bubblewrap. Its configured harness is a plain
command that sleeps for the requested time inside the sandbox and then writes random bytes,
so the change bundle has a chosen, incompressible size. Phases:

1. **Renewal.** A call of `--long-seconds` (default 20 minutes) succeeds; its five-minute
   lease was renewed about once a minute (`task.lease_renewed` events).
2. **Artifacts.** That call's 9 MiB bundle is stored as task-owned objects of at most
   1 MiB and reads back by digest. It survives `backup`, `restore` and `verify`.
3. **Fencing.** A second call is started, and the service process running it is killed
   with SIGKILL after `--kill-after` seconds. The worker's process tree outlives it. A
   restarted service reaps the worker; the call becomes `outcome_unknown`, the task fails,
   the call is not started again, and no process of the worker is left.
4. **Oversized artifact.** A bundle above the descriptor's 10 MiB stores nothing and
   reconciles as not applied.
5. **Limits.** A descriptor whose timeout exceeds the installed profile's `wall_seconds`
   is refused at admission with `operation_timeout_exceeds_profile`, and one above 7200 s
   is invalid.
6. **Loopback fence (ADR-028).** From inside the `coding` worker, the harness connects to
   a kernel listener on 127.0.0.1 (`--kernel-port`, or a stand-in this script opens) and
   to a real per-attempt tool endpoint on the helper's dedicated address. The first
   fails, the second answers, and the tool endpoint is not on 127.0.0.1.

The "service" is this script's own child process running the kernel's operation runtime
(`OperationRuntime.execute`), exactly the step the daemon's workflow runs, on this script's
store. It is not the daemon: the development identity cannot enable host execution, and a
SPIFFE-enrolled daemon would need the M5 fixture. Restart of the real daemon under systemd
is covered by the M5 acceptance (`experiments/m5`).
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coding_fixture import FIXTURE, merged_registry  # noqa: E402
from common import (  # noqa: E402
    HERE,
    Evidence,
    Processes,
    add_common_arguments,
    claim,
    rows,
    run_script,
    use_kernel,
    wait_for,
)

NAME = "long-call"
NAMESPACE = "acc"
REPOSITORY_ALIAS = "acceptance"
MIB = 1024 * 1024
# The harness inside the sandbox: sleep in a child whose argv carries a unique token, so
# the host can find the worker's processes by it, then write the payload file.
HARNESS = (
    "import json, os, subprocess, sys\n"
    "task = json.loads(os.environ['CARDEA_TASK'])\n"
    "subprocess.run([sys.executable, '-c', 'import sys, time; time.sleep(float(sys.argv[1]))',"
    " str(task['sleep']), task['token']], check=True)\n"
    "with open('payload.bin', 'wb') as stream:\n"
    "    stream.write(os.urandom(int(task['bytes'])))\n"
    "print('slept', task['sleep'], 'wrote', task['bytes'])\n"
    # Phase 6: which loopback listeners the worker reaches, without any proxy.
    "import socket\n"
    "seen = {}\n"
    "for name, (host, port) in task.get('probe', {}).items():\n"
    "    try:\n"
    "        with socket.create_connection((host, port), timeout=5) as conn:\n"
    "            conn.sendall(b'POST /mcp HTTP/1.0\\r\\nContent-Length: 0\\r\\n\\r\\n')\n"
    "            first = conn.recv(64).split(b'\\r\\n')[0].decode('ascii', 'replace')\n"
    "        seen[name] = {'connected': True, 'status_line': first}\n"
    "    except OSError as error:\n"
    "        seen[name] = {'connected': False, 'error': type(error).__name__}\n"
    "if seen:\n"
    "    print('LOOPBACK', json.dumps(seen, sort_keys=True))\n"
)


def repositories(source: str) -> dict[str, Any]:
    """The repository registry (ADR-037) `long_call.sh` installs for this test: the fixture
    as a read-only alias."""
    return {
        "repositories": {
            REPOSITORY_ALIAS: {
                "source": source,
                "gates": [{"name": "true", "argv": ["/bin/true"], "timeout_seconds": 60}],
            }
        }
    }


def coding_config(source: str) -> dict[str, Any]:
    """The `code.session` configuration `long_call.sh` installs for this test; its
    repositories are in `repositories(source)`."""
    return {
        "harness": {"argv": ["/usr/bin/python3", "-I", "-c", HARNESS], "timeout_seconds": 3500},
    }


class Auth:
    """Fixed principals for the script's own store; never used outside it."""

    def runtime_proof(self) -> Any:
        from cardea.service.auth import Proof

        return Proof("spiffe", "acceptance", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Any:
        from cardea.service.auth import Proof

        return Proof("spiffe", "acceptance", token, time.time() + 60)


class NoQueue:
    """The script runs each call itself, in its service child; nothing goes to DBOS."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


def descriptor(timeout: int | None = None) -> Any:
    from importlib.resources import files

    from cardea.service.operations import OperationDescriptor

    document = json.loads(
        (files("cardea_coding_ops") / "descriptors" / "code.session.json").read_text()
    )
    if timeout is not None:
        document["cardea"]["timeout_seconds"] = timeout
    return OperationDescriptor.model_validate(document)


def kernel(store: Path, timeout: int | None = None) -> Any:
    from cardea.hosts.debian import DebianHost
    from cardea.service import kernel as handlers
    from cardea.service.confined import Workers
    from cardea.service.database import Database
    from cardea.service.kernel import Kernel
    from cardea.service.operations import Registry

    handlers.DBOSClient = NoQueue  # type: ignore[assignment,misc]
    host = DebianHost()
    host.probe()
    registry = Registry((descriptor(timeout),), {})
    return Kernel(Database(store), Auth(), operations=registry, workers=Workers(host))  # type: ignore[arg-type]


def prepare(store: Path) -> None:
    from cardea.service.admin import initialize
    from cardea.service.database import Database
    from cardea.service.kernel import provision
    from cardea.service.models import RIGHTS, Permission, Principal, Provisioning, now
    from cardea.service.operations import Registry

    initialize(store)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace=NAMESPACE,
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(days=1),
        )
        for right in (*RIGHTS, "code.session", f"repo:{REPOSITORY_ALIAS}")
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


def submit(
    store: Path,
    key: str,
    sleep: float,
    size: int,
    token: str,
    timeout: int | None = None,
    probe: dict[str, list[Any]] | None = None,
) -> str:
    from cardea.service.models import OperationTaskRequest

    instance = kernel(store, timeout)
    try:
        request = OperationTaskRequest.model_validate(
            {
                "request_id": f"long-{key}",
                "agent_id": "agent",
                "namespace": NAMESPACE,
                "objective": f"Long confined call ({key})",
                "operation": "code.session",
                "arguments": {
                    "repository": REPOSITORY_ALIAS,
                    "base_ref": "main",
                    "task": json.dumps(
                        {"sleep": sleep, "bytes": size, "token": token, "probe": probe or {}}
                    ),
                    "task_key": f"{key}-{token[:8]}",
                },
                "grants": [
                    "objects.read",
                    "objects.write",
                    "code.session",
                    f"repo:{REPOSITORY_ALIAS}",
                ],
                "timeout_seconds": 3600,
            }
        )
        return str(instance.submit(instance.auth.validate("supervisor"), request).task.task_id)
    finally:
        instance.database.close()


def serve(store: Path, task_id: str) -> int:
    """The service child: run (or recover) one call with the kernel's operation runtime."""
    from cardea.service.operation_runtime import OperationRuntime

    instance = kernel(store)
    try:
        state = OperationRuntime(instance).execute(task_id)
    finally:
        instance.database.close()
    print(json.dumps({"task_id": task_id, "state": state}), flush=True)
    return 0


def worker_processes(token: str) -> list[int]:
    """PIDs whose command line carries the call's token: the worker's sleeping child."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if token.encode() in (entry / "cmdline").read_bytes():
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def call_rows(store: Path, task_id: str) -> dict[str, Any]:
    database = store / "kernel.sqlite3"
    task = rows(database, "SELECT state FROM tasks WHERE task_id=?", task_id)
    operations = rows(database, "SELECT receipt_id, state FROM operations WHERE task_id=?", task_id)
    renewed = rows(
        database,
        "SELECT COUNT(*) AS n FROM events WHERE event_type='task.lease_renewed' "
        "AND correlation_id=?",
        task_id,
    )
    return {
        "task_state": task[0]["state"] if task else None,
        "operations": operations,
        "renewals": int(renewed[0]["n"]),
    }


def receipt(store: Path, task_id: str) -> Any:
    from cardea.service.database import Database, borrow

    database = Database(store)
    try:
        with database.read() as conn, borrow(conn) as tx:
            return tx.get_operation(task_id, "execute")
    finally:
        database.close()


def run(args: argparse.Namespace) -> int:
    lib = use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    processes = Processes(work)
    store = work / "store"

    def service(task_id: str, label: str) -> Any:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), str(lib)])}
        script = str(Path(__file__).resolve())
        argv = [sys.executable, "-u", script, "serve", "--work", str(work), "--task", task_id]
        argv += ["--cardea-lib", str(lib)]
        return processes.start(argv, work / f"service-{label}.log", env)

    def body() -> None:
        from cardea.service import artifacts, maintenance
        from cardea.service.admin import backup, restore
        from cardea.service.database import Database, Fault

        prepare(store)
        probe = kernel(store)
        try:
            installed = probe.workers.installed()
        finally:
            probe.database.close()
        profile = installed.get("profiles", {}).get("coding")
        if not isinstance(profile, dict):
            raise SystemExit("the helper has no 'coding' profile; install it first")
        evidence.check(
            "code_session_worker_installed",
            "coding" in installed.get("workers", {}).get("code-session", ()),
        )
        evidence.observe("profile", profile)
        wall = int(profile.get("wall_seconds", 0))
        evidence.check(
            "coding_profile_installed", wall >= args.long_seconds + 300, wall_seconds=wall
        )

        # 1. Renewal, and 2. artifacts.
        token = uuid.uuid4().hex
        task_id = submit(store, "renew", args.long_seconds, args.bundle_mib * MIB, token)
        started = time.monotonic()
        process = service(task_id, "renew")
        process.wait(timeout=args.long_seconds + 900)
        took = time.monotonic() - started
        observed = call_rows(store, task_id)
        evidence.check("long_call_succeeds", observed["task_state"] == "succeeded", **observed)
        evidence.check(
            "lease_renewed_about_every_minute",
            observed["renewals"] >= int(args.long_seconds / 60) - 2,
            seconds=round(took),
            renewals=observed["renewals"],
        )
        settled = receipt(store, task_id)
        listed = (settled.result or {}).get("output", {}).get("artifacts", [])
        evidence.check(
            "bundle_artifact_listed",
            len(listed) == 1,
            artifacts=listed and [{k: v for k, v in a.items() if k != "parts"} for a in listed],
        )
        if listed:
            bundle = listed[0]
            database = Database(store)
            try:
                content = artifacts.read(database, bundle)
            finally:
                database.close()
            evidence.check(
                "artifact_reads_back_by_digest",
                hashlib.sha256(content).hexdigest() == bundle["sha256"],
            )
            evidence.check(
                "artifact_parts_at_most_one_mib",
                len(bundle["parts"]) == -(-len(content) // MIB) and len(content) > 8 * MIB,
                bytes=len(content),
                parts=len(bundle["parts"]),
            )
            snapshot = work / "backup.sqlite3"
            backup(store, snapshot)
            restored = work / "restored"
            restored.mkdir(mode=0o700)
            restore(restored, snapshot)
            report = maintenance.verify(restored)
            evidence.check("restored_store_verifies", bool(report), verify=report)
            again = Database(restored)
            try:
                copied = artifacts.read(again, bundle)
            finally:
                again.close()
            evidence.check(
                "artifact_survives_backup_and_restore",
                hashlib.sha256(copied).hexdigest() == bundle["sha256"],
            )

        # 3. Fencing: kill the service mid-call.
        token = uuid.uuid4().hex
        task_id = submit(store, "fence", args.long_seconds + 600, MIB, token)
        process = service(task_id, "fence-1")
        wait_for(lambda: worker_processes(token), 300, 2)
        time.sleep(args.kill_after)
        before = worker_processes(token)
        Processes.kill(process)
        evidence.check("worker_outlives_the_killed_service", bool(before), pids=before)
        killed_at = time.monotonic()
        recovery = service(task_id, "fence-2")
        recovery.wait(timeout=900)
        evidence.observe("recovery_seconds", round(time.monotonic() - killed_at))
        time.sleep(30)
        left = worker_processes(token)
        evidence.check("worker_process_tree_is_gone", not left, left=left)
        observed = call_rows(store, task_id)
        evidence.check(
            "fenced_call_is_outcome_unknown",
            [o["state"] for o in observed["operations"]] == ["outcome_unknown"],
            **observed,
        )
        evidence.check("fenced_task_failed", observed["task_state"] == "failed")
        time.sleep(60)
        evidence.check("fenced_call_not_started_again", not worker_processes(token))

        # 4. An oversized artifact.
        token = uuid.uuid4().hex
        task_id = submit(store, "oversize", 5, int(args.oversize_mib * MIB), token)
        service(task_id, "oversize").wait(timeout=900)
        observed = call_rows(store, task_id)
        stored = rows(
            store / "kernel.sqlite3",
            "SELECT COUNT(*) AS n FROM objects WHERE task_id=?",
            task_id,
        )
        evidence.check(
            "oversized_artifact_stores_nothing",
            int(stored[0]["n"]) == 0
            and [o["state"] for o in observed["operations"]] == ["outcome_unknown"],
            objects=int(stored[0]["n"]),
            **observed,
        )
        from cardea.service.operation_runtime import OperationRuntime

        instance = kernel(store)
        try:
            reconciled = OperationRuntime(instance).reconcile(task_id, "execute")
        finally:
            instance.database.close()
        evidence.check("oversized_call_reconciles_as_not_applied", reconciled == "failed")

        # 5. Limits against the installed profile.
        try:
            submit(store, "too-long", 5, MIB, uuid.uuid4().hex, timeout=min(7200, wall + 60))
            refused = None
        except Fault as error:
            refused = error.code
        evidence.check(
            "timeout_beyond_profile_refused",
            refused == "operation_timeout_exceeds_profile",
            code=refused,
        )
        try:
            descriptor(7201)
            invalid = False
        except ValueError:
            invalid = True
        evidence.check("timeout_above_two_hours_invalid", invalid)

        # 6. The loopback fence: only the dedicated endpoint address is reachable.
        loopback_fence(store, service, evidence, args.kernel_port)

    try:
        return run_script(evidence, body)
    finally:
        processes.close()


def loopback_fence(store: Path, service: Any, evidence: Evidence, kernel_port: int | None) -> None:
    """Phase 6: from inside the worker, 127.0.0.1 is closed and the endpoint answers."""
    import socket
    import threading

    from cardea.hosts import launcher
    from cardea.service.tool_endpoint import ToolEndpoint

    async def no_call(name: str, arguments: dict[str, Any], gone: Any) -> Any:
        raise RuntimeError("the probe calls no tool")

    stand_in: socket.socket | None = None
    if kernel_port is None:
        # A stand-in for the kernel API or any operator service on 127.0.0.1.
        stand_in = socket.create_server(("127.0.0.1", 0))
        kernel_port = int(stand_in.getsockname()[1])

        def answer() -> None:
            assert stand_in is not None
            while True:
                try:
                    conn, _ = stand_in.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")

        threading.Thread(target=answer, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", kernel_port), timeout=5):
            host_reaches = True
    except OSError:
        host_reaches = False
    evidence.check("host_reaches_the_kernel_listener", host_reaches, port=kernel_port)
    try:
        with ToolEndpoint([], no_call, launcher.ENDPOINT_ADDRESS) as endpoint:
            try:
                with socket.create_connection(("127.0.0.1", endpoint.port), timeout=5):
                    on_default = True
            except OSError:
                on_default = False
            evidence.check("tool_endpoint_not_on_127_0_0_1", not on_default, url=endpoint.url)
            probe = {
                "kernel": ["127.0.0.1", kernel_port],
                "endpoint": [launcher.ENDPOINT_ADDRESS, endpoint.port],
            }
            task_id = submit(store, "loopback", 0, 1024, uuid.uuid4().hex, probe=probe)
            service(task_id, "loopback").wait(timeout=900)
    finally:
        if stand_in is not None:
            stand_in.close()
    output = (receipt(store, task_id).result or {}).get("output", {})
    tail = str(output.get("harness", {}).get("output_tail", ""))
    line = next((x for x in tail.splitlines() if x.startswith("LOOPBACK ")), "")
    seen = json.loads(line.removeprefix("LOOPBACK ")) if line else {}
    evidence.observe("loopback_probe", seen)
    evidence.check(
        "worker_cannot_reach_127_0_0_1",
        seen.get("kernel", {}).get("connected") is False,
        kernel=seen.get("kernel"),
    )
    evidence.check(
        "worker_reaches_the_tool_endpoint",
        str(seen.get("endpoint", {}).get("status_line", "")).startswith("HTTP/1."),
        endpoint=seen.get("endpoint"),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run", help="run every phase and write the evidence")
    add_common_arguments(running, NAME)
    running.add_argument("--long-seconds", type=int, default=1200)
    running.add_argument("--kill-after", type=int, default=600)
    running.add_argument("--bundle-mib", type=int, default=9)
    running.add_argument("--oversize-mib", type=float, default=10.3)
    running.add_argument(
        "--kernel-port",
        type=int,
        default=None,
        help="a listener on 127.0.0.1 the worker must not reach, e.g. the daemon's port",
    )
    commands.add_parser("config", help="print the code.session configuration")
    registry = commands.add_parser(
        "registry", help="print the host registry with this test's alias added"
    )
    registry.add_argument("existing", type=Path, help="the host's registry (may be absent)")
    gates = commands.add_parser("gates", help="write this test's gate files (none)")
    gates.add_argument("directory", type=Path)
    serving = commands.add_parser("serve", help="(internal) the service child")
    serving.add_argument("--work", type=Path, required=True)
    serving.add_argument("--task", required=True)
    serving.add_argument("--cardea-lib", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "config":
        print(json.dumps(coding_config(FIXTURE), indent=2))
        return 0
    if args.command == "registry":
        added = repositories(FIXTURE)["repositories"]
        print(json.dumps(merged_registry(args.existing, added), indent=2))
        return 0
    if args.command == "gates":
        return 0
    if args.command == "serve":
        use_kernel(args.cardea_lib)
        return serve(args.work / "store", args.task)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
