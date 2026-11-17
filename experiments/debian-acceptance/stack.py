"""One throwaway kernel daemon for an acceptance script: its store, policy and clients.

The daemon is `launcher.py` (the installed kernel's `serve()` with the development
identity and the scripted harness), started on the script's own store and killed or
stopped only through the PIDs this object recorded.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

from common import DOMAIN, HERE, REFERENCE_OPS, TRY, Processes, wait_for

TERMINAL = {"succeeded", "failed", "cancelled"}
# Reading a task (GET /tasks/{id}, its receipt and output) is checked against the task's own
# grants, like every other right: a supervisor can inspect only a task granted
# `tasks.inspect`. The scripts read every task they start, so each one carries it.
INSPECT = "tasks.inspect"


def inspectable(grants: list[str]) -> list[str]:
    """`grants` with `tasks.inspect`, once."""
    return [*grants, INSPECT] if INSPECT not in grants else list(grants)


def spiffe(name: str) -> str:
    return f"spiffe://{DOMAIN}/{name}"


class Stack:
    def __init__(
        self,
        work: Path,
        processes: Processes,
        kernel_lib: Path,
        *,
        reference_ops: bool = False,
        **config: Any,
    ) -> None:
        sys.path[:0] = [str(TRY), *([str(REFERENCE_OPS)] if reference_ops else [])]
        from cardea.service.admin import initialize
        from cardea.service.models import ServiceConfig

        self.work = work
        self.processes = processes
        self.kernel_lib = kernel_lib
        self.reference_ops = reference_ops
        self.store = work / "store"
        self.config = ServiceConfig.model_validate(
            {
                "data_dir": str(self.store),
                "spiffe_socket": "unix:///nonexistent",
                "trust_domain": DOMAIN,
                "runtime_spiffe_id": spiffe("runtime"),
                **config,
            }
        )
        self.config_path = work / "config.json"
        self.config_path.write_text(self.config.model_dump_json(exclude_none=True))
        self.config_path.chmod(0o600)
        initialize(self.store)
        self.daemon: subprocess.Popen[bytes] | None = None
        self.starts = 0
        self.ids = count(1)

    @property
    def database(self) -> Path:
        return self.store / "kernel.sqlite3"

    def registry(self) -> Any:
        from cardea.service.operations import Registry

        # Provisioning needs the descriptors only, as `cardea provision` loads them: no
        # handlers (`None`). An empty mapping would mean "these handlers, and no others",
        # which every reference entry point fails (operation_handler_missing).
        if self.reference_ops:
            from cardea_reference_ops import descriptors

            return Registry(descriptors(), None)
        return Registry((), None)

    def provision(self, document: dict[str, Any]) -> None:
        """What `cardea provision` does, before the daemon starts or while it runs."""
        from cardea.service.database import Database
        from cardea.service.kernel import provision
        from cardea.service.models import Provisioning

        database = Database(self.store)
        try:
            provision(database, Provisioning.model_validate(document), self.registry())
        finally:
            database.close()

    # The daemon

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DBOS_", "OTEL_"))}
        paths = [str(HERE), str(TRY), str(self.kernel_lib)]
        if self.reference_ops:
            paths.append(str(REFERENCE_OPS))
        env["PYTHONPATH"] = os.pathsep.join(paths)
        argv = [sys.executable, str(HERE / "launcher.py"), str(self.config_path), str(self.work)]
        if self.reference_ops:
            argv.append("--reference-ops")
        self.starts += 1
        self.daemon = self.processes.start(argv, self.work / f"daemon-{self.starts}.log", env)
        daemon = self.daemon

        def ready() -> bool:
            if daemon.poll() is not None:
                raise SystemExit(f"the daemon exited: see {self.work}/daemon-{self.starts}.log")
            self.request("runtime", "GET", "/system")
            return True

        wait_for(ready, 120)

    def kill(self) -> None:
        """SIGKILL the daemon this script started: a crash, not a shutdown."""
        assert self.daemon is not None
        Processes.kill(self.daemon)
        self.daemon = None

    def stop(self) -> None:
        if self.daemon is not None:
            Processes.stop(self.daemon)
            self.daemon = None

    def restart_after_kill(self) -> None:
        self.kill()
        self.start()

    # Requests

    def token(self, principal: str) -> str:
        from devauth import issue

        # The development identity refuses a configuration with an `oidc` block; it signs
        # SPIFFE tokens only. The launcher hands it the same copy (launcher.py).
        return str(issue(self.config.model_copy(update={"oidc": None}), spiffe(principal)))

    def request(
        self,
        principal: str,
        method: str,
        path: str,
        *,
        credential: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """A request as `principal` (a development SPIFFE token), or with `credential`,
        a `(kind, token)` pair such as `("oidc", token)` from a test issuer."""
        from cardea.service.client import KernelClient

        def credentials() -> tuple[str, str]:
            return credential or ("spiffe", self.token(principal))

        client = KernelClient(self.store / "kernel.sock", credentials)
        try:
            return client.request(method, path, **kwargs)
        finally:
            client.http.close()

    def attempt(self, principal: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """A request whose refusal is an expected outcome: returns the code, not a raise."""
        from cardea.service.client import ClientError

        try:
            return {"ok": True, "body": self.request(principal, method, path, **kwargs)}
        except ClientError as error:
            return {"ok": False, "code": error.code, "status": error.status}

    def operation(
        self,
        supervisor: str,
        agent: str,
        namespace: str,
        operation: str,
        arguments: dict[str, Any],
        grants: list[str],
    ) -> str:
        """Submit one operation task; returns its task ID."""
        body = {
            "request_id": f"acc-{int(time.time())}-{next(self.ids)}",
            "agent_id": agent,
            "namespace": namespace,
            "objective": f"Acceptance: {operation}",
            "operation": operation,
            "arguments": arguments,
            "grants": inspectable(["objects.read", "objects.write", *grants]),
        }
        view = self.request(supervisor, "POST", "/operation-tasks", json=body)
        return str(view["task"]["task_id"])

    def agent_run(self, supervisor: str, agent: str, namespace: str, **fields: Any) -> str:
        body = {
            "request_id": f"acc-run-{int(time.time())}-{next(self.ids)}",
            "agent_id": agent,
            "namespace": namespace,
            "objective": "Acceptance run",
            "instructions": "Play the acceptance plan.",
            "max_attempts": 1,
            "attempt_timeout_seconds": 120,
            **fields,
        }
        body["grants"] = inspectable(list(body.get("grants", [])))
        return str(self.request(supervisor, "POST", "/agent-runs", json=body)["task"]["task_id"])

    def task(self, principal: str, task_id: str) -> dict[str, Any]:
        return dict(self.request(principal, "GET", f"/tasks/{task_id}"))

    def finished(self, principal: str, task_id: str, timeout: float = 300) -> dict[str, Any]:
        def done() -> dict[str, Any] | None:
            view = self.task(principal, task_id)
            return view if view["task"]["state"] in TERMINAL else None

        return wait_for(done, timeout, 1.0)

    def receipt(self, principal: str, task_id: str) -> dict[str, Any]:
        """The receipt that stands for the task: `state`, `result`, `failure`."""
        return dict(self.request(principal, "GET", f"/tasks/{task_id}/receipt")["receipt"])

    def output(self, principal: str, task_id: str) -> Any:
        """An operation task's output, or None."""
        return (self.receipt(principal, task_id).get("result") or {}).get("output")

    def failure(self, principal: str, task_id: str) -> str | None:
        failure = self.receipt(principal, task_id).get("failure") or {}
        return failure.get("code")


