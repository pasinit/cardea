"""`make try`: the real kernel daemon, crash and recovery, without SPIRE or Keycloak.

Uses the UNENFORCED development identity in devauth.py. Everything else (DBOS dispatch,
SQLite store, authority ceilings, receipts, verification) is the production code path.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from cardea.service.admin import initialize
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import Database
from cardea.service.kernel import provision
from cardea.service.models import (
    RIGHTS,
    Permission,
    Principal,
    Provisioning,
    ServiceConfig,
    Signal,
    TaskRequest,
    now,
)
from devauth import issue

HERE = Path(__file__).resolve().parent
DOMAIN = "try.invalid"
SUPERVISOR = f"spiffe://{DOMAIN}/supervisor/alice"
OUTSIDER = f"spiffe://{DOMAIN}/supervisor/mallory"


def wait_for(check: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


class Trial:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = ServiceConfig(
            data_dir=root / "store",
            spiffe_socket="unix:///nonexistent",  # unused: no Workload API in development
            trust_domain=DOMAIN,
            runtime_spiffe_id=f"spiffe://{DOMAIN}/runtime/kernel",
        )
        self.config_path = root / "config.json"
        self.config_path.write_text(self.config.model_dump_json())
        self.config_path.chmod(0o600)
        self.processes: list[subprocess.Popen[bytes]] = []
        initialize(self.config.data_dir)
        expiry = now() + timedelta(hours=1)
        database = Database(self.config.data_dir)
        try:
            provision(
                database,
                Provisioning(
                    principals=(
                        self.spiffe("runtime", self.config.runtime_spiffe_id),
                        self.spiffe("alice", SUPERVISOR),
                        self.spiffe("mallory", OUTSIDER),
                        Principal(principal_id="agent", kind="agent", runtime_id="runtime"),
                    ),
                    # Only alice may act in namespace "demo"; mallory holds no rights there.
                    permissions=tuple(
                        Permission(
                            principal_id="alice",
                            namespace="demo",
                            operation=right,
                            delegate=True,
                            expires_at=expiry,
                        )
                        for right in RIGHTS
                    ),
                    platform=tuple(
                        Permission(
                            principal_id="platform",
                            namespace="demo",
                            operation=right,
                            expires_at=expiry,
                        )
                        for right in RIGHTS
                    ),
                ),
            )
        finally:
            database.close()
        self.alice = self.client(SUPERVISOR)
        self.mallory = self.client(OUTSIDER)

    @staticmethod
    def spiffe(principal: str, subject: str) -> Principal:
        return Principal(
            principal_id=principal,
            kind="service",
            auth_kind="spiffe",
            issuer=DOMAIN,
            subject=subject,
        )

    def client(self, subject: str) -> KernelClient:
        return KernelClient(
            self.config.data_dir / "kernel.sock", lambda: ("spiffe", issue(self.config, subject))
        )

    def start(self) -> subprocess.Popen[bytes]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_"))}
        env["PYTHONPATH"] = os.pathsep.join([str(HERE), *sys.path])
        log = (self.root / f"daemon-{len(self.processes)}.log").open("wb")
        process = subprocess.Popen(
            [sys.executable, str(HERE / "daemon.py"), str(self.config_path)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)

        def ready() -> None:
            if process.poll() is not None:
                raise SystemExit(self.logs())
            self.alice.request("GET", "/system")

        wait_for(ready)
        return process

    def logs(self) -> str:
        return "\n".join(p.read_text()[-4000:] for p in sorted(self.root.glob("daemon-*.log")))

    def state(self, task_id: str, expected: str) -> None:
        def reached() -> None:
            assert self.alice.inspect(task_id).task.state.value == expected

        wait_for(reached)

    def close(self) -> None:
        self.alice.close()
        self.mallory.close()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def run(say: Callable[[str], None] = print) -> dict[str, Any]:
    # macOS limits Unix socket paths to 104 bytes; keep the store under /tmp.
    with tempfile.TemporaryDirectory(prefix="cardea-try-", dir="/tmp") as temp:
        trial = Trial(Path(temp))
        try:
            started = time.monotonic()
            first = trial.start()
            say("1. Started the kernel daemon (UNENFORCED development identity).")
            task_id = trial.alice.submit(
                TaskRequest(
                    request_id="try-1",
                    agent_id="agent",
                    namespace="demo",
                    objective="Publish and verify a durable JSON artifact",
                    payload={"message": "Hello from Cardea"},
                    wait_for_event=True,
                )
            ).task.task_id
            trial.state(task_id, "waiting")
            receipt = trial.alice.request("GET", f"/tasks/{task_id}/receipt")
            say(f"2. alice submitted {task_id}; it checkpointed and is waiting for a signal.")
            try:
                trial.mallory.inspect(task_id)
                raise SystemExit("authority ceiling failed: mallory could inspect the task")
            except ClientError as denied:
                say(f"3. mallory (no rights in 'demo') was refused: {denied.code}.")
            first.send_signal(signal.SIGKILL)
            first.wait(timeout=10)
            say("4. Killed the daemon with SIGKILL mid-task.")
            trial.start()
            say("5. Restarted it from the same store.")
            trial.alice.control(task_id, Signal(request_id="resume-1"))
            trial.state(task_id, "succeeded")
            after = trial.alice.request("GET", f"/tasks/{task_id}/receipt")
            assert after == receipt, "acknowledged receipt changed across the crash"
            output = trial.alice.request("GET", f"/tasks/{task_id}/output")
            verified = trial.alice.inspect(task_id).task.verification
            assert verified is not None and verified.passed
            say(
                "6. alice resumed it; it finished and its artifact passed independent verification."
            )
            result = {
                "task_id": task_id,
                "state": "succeeded",
                "receipt_preserved_across_crash": True,
                "outsider_denied": True,
                "output": output["content"],
                "seconds": round(time.monotonic() - started, 1),
            }
            say(json.dumps(result, indent=2))
            return result
        finally:
            trial.close()


if __name__ == "__main__":
    run()
