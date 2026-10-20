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
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from pydantic import JsonValue

from cardea.hosts.debian import WorkerHost
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service.database import Fault
from cardea.service.operations import (
    ConfinedExecutor,
    Handler,
    OperationContext,
    OperationRejected,
)

log = logging.getLogger(__name__)
POLL_SECONDS = 0.25
# systemd unit results that identify an enforced limit.
LIMIT_RESULTS = {"oom-kill": "worker_memory_limit", "timeout": "worker_timeout"}
Watch = Callable[[], str | None]


class WorkerFailed(Exception):
    """The worker started, or may have started, and produced no result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkerStopped(WorkerFailed):
    """Cardea stopped a running worker because of cancellation or a lost lease."""


def job_id(kind: str, key: str) -> str:
    """Deterministic helper job: recovery can find it, and the helper refuses a replay."""
    return hashlib.sha256(f"cardea-{kind}:{key}".encode()).hexdigest()[:32]


def _result(status: dict[str, Any]) -> dict[str, Any]:
    result = status.get("result")
    if result in LIMIT_RESULTS:
        raise WorkerFailed(LIMIT_RESULTS[str(result)])
    if result != "success" or str(status.get("exit_status")) != "0":
        raise WorkerFailed("worker_failed")
    try:
        document = json.loads(base64.b64decode(str(status.get("output", "")), validate=True))
    except (ValueError, binascii.Error):
        raise WorkerFailed("worker_output_invalid") from None
    if isinstance(document, dict) and set(document) == {"rejected"}:
        raise OperationRejected(str(document["rejected"])[:256])
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
    ) -> dict[str, Any]:
        content = json.dumps(payload, separators=(",", ":")).encode()
        try:
            await asyncio.to_thread(
                self.host.start_confined, job, content, deadline, profile, worker
            )
        except Fault as error:
            if error.code == "host_rejected":
                raise OperationRejected(f"worker_not_started: {error.message}") from None
            # The helper may have passed its fence before failing: treat as started.
            await asyncio.to_thread(self.reap, job)
            raise WorkerFailed("worker_start_unknown") from None
        try:
            while True:
                try:
                    status = await asyncio.to_thread(self.host.status, job)
                except Fault:
                    raise WorkerFailed("worker_status_unknown") from None
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
            deadline=min(time.time() + timeout_seconds, context.deadline.timestamp()),
            watch=lambda: watch(context.task_id, context.lease_id),
        )

    return run


class ConfinedAgentSession:
    """Runs one agent session inside a confined worker (ADR-028).

    The worker holds the session's only credential (loaded by systemd from the profile),
    reaches the model API through the profile's proxy and Cardea's loopback tool endpoint,
    and sees neither the Cardea store nor the service's configuration.
    """

    def __init__(self, workers: Workers, profile: str, worker: str, model: str | None) -> None:
        self.workers, self.profile, self.worker, self.model = workers, profile, worker, model

    def check(self) -> None:
        self.workers.require(self.profile, self.worker)

    @staticmethod
    def job(job_key: str) -> str:
        return job_id("session", job_key)

    def reap(self, job_key: str) -> None:
        self.workers.reap(self.job(job_key))

    async def run(self, request: SessionRequest) -> SessionResult:
        try:
            self.check()
            output = await self.workers.run(
                self.job(request.job_key or request.mcp_token),
                {"model": self.model, "request": asdict(request)},
                profile=self.profile,
                worker=self.worker,
                deadline=time.time() + max(1.0, request.timeout_seconds),
            )
        except Fault as refused:
            return SessionResult(text="", is_error=True, stop_reason=refused.code)
        except OperationRejected:
            return SessionResult(text="", is_error=True, stop_reason="worker_not_started")
        except WorkerFailed as failed:
            return SessionResult(text="", is_error=True, stop_reason=failed.code)
        try:
            return SessionResult(
                text=str(output.get("text", "")),
                is_error=bool(output.get("is_error", True)),
                cost_usd=float(output["cost_usd"]) if output.get("cost_usd") is not None else None,
                turns=int(output.get("turns", 0)),
                stop_reason=str(output.get("stop_reason", ""))[:128],
                usage=dict(output.get("usage") or {}),
            )
        except (TypeError, ValueError):
            return SessionResult(text="", is_error=True, stop_reason="worker_output_invalid")
