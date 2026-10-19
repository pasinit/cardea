"""Real SPIRE and Cardea services with private disposable state and bounded cleanup."""

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from provider_lab import Lab

from cardea.service.admin import initialize
from cardea.service.client import KernelClient
from cardea.service.database import Database
from cardea.service.kernel import provision
from cardea.service.models import RIGHTS, Principal, Provisioning, Permission, ServiceConfig, now

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = "spiffe://cardea.test/supervisor"
OUTSIDER = "spiffe://cardea.test/outsider"


def wait_for[T](check: Callable[[], T], timeout: float = 25) -> T:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class ServiceLab:
    def __init__(self, provider: Lab, **options: Any) -> None:
        self.provider = provider
        self.temp = tempfile.TemporaryDirectory(prefix="cm1-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.data = self.root / "store"
        self.processes: list[subprocess.Popen[bytes]] = []
        self.config = ServiceConfig(data_dir=self.data, spiffe_socket=provider.socket,
            trust_domain="cardea.test", audience="cardea-kernel-fit",
            runtime_spiffe_id="spiffe://cardea.test/runtime/a", **options)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(self.config.model_dump_json())
        self.config_path.chmod(0o600)
        initialize(self.data)
        self.document = Provisioning(
            principals=(
                Principal(principal_id="runtime-a", kind="service", auth_kind="spiffe",
                          issuer="cardea.test", subject=self.config.runtime_spiffe_id),
                Principal(principal_id="supervisor", kind="service", auth_kind="spiffe",
                          issuer="cardea.test", subject=SUPERVISOR),
                Principal(principal_id="outsider", kind="service", auth_kind="spiffe",
                          issuer="cardea.test", subject=OUTSIDER),
                Principal(principal_id="agent-a", kind="agent", runtime_id="runtime-a"),
            ),
            permissions=tuple(Permission(principal_id=principal, namespace="test", operation=op,
                delegate=True, expires_at=now() + timedelta(hours=2))
                for principal in ("supervisor", "outsider") for op in RIGHTS),
            platform=tuple(Permission(principal_id="platform", namespace="test", operation=op,
                expires_at=now() + timedelta(hours=2)) for op in RIGHTS),
        )
        self.apply(self.document)
        self.client = KernelClient.for_workload(self.data / "kernel.sock", provider.socket,
                                                SUPERVISOR, self.config.audience)

    def apply(self, document: Provisioning) -> None:
        database = Database(self.data)
        try:
            provision(database, document)
        finally:
            database.close()

    def start(self, barrier: str = "") -> subprocess.Popen[bytes]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_", "M1_"))}
        env.update(PYTHONPATH=str(ROOT / "apps/api/src"), M1_CONFIG=str(self.config_path), M1_BARRIER=barrier)
        with (self.root / f"daemon-{len(self.processes)}.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "experiments/m1/daemon_probe.py")],
                cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(process)
        def ready() -> None:
            if process.poll() is not None:
                raise AssertionError(self.logs())
            assert self.client.request("GET", "/system")["schema_version"] == "0.2"
        try:
            wait_for(ready)
        except Exception:
            raise AssertionError(self.logs()) from None
        return process

    def logs(self) -> str:
        return "\n".join(p.read_text()[-6000:] for p in self.root.glob("daemon-*.log"))

    def wait_state(self, task_id: str, expected: str, timeout: float = 20) -> None:
        def reached() -> None:
            state = self.client.inspect(task_id).task.state.value
            assert state == expected, (state, self.logs())
        wait_for(reached, timeout)

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def close(self) -> None:
        self.client.close()
        for process in self.processes:
            self.stop(process)
        self.temp.cleanup()
