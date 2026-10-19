"""Installed coordinator: native peer calls; DBOS owns durable scheduling and retries."""

import logging
from typing import Any
from uuid import uuid4

import httpx
import rfc8785
from a2a.client.errors import AgentCardResolutionError
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import A2AError

from cardea.kernel.models import (
    DispatchLease,
    KernelFailure,
    OperationRequest,
    OperationState,
    ResourceRef,
    RetryClass,
    TaskState,
    VerificationRecord,
)
from cardea.runtime.a2a import artifact_content, discover, message
from cardea.service import host_operations
from cardea.service.database import Fault, borrow
from cardea.service.delegation import active_chain, cancel_children, parent_usage
from cardea.service.kernel import TERMINAL, Kernel
from cardea.service.models import DelegationTaskRequest, now
from cardea.service.policy import Policy

log = logging.getLogger(__name__)


class DelegationRuntime:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    def guard(self, conn: Any, task_id: str) -> DelegationTaskRequest:
        pending = conn.exec_driver_sql(
            "SELECT json_extract(e.payload,'$.code') FROM control_requests c "
            "JOIN events e ON e.cursor=c.cursor "
            "WHERE c.task_id=? AND c.kind='delegation.failure'",
            (task_id,),
        ).scalar_one_or_none()
        if pending:
            raise Fault(str(pending), "Delegation is stopping after a recorded failure")
        policy = Policy(conn)
        binding = policy.ceiling(task_id, "tasks.delegate")
        runtime = self.kernel.auth.runtime_proof().resolve(conn)
        if runtime != binding["runtime_id"] or runtime != self.kernel.runtime_id:
            raise Fault("runtime_binding", "Coordinator identity changed")
        for right in ("objects.read", "objects.write", "tasks.submit"):
            policy.ceiling(task_id, right)
        with borrow(conn) as tx:
            active_chain(conn, tx.get_task(task_id))
        return DelegationTaskRequest.model_validate_json(binding["request_json"])

    def stopping(self, task_id: str, code: str) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            task = tx.get_task(task_id)
            if task.state in TERMINAL:
                return task.state.value
            pending = conn.exec_driver_sql(
                "SELECT json_extract(e.payload,'$.code') FROM control_requests c "
                "JOIN events e ON e.cursor=c.cursor "
                "WHERE c.task_id=? AND c.kind='delegation.failure'",
                (task_id,),
            ).scalar_one_or_none()
            cancelled = conn.exec_driver_sql(
                "SELECT 1 FROM control_requests WHERE task_id=? AND kind='cancel'",
                (task_id,),
            ).first()
            if cancelled:
                # A concurrent peer rejection must not replace acknowledged cancellation.
                code = "cancel_requested"
            elif pending:
                code = str(pending)
            elif code != "cancel_requested":
                identity = "failure-" + uuid4().hex
                binding = Policy(conn).binding(task_id)
                event = tx.append_event(
                    namespace=binding["namespace"],
                    event_type="delegation.stopping",
                    producer_id=self.kernel.runtime_id,
                    correlation_id=identity,
                    task_id=task_id,
                    at=now(),
                    payload={"code": code},
                )
                conn.exec_driver_sql(
                    "INSERT INTO control_requests VALUES (?,?,?,?)",
                    (task_id, identity, "delegation.failure", event.cursor),
                )
            cancel_children(self.kernel, conn, task_id)
            active = conn.exec_driver_sql(
                "SELECT 1 FROM delegation_links l JOIN tasks t ON l.child_id=t.task_id "
                "WHERE l.parent_id=? AND t.state NOT IN ('succeeded','failed','cancelled')",
                (task_id,),
            ).first()
            if active:
                return "waiting"
            if task.lease and now() >= task.lease.expires_at:
                task = tx.recover_task(task_id, expected_revision=task.revision, at=now())
                if task.state in TERMINAL:
                    return task.state.value
            is_cancelled = code == "cancel_requested"
            tx.change_task(
                task_id,
                TaskState.CANCELLED if is_cancelled else TaskState.FAILED,
                expected_revision=task.revision,
                lease_id=task.lease.lease_id if task.lease else None,
                at=now(),
                failure=None
                if is_cancelled
                else KernelFailure(
                    code=code, message="Delegation could not satisfy its bounded objective"
                ),
            )
            return "cancelled" if is_cancelled else "failed"

    async def tick(self, task_id: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
        try:
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if task.state in TERMINAL:
                    return task.state.value
                request = self.guard(conn, task_id)
                if task.state is TaskState.QUEUED:
                    at = now()
                    tx.change_task(
                        task_id,
                        TaskState.RUNNING,
                        expected_revision=task.revision,
                        at=at,
                        lease=DispatchLease(
                            lease_id=f"lease-{uuid4().hex}",
                            worker_id=self.kernel.runtime_id,
                            acquired_at=at,
                            expires_at=task.limits.deadline,
                        ),
                    )
                links: dict[int, str] = {
                    int(row[0]): str(row[1])
                    for row in conn.exec_driver_sql(
                        "SELECT slot,child_id FROM delegation_links "
                        "WHERE parent_id=? ORDER BY slot",
                        (task_id,),
                    ).fetchall()
                }
            self.kernel.fault("delegation.before_peer")
            headers = {
                "Authorization": f"Bearer {self.kernel.auth.runtime_token()}",
                "X-Cardea-Parent": task_id,
                "A2A-Version": "1.0",
            }
            async with httpx.AsyncClient(
                transport=transport
                or httpx.AsyncHTTPTransport(
                    uds=str(self.kernel.database.directory / "kernel.sock"), retries=0
                ),
                headers=headers,
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                peer = await discover(client)
                observed: dict[int, a2a.Task] = {}
                for slot, child in links.items():
                    result = await peer.get_task(a2a.GetTaskRequest(id=child, history_length=0))
                    if result.id != child or result.context_id != task_id:
                        raise Fault("peer_identity_mismatch", "Peer returned a different task")
                    observed[slot] = result
                active = sum(
                    t.status.state
                    not in {
                        a2a.TaskState.TASK_STATE_COMPLETED,
                        a2a.TaskState.TASK_STATE_FAILED,
                        a2a.TaskState.TASK_STATE_CANCELED,
                        a2a.TaskState.TASK_STATE_REJECTED,
                    }
                    for t in observed.values()
                )
                for slot in range(len(request.children)):
                    if slot not in links and active < request.concurrent_children:
                        # Admission checks the live binding again in the receiving transaction.
                        response = await peer.send_message(message(task_id, request, slot))
                        if not response.HasField("task"):
                            raise Fault("invalid_peer_result", "Peer must return a durable task")
                        self.kernel.fault("delegation.after_peer")
                        active += 1
                if len(observed) != len(request.children) or active:
                    return "waiting"
            self.kernel.fault("delegation.before_verify")
            return self.finish(task_id, observed)
        except Fault as error:
            return self.stopping(task_id, error.code)
        except AgentCardResolutionError as error:
            if error.status_code is None and isinstance(error.__cause__, httpx.RequestError):
                return "waiting"
            return self.stopping(task_id, "peer_discovery_denied")
        except A2AError:
            return self.stopping(task_id, "peer_rejected")
        except (httpx.HTTPError, OSError):
            # A local socket failure is reconciled from immutable admission links next tick.
            # Never relaunch a host effect or assume a peer response was committed.
            with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
                task = tx.get_task(task_id)
                if now() >= task.limits.deadline:
                    pass
                else:
                    return "waiting"
            return self.stopping(task_id, "peer_unavailable")
        except Exception:
            log.exception("unexpected delegation failure", extra={"task_id": task_id})
            return self.stopping(task_id, "delegation_runtime_failed")

    def finish(self, task_id: str, observed: dict[int, a2a.Task]) -> str:
        with self.kernel.database.engine.begin() as conn, borrow(conn) as tx:
            request = self.guard(conn, task_id)
            task = tx.get_task(task_id)
            assert task.lease is not None
            results: list[dict[str, Any]] = []
            passed = 0
            for slot, child_id in conn.exec_driver_sql(
                "SELECT slot,child_id FROM delegation_links WHERE parent_id=? ORDER BY slot",
                (task_id,),
            ).fetchall():
                child = tx.get_task(child_id)
                if child.state not in TERMINAL or slot not in observed:
                    raise Fault("verification_failed", "Child has no terminal evidence")
                item: dict[str, Any] = {
                    "slot": slot,
                    "task_id": child_id,
                    "state": child.state.value,
                }
                peer = observed[slot]
                if child.state is TaskState.SUCCEEDED:
                    Policy(conn).ceiling(child_id, "objects.read")
                    expected = host_operations.expected(
                        request.children[slot].operation,
                        rfc8785.dumps(request.children[slot].payload),
                    )
                    output = tx.latest_object(
                        ResourceRef(namespace=request.namespace, resource_id=f"{child_id}.output")
                    )
                    data = artifact_content(peer)
                    if (
                        peer.id != child_id
                        or peer.context_id != task_id
                        or peer.status.state != a2a.TaskState.TASK_STATE_COMPLETED
                        or child.verification is None
                        or child.verification.artifacts != (output,)
                        or tx.read_object(output) != rfc8785.dumps(expected)
                        or rfc8785.dumps(data)
                        != rfc8785.dumps(
                            {"reference": output.model_dump(mode="json"), "content": expected}
                        )
                    ):
                        raise Fault(
                            "verification_failed", "Peer artifact failed independent checks"
                        )
                    item["artifact"] = output.model_dump(mode="json")
                    passed += 1
                else:
                    item["failure"] = child.failure.code if child.failure else "cancelled"
                results.append(item)
            if len(results) != len(request.children) or passed < request.minimum_success:
                raise Fault("child_objective_failed", "Too few verified child results")
            content: dict[str, Any] = {
                "children": results,
                "verified_children": passed,
                "usage": parent_usage(conn, task_id),
            }
            encoded = rfc8785.dumps(content)
            if len(encoded) > 32768:
                raise Fault("output_limit", "Coordinator report exceeds reserved bytes")
            lease = task.lease.lease_id
            operation = OperationRequest(
                operation_id="publish",
                task_id=task_id,
                subject_id=task.agent_id,
                name="objects.write",
                resource=ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.output"),
                arguments={"content": content},
                deadline=task.limits.deadline,
            )
            receipt = tx.admit_operation(
                operation,
                grant_ids=task.capability_ids,
                retry_class=RetryClass.IDEMPOTENT,
                expected_task_revision=task.revision,
                lease_id=lease,
                at=now(),
            )
            receipt = tx.change_operation(
                task_id,
                "publish",
                OperationState.RUNNING,
                expected_revision=receipt.revision,
                lease_id=lease,
                at=now(),
            )
            output = tx.put_object(
                operation.resource,
                encoded,
                expected_version=0,
                producer_id=task.agent_id,
                task_id=task_id,
                lease_id=lease,
                at=now(),
            )
            evidence = tx.put_object(
                ResourceRef(namespace=request.namespace, resource_id=f"{task_id}.verification"),
                rfc8785.dumps(
                    {
                        "passed": True,
                        "output_digest": output.content_digest,
                        "verified_children": passed,
                    }
                ),
                expected_version=0,
                producer_id=task.verifier_id,
                task_id=task_id,
                lease_id=lease,
                at=now(),
            )
            tx.change_operation(
                task_id,
                "publish",
                OperationState.SUCCEEDED,
                expected_revision=receipt.revision,
                lease_id=lease,
                at=now(),
                result={"artifact": output.model_dump(mode="json")},
            )
            at = now()
            tx.change_task(
                task_id,
                TaskState.SUCCEEDED,
                expected_revision=task.revision,
                lease_id=lease,
                at=at,
                verification=VerificationRecord(
                    verification_id=f"verification-{task_id}",
                    task_id=task_id,
                    verifier_id=task.verifier_id,
                    checked_at=at,
                    passed=True,
                    artifacts=(output,),
                    evidence=(
                        ResourceRef(namespace=evidence.namespace, resource_id=evidence.resource_id),
                    ),
                ),
            )
            self.kernel.fault("delegation.publish_before_commit")
            return "succeeded"