def people(*names: str, kind: str = "service") -> list[dict[str, Any]]:
    """Principals with a development SPIFFE identity (kind `service` or `human`)."""
    return [
        {
            "principal_id": name,
            "kind": kind,
            "auth_kind": "spiffe",
            "issuer": DOMAIN,
            "subject": spiffe(name),
        }
        for name in names
    ]


def agents(*names: str) -> list[dict[str, Any]]:
    return [{"principal_id": n, "kind": "agent", "runtime_id": "runtime"} for n in names]


def rights(principal: str, namespace: str, grants: list[str], **modes: bool) -> list[Any]:
    """Permission rows for a principal, and the matching `platform` rows."""
    expires = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    rows: list[Any] = []
    delegate = modes.pop("delegate", modes.get("allow", True))
    for grant in sorted(set(grants)):
        row = {"namespace": namespace, "operation": grant, "expires_at": expires}
        rows.append({"principal_id": principal, "delegate": delegate, **row, **modes})
    return rows


def platform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["namespace"], row["operation"])
        entry = seen.setdefault(
            key,
            {
                "principal_id": "platform",
                "namespace": row["namespace"],
                "operation": row["operation"],
                "expires_at": row["expires_at"],
            },
        )
        if row.get("approve"):
            entry["approve"] = True
    return list(seen.values())


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
