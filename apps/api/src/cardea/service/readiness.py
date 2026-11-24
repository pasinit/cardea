"""`/livez` and `/readyz`: whether the kernel process answers, and whether a team can work.

Liveness is the process answering. Readiness is a list of checks, each `ok` or `fail` with a
short reason; the endpoint is 503 when any fails. A reason names what is wrong, never a path,
a token or a host answer. Each check reads, or sends a request the other side rejects as
invalid; none changes anything a team depends on.

The kernel never holds a repository token (ADR-037): the token check runs `vcs.check_token`
in the `vcs` confined worker, as a reconciler, for one member of each pool that may push or
review. Its answers are kept for `TOKEN_CACHE_SECONDS`, so probes do not call the host often.
"""

import asyncio
import logging
import socket
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from dbos import DBOS

from cardea.kernel.models import Task
from cardea.service import repositories
from cardea.service.confined import job_document, job_id
from cardea.service.kernel import Kernel
from cardea.service.models import GroupSpec, now
from cardea.service.operations import OperationContext, OperationRejected

log = logging.getLogger(__name__)

MIRROR_MAX_AGE_SECONDS = 600
TOKEN_CACHE_SECONDS = 60
TOKEN_CALL_SECONDS = 30
PROXY_TIMEOUT_SECONDS = 2.0
# What each role needs of its token, by the operation that role's pools are granted.
ROLES = {"vcs.push_branch": "pusher", "vcs.review": "reviewer"}
Check = tuple[bool, str]


def executor_running() -> None:
    """Raises unless the DBOS executor answers: it is launched and reads its system tables."""
    DBOS.list_workflows(limit=1)


