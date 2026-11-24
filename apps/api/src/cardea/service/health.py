"""Liveness and readiness of one kernel (`GET /v0.2/livez`, `GET /v0.2/readyz`).

The two endpoints follow the Kubernetes API server's `/livez` and `/readyz` convention:
liveness says only that the process answers, readiness that it can do its work. Readiness
is a list of named checks, each bounded in time, so a slow dependency fails its check and
never hangs the request:

- `database`: a write transaction can be opened (`BEGIN IMMEDIATE`, a no-op update,
  `ROLLBACK`); nothing is committed or journaled;
- `workflow_engine`: this process launched DBOS and its system database answers;
- `host_helper`: with a confined host, the helper answers its `profiles` action, called
  afresh (the kernel's own copy is cached for a minute);
- `repository:<alias>`: each repository of the host registry (ADR-037) has a readable
  `source` mirror refreshed within the allowed age (newest of `FETCH_HEAD`, `packed-refs`
  and the loose refs);
- `supervision`: no task is `running` on a lease that expired more than the grace period
  ago, which is a member the kernel no longer drives;
- `group:<namespace>/<name>`: the group's supervisor is enabled; a disabled supervisor is
  a paused team, which `allow_paused` accepts.

Group names, task IDs and agent IDs are shown only for namespaces in which the caller
holds `tasks.inspect` (and the platform allows it); every other namespace still counts
towards `ready`, under one anonymous check per kind.
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from cardea.kernel.models import Task
from cardea.service import repositories
from cardea.service.database import Fault
from cardea.service.kernel import Kernel
from cardea.service.models import now
from cardea.service.policy import Policy

log = logging.getLogger(__name__)

# Bound of each check; they run concurrently, so a request takes at most about this long.
CHECK_SECONDS = 8.0
DEFAULT_MIRROR_AGE_SECONDS = 900
DEFAULT_LEASE_GRACE_SECONDS = 600
# How many running tasks, loose refs and listed IDs one check looks at or names.
MAX_RUNNING = 5000
MAX_REFS = 20000
MAX_NAMED = 20


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail[:500]}


class Bounded:
    """`probe` running in its own daemon thread, waited for until a shared deadline.

    A probe still running at the deadline is reported as failed and left to finish on its
    own; every probe's own I/O is bounded too (SQLite's busy timeout, the helper's call
    timeout), so such threads do not accumulate without limit."""

    def __init__(self, name: str, probe: Callable[[], list[Check]], seconds: float) -> None:
        self.name, self.probe, self.seconds = name, probe, seconds
        self.result: list[Check] = []
        self.failure: str | None = None
        self.thread = threading.Thread(target=self.run, name=f"cardea-readyz-{name}", daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            self.result = self.probe()
        except Fault as error:
            self.failure = error.code
        except Exception as error:
            log.warning("readiness check %s failed", self.name, exc_info=True)
            self.failure = type(error).__name__

    def wait(self, deadline: float) -> list[Check]:
        self.thread.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():
            return [Check(self.name, False, f"timed out after {self.seconds:g} s")]
        if self.failure is not None:
            return [Check(self.name, False, f"failed: {self.failure}")]
        return self.result or [Check(self.name, False, "no result")]


def database(kernel: Kernel) -> list[Check]:
    with kernel.database.engine.connect() as conn:
        transaction = conn.begin()  # BEGIN IMMEDIATE: takes the write lock
        try:
            conn.exec_driver_sql("UPDATE service_meta SET value=value WHERE name='runtime_version'")
        finally:
            transaction.rollback()
    return [Check("database", True, "write transaction opened and rolled back")]


def workflow_engine(kernel: Kernel) -> list[Check]:
    if not kernel.workflows_launched:
        return [Check("workflow_engine", False, "DBOS is not launched in this process")]
    kernel.client.check_connection()
    return [Check("workflow_engine", True, "DBOS launched; system database answers")]


def host_helper(kernel: Kernel) -> list[Check]:
    host = kernel.workers.host if kernel.workers is not None else kernel.host
    if host is None or not hasattr(host, "profiles"):
        return [Check("host_helper", True, "no confined host configured")]
    installed = host.profiles()
    profiles = installed.get("profiles") if isinstance(installed, dict) else None
    if not isinstance(profiles, dict):
        return [Check("host_helper", False, "the helper's profiles answer is malformed")]
    return [Check("host_helper", True, f"helper answered with {len(profiles)} profile(s)")]


def _newest_ref(source: Path) -> float:
    """The newest modification time of the mirror's fetch markers and refs."""
    newest = 0.0
    for marker in ("FETCH_HEAD", "packed-refs"):
        with suppress(FileNotFoundError):
            newest = max(newest, (source / marker).stat().st_mtime)
    seen = 0
    for root, _, files in os.walk(source / "refs"):
        for name in files:
            seen += 1
            if seen > MAX_REFS:
                return newest
            newest = max(newest, (Path(root) / name).stat().st_mtime)
    return newest


