"""Confined execution of installed operations and agent sessions (ADR-028).

A confined worker is a separate process that the root helper starts under a host-owner
profile (systemd transient unit + bubblewrap). Its arguments arrive on stdin as JSON and its
result leaves on stdout as JSON. The kernel keeps intent, receipt and settlement exactly as
for in-process operations; this module only maps a worker's life onto handler outcomes:

- the helper refused before its no-relaunch fence: ``OperationRejected`` (nothing started);
- the worker finished with ``{"output": ...}``: the handler's result, checked as usual;
- the worker reported ``{"rejected": ...}``: ``OperationRejected`` from trusted worker code;
- anything else once started (memory cap, wall time, crash, unknown start): ``WorkerFailed``;
- cancellation or lease loss while running: ``WorkerStopped`` after stop and cleanup.

A profile may limit how many of its workers run at once. A call that cannot get a slot
before its deadline is refused before anything starts.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from pydantic import JsonValue

from cardea.hosts.debian import WorkerHost
from cardea.runtime import activity as activities
from cardea.runtime.harness import HarnessRequest, HarnessResult
from cardea.service.database import Fault
from cardea.service.operations import (
    ConfinedExecutor,
    Handler,
    OperationContext,
    OperationRejected,
)

log = logging.getLogger(__name__)
POLL_SECONDS = 0.25
# Queue wait for a profile slot when the helper reports none (older helpers).
DEFAULT_QUEUE_TIMEOUT_SECONDS = 600
# systemd unit results that identify an enforced limit.
LIMIT_RESULTS = {"oom-kill": "worker_memory_limit", "timeout": "worker_timeout"}
Watch = Callable[[], str | None]
# Whether whoever asked for the call has given up on it (an agent's MCP client disconnected
# or sent a cancellation). Set by the caller's context; checked only while the call waits
# for a slot, so a worker that already started always finishes and settles.
abandoned: ContextVar[Callable[[], bool] | None] = ContextVar("cardea_abandoned", default=None)


class WorkerFailed(Exception):
    """The worker started, or may have started, and produced no result.

    ``reason`` and ``summary`` are bounded, sanitized descriptions for receipts and agents.
    """

    def __init__(self, code: str, reason: str = "worker_exit", summary: str = "") -> None:
        super().__init__(code)
        self.code, self.reason, self.summary = code, reason, summary or code


class WorkerNotStarted(OperationRejected):
    """The helper refused the job, or it was stopped before it started: nothing ran."""


class CallAbandoned(WorkerNotStarted):
    """The caller gave up while the call waited for a slot; it was withdrawn and nothing ran."""


class ExecutorBusy(OperationRejected):
    """No slot of the worker profile freed up within its queue timeout: nothing ran."""


class WorkerStopped(WorkerFailed):
    """Cardea stopped a running worker because of cancellation or a lost lease."""

    def __init__(self, code: str) -> None:
        super().__init__(code, "worker_exit", f"stopped by Cardea: {code}")


def job_id(kind: str, key: str) -> str:
    """Deterministic helper job: recovery can find it, and the helper refuses a replay."""
    return hashlib.sha256(f"cardea-{kind}:{key}".encode()).hexdigest()[:32]


def _word(value: object) -> str:
    """A systemd property value reduced to a short safe token."""
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(value))[:32] or "unknown"


def _result(status: dict[str, Any]) -> dict[str, Any]:
    result = status.get("result")
    if result in LIMIT_RESULTS:
        code = LIMIT_RESULTS[str(result)]
        raise WorkerFailed(
            code,
            "timeout" if code == "worker_timeout" else "worker_exit",
            f"stopped by its profile limit (systemd result {_word(result)})",
        )
    if result != "success" or str(status.get("exit_status")) != "0":
        raise WorkerFailed(
            "worker_failed",
            "worker_exit",
            f"exit status {_word(status.get('exit_status'))}, systemd result {_word(result)}",
        )
    try:
        document = json.loads(base64.b64decode(str(status.get("output", "")), validate=True))
    except (ValueError, binascii.Error):
        raise WorkerFailed(
            "worker_output_invalid", "invalid_output", "output is not JSON"
        ) from None
    if isinstance(document, dict) and set(document) == {"rejected"}:
        raise OperationRejected(str(document["rejected"])[:256])
    if isinstance(document, dict) and set(document) == {"error"}:
        # The worker caught an exception and described it (operation_worker): no stderr.
        error = document["error"] if isinstance(document["error"], dict) else {}
        kind = error.get("type")
        kind = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_][\w.]{0,63}", kind) else ""
        http = error.get("status")
        if type(http) is int and 100 <= http <= 599:
            raise WorkerFailed("worker_error", "upstream_http_error", f"{kind} (HTTP {http})")
        raise WorkerFailed("worker_error", "error", kind or "handler error")
    if (
        isinstance(document, dict)
        and set(document) == {"output"}
        and isinstance(document["output"], dict)
    ):
        return document["output"]
    raise WorkerFailed("worker_output_invalid")


class Workers:
    """Kernel-side view of the helper's installed profiles and confined jobs."""

    def __init__(self, host: WorkerHost) -> None:
        self.host = host
        self._installed: dict[str, Any] | None = None
        # Per-profile slots. Threading, not asyncio: each attempt runs its own event loop.
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        self._slots_lock = threading.Lock()

    def installed(self) -> dict[str, Any]:
        # Profiles are root-owned host configuration; changing them requires a restart.
        if self._installed is None:
            try:
                self._installed = self.host.profiles()
            except Fault:
                log.warning("worker profiles unavailable", exc_info=True)
                return {"profiles": {}, "workers": {}}
        return self._installed

    def require(self, profile: str, worker: str) -> dict[str, JsonValue]:
        """The intent's executor record, or a refusal when the host lacks the profile."""
        installed = self.installed()
        entry = installed.get("profiles", {}).get(profile)
        if entry is None or profile not in installed.get("workers", {}).get(worker, ()):
            raise Fault(
                "worker_profile_not_installed",
                "The host owner has not installed this worker profile",
                409,
            )
        return {"profile": profile, "worker": worker, "profile_sha256": str(entry.get("sha256"))}

    def _slot(self, profile: str) -> threading.BoundedSemaphore | None:
        limit = self.installed().get("profiles", {}).get(profile, {}).get("concurrency")
        if not isinstance(limit, int) or limit < 1:
            return None
        with self._slots_lock:
            return self._slots.setdefault(profile, threading.BoundedSemaphore(limit))

    async def _acquire(
        self, slot: threading.BoundedSemaphore, deadline: float, watch: Watch | None
    ) -> None:
        """Wait for a free slot without blocking the loop; nothing has started yet."""
        gone = abandoned.get()
        while not slot.acquire(blocking=False):
            if gone is not None and gone():
                raise CallAbandoned("the caller gave up while it waited for a slot; nothing ran")
            if time.time() >= deadline:
                raise ExecutorBusy("no free slot in this worker profile; nothing ran, retry later")
            reason = await asyncio.to_thread(watch) if watch is not None else None
            if reason:
                raise WorkerNotStarted(f"stopped before it started: {reason}")
            await asyncio.sleep(POLL_SECONDS)

    def queue_timeout(self, profile: str) -> float:
        value = self.installed().get("profiles", {}).get(profile, {}).get("queue_timeout_seconds")
        return (
            float(value) if isinstance(value, int) and value > 0 else DEFAULT_QUEUE_TIMEOUT_SECONDS
        )

    def queue_allowance(self, profile: str) -> float:
        """How long a call may wait for a slot of this profile; 0 when it never waits."""
        return self.queue_timeout(profile) if self._slot(profile) is not None else 0.0

    def reap(self, job: str) -> None:
        """Stop the whole unit, then retire and delete its staging. Safe to repeat."""
        try:
            self.host.stop(job)
        except Fault:
            log.warning("confined worker stop failed", exc_info=True, extra={"job": job})
            return  # Cleanup refuses an unconfirmed stop; a later reap retries.
        try:
            self.host.cleanup(job)
        except Fault:
            log.warning("confined worker cleanup failed", exc_info=True, extra={"job": job})

    async def run(
        self,
        job: str,
        payload: dict[str, Any],
        *,
        profile: str,
        worker: str,
        deadline: float,
        watch: Watch | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Wait for a profile slot (bounded by the profile's queue timeout), then run.

        ``deadline`` is the absolute limit; ``timeout_seconds`` bounds execution only and
        starts once the worker has a slot, so queueing never eats an operation's time.
        """
        slot = self._slot(profile)
        if slot is not None:
            await self._acquire(
                slot, min(deadline, time.time() + self.queue_timeout(profile)), watch
            )
        try:
            if timeout_seconds is not None:
                deadline = min(deadline, time.time() + timeout_seconds)
            return await self._run(job, payload, profile, worker, deadline, watch)
        finally:
            if slot is not None:
                slot.release()

    async def _run(
        self,
        job: str,
        payload: dict[str, Any],
        profile: str,
        worker: str,
        deadline: float,
        watch: Watch | None,
    ) -> dict[str, Any]:
        content = json.dumps(payload, separators=(",", ":")).encode()
        try:
            await asyncio.to_thread(
                self.host.start_confined, job, content, deadline, profile, worker
            )
        except Fault as error:
            if error.code == "host_rejected":
                raise WorkerNotStarted(f"helper refused the job: {error.message}") from None
            # The helper may have passed its fence before failing: treat as started.
            await asyncio.to_thread(self.reap, job)
            raise WorkerFailed("worker_start_unknown", summary="helper start failed") from None
        try:
            while True:
                try:
                    status = await asyncio.to_thread(self.host.status, job)
                except Fault:
                    raise WorkerFailed(
                        "worker_status_unknown", summary="worker status unavailable"
                    ) from None
                if status.get("state") == "finished":
                    break
                reason = await asyncio.to_thread(watch) if watch is not None else None
                if reason:
                    raise WorkerStopped(reason)
                await asyncio.sleep(POLL_SECONDS)
        except BaseException:
            # Cancellation, timeout, lease loss or an unknown state: stop the process tree.
            await asyncio.to_thread(self.reap, job)
            raise
        await asyncio.to_thread(self.reap, job)
        return _result(status)


def confined_handler(
    workers: Workers,
    executor: ConfinedExecutor,
    timeout_seconds: int,
    watch: Callable[[str, str], str | None],
) -> Handler:
    """A handler with the in-process signature whose code runs in a confined worker."""

    async def run(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
        payload = {
            "arguments": arguments,
            "context": {
                "task_id": context.task_id,
                "namespace": context.namespace,
                "receipt_id": context.receipt_id,
                "deadline": context.deadline.isoformat(),
                "effect_key": context.effect_key,
                "agent_id": context.agent_id,
                "idempotency_context": context.idempotency_context,
                "slot_time": context.slot_time,
            },
        }
        return await workers.run(
            job_id("operation", context.receipt_id),
            payload,
            profile=executor.profile,
            worker=executor.worker,
            deadline=context.deadline.timestamp(),
            timeout_seconds=timeout_seconds,
            watch=lambda: watch(context.task_id, context.lease_id),
        )

    return run


class ConfinedAgentSession:
    """Runs one agent session inside a confined worker (ADR-028).

    The worker holds the session's only credential (loaded by systemd from the profile),
    reaches the model API through the profile's proxy and Cardea's loopback tool endpoint,
    and sees neither the Cardea store nor the service's configuration.
    """

    def __init__(
        self,
        workers: Workers,
        profile: str,
        worker: str,
        model: str | None,
        *,
        settings: dict[str, Any] | None = None,
        requires_tool_listing: bool = False,
    ) -> None:
        self.workers, self.profile, self.worker, self.model = workers, profile, worker, model
        # Non-secret harness settings from the registry (ADR-033), checked by the worker.
        self.settings = settings
        self.requires_tool_listing = requires_tool_listing

    def check(self) -> None:
        self.workers.require(self.profile, self.worker)

    @staticmethod
    def job(job_key: str) -> str:
        return job_id("session", job_key)

    def reap(self, job_key: str) -> None:
        self.workers.reap(self.job(job_key))

    async def run(self, request: HarnessRequest) -> HarnessResult:
        job = self.job(request.job_key or request.mcp_token)
        activity = activities.current()
        # The harness runs inside the worker, so its turns and stderr are not visible here;
        # the unit name ties this attempt to the host's record of the worker.
        activity.event(
            "confined session starting",
            job=job,
            unit=f"cardea-host-{job}.service",
        )
        try:
            self.check()
            output = await self.workers.run(
                job,
                {
                    "model": request.model or self.model,
                    "request": request.model_dump(
                        mode="json", exclude={"model"}, exclude_none=True
                    ),
                    **({"harness": self.settings} if self.settings is not None else {}),
                },
                profile=self.profile,
                worker=self.worker,
                deadline=time.time() + max(1.0, request.timeout_seconds),
            )
        except Fault as refused:
            activity.event("confined session refused", logging.WARNING, code=refused.code, job=job)
            return HarnessResult.failed(refused.code)
        except ExecutorBusy:
            activity.event("confined session found no free slot", logging.WARNING, job=job)
            return HarnessResult.failed("executor_busy")
        except OperationRejected:
            activity.event("confined session did not start", logging.WARNING, job=job)
            return HarnessResult.failed("worker_not_started")
        except WorkerFailed as failed:
            activity.event(
                "confined session worker failed",
                logging.WARNING,
                code=failed.code,
                reason=failed.summary,
                job=job,
            )
            return HarnessResult.failed(failed.code)
        activity.event("confined session finished", job=job)
        try:
            fields = {
                key: output[key]
                for key in (
                    "text",
                    "success",
                    "is_error",
                    "turns",
                    "cost_usd",
                    "failure_reason",
                    "stop_reason",
                    "usage",
                )
                if output.get(key) is not None
            }
            fields.setdefault("text", "")
            if "success" not in fields:
                fields.setdefault("is_error", True)
            if isinstance(fields.get("stop_reason"), str):
                fields["stop_reason"] = fields["stop_reason"][:128]
            return HarnessResult.model_validate(fields)
        except (AttributeError, TypeError, ValueError):
            return HarnessResult.failed("worker_output_invalid")