class Readiness:
    def __init__(
        self,
        kernel: Kernel,
        *,
        clock: Callable[[], float] = time.time,
        executor: Callable[[], None] | None = None,
        connect: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self.kernel = kernel
        self.clock, self.executor, self.connect = clock, executor or executor_running, connect
        self._tokens: tuple[float, list[Check]] | None = None

    def report(self) -> dict[str, Any]:
        checks = []
        for name, check in (
            ("database", self.database),
            ("dbos_executor", self.dbos),
            ("host_helper", self.helper),
            ("egress_proxy", self.proxies),
            ("repository_mirrors", self.mirrors),
            ("vcs_tokens", self.tokens),
            ("supervisor_and_triggers", self.triggers),
            ("member_leases", self.leases),
        ):
            try:
                ok, reason = check()
            except Exception as error:
                log.warning("readiness check failed", exc_info=True, extra={"code": name})
                ok, reason = False, f"the check itself failed ({type(error).__name__})"
            checks.append({"name": name, "status": "ok" if ok else "fail", "reason": reason})
        return {
            "status": "ok" if all(c["status"] == "ok" for c in checks) else "fail",
            "checks": checks,
        }

    def database(self) -> Check:
        # Takes the write lock and changes nothing.
        with self.kernel.database.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE service_meta SET value=value WHERE name='policy_revision'")
        return True, "the store accepts a write"

    def dbos(self) -> Check:
        self.executor()
        return True, "the DBOS executor answers"

    def _profiles(self) -> dict[str, Any] | None:
        if self.kernel.workers is None:
            return None
        # The helper validates the workers file before it answers; a refusal is a fault.
        return self.kernel.workers.host.profiles()

    def helper(self) -> Check:
        if self._profiles() is None:
            return True, "host execution is not configured"
        return True, "the helper answers and accepts the workers file"

    def proxies(self) -> Check:
        installed = self._profiles()
        if installed is None:
            return True, "host execution is not configured"
        addresses = sorted(
            {
                str(p["network"]["proxy"])
                for p in installed.get("profiles", {}).values()
                if "proxy" in p.get("network", {})
            }
        )
        down = []
        for address in addresses:
            host, _, port = address.rpartition(":")
            try:
                self.connect((host, int(port)), timeout=PROXY_TIMEOUT_SECONDS).close()
            except OSError:
                down.append(address)
        if down:
            return False, f"no answer from the egress proxy at {', '.join(down)}"
        return True, f"{len(addresses)} egress prox{'y' if len(addresses) == 1 else 'ies'} answer"

    def mirrors(self) -> Check:
        registry = repositories.load()
        if registry is None:
            return True, "no repository registry is configured"
        stale = []
        for alias, entry in sorted(registry.repositories.items()):
            # A fetch rewrites FETCH_HEAD even when nothing changed; a fresh clone has HEAD.
            times = [
                (Path(entry.source) / name).stat().st_mtime
                for name in ("FETCH_HEAD", "HEAD")
                if (Path(entry.source) / name).exists()
            ]
            if not times:
                stale.append(f"{alias} (no mirror)")
            elif self.clock() - max(times) > MIRROR_MAX_AGE_SECONDS:
                stale.append(f"{alias} (older than {MIRROR_MAX_AGE_SECONDS // 60} min)")
        if stale:
            return False, f"not fresh: {', '.join(stale)}"
        return True, f"{len(registry.repositories)} mirror(s) fetched in the last 10 min"

    def _holders(self) -> list[tuple[str, str, str, str, str]]:
        """Operation, role, group, pool and one active member for each pool granted a role."""
        with self.kernel.database.read() as conn:
            groups = conn.exec_driver_sql(
                "SELECT namespace,name,spec_json FROM agent_groups ORDER BY namespace,name"
            ).all()
            members = {
                (str(g), str(p)): str(a)
                for g, p, a in conn.exec_driver_sql(
                    "SELECT group_name,pool,MIN(principal_id) FROM group_members "
                    "WHERE state='active' GROUP BY namespace,group_name,pool"
                )
            }
        found = []
        for _, name, document in groups:
            for pool in GroupSpec.model_validate_json(document).pools:
                agent = members.get((str(name), pool.name))
                for operation, role in ROLES.items():
                    if agent is not None and operation in pool.grants:
                        found.append((operation, role, str(name), pool.name, agent))
        return found

    def _probe(self, operation: str, role: str, group: str, agent: str, alias: str) -> Check:
        workers = self.kernel.workers
        descriptor = self.kernel.operations.descriptors.get(operation)
        executor = descriptor.cardea.executor if descriptor else None
        if workers is None or executor is None:
            return False, f"{operation} does not run in a confined worker"
        stamp = uuid.uuid4().hex
        context = OperationContext(
            task_id=f"readyz-{stamp}",
            namespace="readyz",
            receipt_id=f"readyz-{stamp}",
            deadline=now() + timedelta(seconds=TOKEN_CALL_SECONDS),
            effect_key=f"readyz-{stamp}",
            agent_id=agent,
            group=group,
        )
        document = job_document({"repository": alias, "role": role}, context, "vcs.check_token")
        try:
            # A reconcile job runs only a listed entry: a worker that lacks it refuses.
            output = asyncio.run(
                workers.run(
                    job_id("readyz", f"{operation}:{alias}:{agent}:{stamp}"),
                    {**document, "reconcile": True},
                    profile=executor.profile,
                    worker=executor.worker,
                    deadline=time.time() + TOKEN_CALL_SECONDS,
                    timeout_seconds=TOKEN_CALL_SECONDS,
                )
            )
        except OperationRejected as rejected:
            return False, str(rejected)[:200]
        answer = output.get("output")
        reason = str(answer.get("reason", ""))[:200] if isinstance(answer, dict) else ""
        return output.get("applied") is True, reason or "no reason given"

    def tokens(self) -> Check:
        if self._tokens is not None and self.clock() - self._tokens[0] < TOKEN_CACHE_SECONDS:
            return _summary(self._tokens[1], "vcs token")
        registry = repositories.load()
        entries = registry.repositories if registry else {}
        hosted = sorted(alias for alias, entry in entries.items() if entry.hosted)
        results: list[Check] = []
        if self.kernel.workers is not None:
            for operation, role, group, pool, agent in self._holders():
                for alias in hosted:
                    ok, reason = self._probe(operation, role, group, agent, alias)
                    results.append((ok, f"{group}.{pool} {role} on {alias}: {reason}"))
        self._tokens = (self.clock(), results)
        return _summary(results, "vcs token")

    def triggers(self) -> Check:
        problems = []
        with self.kernel.database.read() as conn:
            rows = conn.exec_driver_sql(
                "SELECT t.trigger_id,p.disabled,(SELECT e.kind||':'||COALESCE(e.code,'') "
                "FROM trigger_events e WHERE e.trigger_id=t.trigger_id AND e.kind IN "
                "('admitted','failed','coalesced') ORDER BY e.event_id DESC LIMIT 1) "
                "FROM triggers t LEFT JOIN principals p ON p.principal_id=t.supervisor_id "
                "WHERE t.enabled=1 AND t.removed=0 ORDER BY t.trigger_id"
            ).all()
        for trigger_id, disabled, last in rows:
            if disabled is None or disabled:
                problems.append(f"{trigger_id}: its supervisor is missing or disabled")
            elif str(last).startswith("failed"):
                problems.append(f"{trigger_id}: its last slot was refused ({str(last)[7:]})")
        if problems:
            return False, "; ".join(problems)
        return True, f"{len(rows)} enabled trigger(s) have an enabled supervisor and admit"

    def leases(self) -> Check:
        at = now()
        with self.kernel.database.read() as conn:
            rows = conn.exec_driver_sql(
                "SELECT t.snapshot FROM tasks t JOIN task_bindings b USING(task_id) "
                "JOIN group_members m ON m.principal_id=b.agent_id WHERE t.state='running' "
                "ORDER BY t.task_id"
            ).all()
        late = [
            task.task_id
            for task in (Task.model_validate_json(row[0]) for row in rows)
            if task.lease is not None and at >= task.lease.expires_at
        ]
        if late:
            return False, f"running past its lease: {', '.join(late)}"
        return True, f"{len(rows)} member run(s) hold a live lease"


def _summary(results: list[Check], what: str) -> Check:
    failed = [reason for ok, reason in results if not ok]
    if failed:
        return False, "; ".join(failed)
    return True, f"{len(results)} {what} check(s) passed"