def mirrors(max_age_seconds: int, clock: Callable[[], float]) -> list[Check]:
    registry = repositories.load()
    if registry is None:
        return [Check("repositories", True, "no repository registry configured")]
    checks = []
    for alias, entry in sorted(registry.repositories.items()):
        name = f"repository:{alias}"
        source = Path(entry.source)
        try:
            if not (source / "HEAD").is_file() and not (source / ".git").exists():
                checks.append(Check(name, False, "source is not a readable git repository"))
                continue
            newest = _newest_ref(source)
        except OSError as error:
            checks.append(Check(name, False, f"source is not readable ({type(error).__name__})"))
            continue
        if newest == 0.0:
            checks.append(Check(name, False, "source has no refs"))
            continue
        age = max(0, int(clock() - newest))
        fresh = age <= max_age_seconds
        detail = f"refreshed {age} s ago" + ("" if fresh else f" (limit {max_age_seconds} s)")
        checks.append(Check(name, fresh, detail))
    return checks or [Check("repositories", True, "the registry lists no repository")]


class Visibility:
    """Which namespaces the caller may see names in: those it may inspect tasks in."""

    def __init__(self, conn: sa.Connection, principal: str) -> None:
        self.policy, self.principal = Policy(conn), principal
        self.known: dict[str, bool] = {}

    def __call__(self, namespace: str) -> bool:
        if namespace not in self.known:
            try:
                self.policy.require(self.principal, namespace, "tasks.inspect")
                self.policy.require("platform", namespace, "tasks.inspect")
                self.known[namespace] = True
            except Fault:
                self.known[namespace] = False
        return self.known[namespace]


@contextmanager
def _visibility(kernel: Kernel, principal: str) -> Iterator[tuple[sa.Connection, Visibility]]:
    with kernel.database.read() as conn:
        yield conn, Visibility(conn, principal)


def supervision(kernel: Kernel, principal: str, grace_seconds: int) -> list[Check]:
    at = now()
    limit = at - timedelta(seconds=grace_seconds)
    stuck: list[str] = []
    hidden = 0
    with _visibility(kernel, principal) as (conn, visible):
        rows = conn.exec_driver_sql(
            "SELECT t.snapshot,b.namespace,b.agent_id FROM tasks t "
            "LEFT JOIN task_bindings b ON b.task_id=t.task_id WHERE t.state='running' LIMIT ?",
            (MAX_RUNNING,),
        ).all()
        for snapshot, namespace, agent in rows:
            task = Task.model_validate_json(snapshot)
            if task.lease is None or task.lease.expires_at >= limit:
                continue
            if namespace is not None and visible(str(namespace)):
                expired = int((at - task.lease.expires_at).total_seconds())
                stuck.append(f"{task.task_id} (agent {agent or task.agent_id}, {expired} s)")
            else:
                hidden += 1
    if not stuck and not hidden:
        return [
            Check(
                "supervision",
                True,
                f"no running task's lease expired more than {grace_seconds} s ago",
            )
        ]
    parts = []
    if stuck:
        shown = ", ".join(stuck[:MAX_NAMED])
        more = f" and {len(stuck) - MAX_NAMED} more" if len(stuck) > MAX_NAMED else ""
        parts.append(f"running on an expired lease: {shown}{more}")
    if hidden:
        parts.append(f"{hidden} such task(s) in namespaces you cannot inspect")
    return [Check("supervision", False, "; ".join(parts))]


def supervisors(kernel: Kernel, principal: str, allow_paused: bool) -> list[Check]:
    checks: list[Check] = []
    hidden_paused = 0
    with _visibility(kernel, principal) as (conn, visible):
        rows = conn.exec_driver_sql(
            "SELECT g.namespace,g.name,g.supervisor_id,p.disabled FROM agent_groups g "
            "LEFT JOIN principals p ON p.principal_id=g.supervisor_id "
            "ORDER BY g.namespace,g.name"
        ).all()
        for namespace, name, supervisor, disabled in rows:
            paused = disabled is None or bool(disabled)
            if not visible(str(namespace)):
                hidden_paused += paused
                continue
            label = f"group:{namespace}/{name}"
            if not paused:
                checks.append(Check(label, True, f"supervisor {supervisor} enabled"))
            elif allow_paused:
                checks.append(Check(label, True, "paused (allowed)"))
            else:
                checks.append(Check(label, False, "paused"))
    if hidden_paused:
        detail = f"{hidden_paused} paused group(s) in namespaces you cannot inspect"
        checks.append(
            Check("groups:other", allow_paused, detail + (" (allowed)" if allow_paused else ""))
        )
    return checks or [Check("groups", True, "no agent group")]


def readiness(
    kernel: Kernel,
    principal: str,
    *,
    allow_paused: bool = False,
    mirror_max_age_seconds: int = DEFAULT_MIRROR_AGE_SECONDS,
    lease_grace_seconds: int = DEFAULT_LEASE_GRACE_SECONDS,
    seconds: float = CHECK_SECONDS,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Every check, concurrently, each within `seconds`; `ready` when all pass."""
    wall = clock or time.time
    probes: list[tuple[str, Callable[[], list[Check]]]] = [
        ("database", lambda: database(kernel)),
        ("workflow_engine", lambda: workflow_engine(kernel)),
        ("host_helper", lambda: host_helper(kernel)),
        ("repositories", lambda: mirrors(mirror_max_age_seconds, wall)),
        ("supervision", lambda: supervision(kernel, principal, lease_grace_seconds)),
        ("groups", lambda: supervisors(kernel, principal, allow_paused)),
    ]
    deadline = time.monotonic() + seconds
    running = [Bounded(name, probe, seconds) for name, probe in probes]
    checks = [check for probe in running for check in probe.wait(deadline)]
    return {
        "schema_version": "0.2",
        "ready": all(check.ok for check in checks),
        "checks": [check.document() for check in checks],
        "as_of": now().isoformat(),
    }
