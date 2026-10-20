"""Bounded versioned HTTP resource API on the private Unix socket and optional TLS listener."""

import json
import logging
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cardea.kernel.lifecycle import ContractError
from cardea.kernel.models import KernelEvent, ResourceRef
from cardea.kernel.store import StoreError
from cardea.service import agent_budget, host_budget, host_operations
from cardea.service.auth import Proof
from cardea.service.database import Fault, borrow
from cardea.service.kernel import Kernel
from cardea.service.models import (
    RIGHTS,
    AgentInput,
    AgentRunRequest,
    AgentTaskRequest,
    DelegationTaskRequest,
    ErrorBody,
    HostObjectTaskRequest,
    HostTaskRequest,
    OperationTaskRequest,
    Signal,
    TaskRequest,
    TaskView,
    TriggerSpec,
)
from cardea.service.policy import Policy
from cardea.service.profiles import for_runtime

log = logging.getLogger(__name__)


def receipt_operation(conn: sa.Connection, runtime_id: str, task_id: str) -> str:
    fixed = for_runtime(runtime_id).receipt_operation
    if fixed is not None:
        return fixed
    latest = conn.exec_driver_sql(
        "SELECT operation_id FROM operations WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).first()
    if latest is None:
        raise Fault("receipt_unavailable", "Task has not admitted an operation", 409)
    return str(latest[0])


TaskID = Annotated[str, Path(pattern=r"^task-[0-9a-f]{32}$")]
TriggerPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,110}$")]
OperationID = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Namespace = Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


DEFAULT_MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 262144


class Bounds:
    def __init__(self, app: ASGIApp, max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        body = bytearray()
        if headers.get(b"content-encoding", b"identity") != b"identity":
            await JSONResponse(
                {
                    "schema_version": "0.2",
                    "code": "unsupported_encoding",
                    "message": "Only identity content encoding is supported",
                },
                status_code=415,
            )(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await JSONResponse(
                    {
                        "schema_version": "0.2",
                        "code": "request_too_large",
                        "message": f"Request exceeds {self.max_request_bytes} bytes",
                    },
                    status_code=413,
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        messages: list[Message] = []
        response_size = 0

        async def bounded_send(message: Message) -> None:
            nonlocal response_size
            response_size += len(message.get("body", b""))
            messages.append(message)

        await self.app(scope, bounded_receive, bounded_send)
        if response_size > MAX_RESPONSE_BYTES:
            await JSONResponse(
                {
                    "schema_version": "0.2",
                    "code": "response_too_large",
                    "message": "Use a smaller page",
                },
                status_code=413,
            )(scope, receive, send)
        else:
            for message in messages:
                await send(message)


def create_app(kernel: Kernel, max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES) -> FastAPI:
    """The v0.2 API; ``max_request_bytes`` is the host owner's request-body cap."""
    app = FastAPI(
        title="Cardea kernel",
        version="0.2",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        responses={
            status: {"model": ErrorBody}
            for status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 500, 503)
        },
    )
    app.add_middleware(Bounds, max_request_bytes=max_request_bytes)

    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
        x_cardea_auth: Annotated[str, Header()] = "spiffe",
    ) -> Proof:
        if not authorization or authorization.scheme.lower() != "bearer":
            raise Fault("invalid_credential", "Bearer authentication is required", 401)
        return kernel.auth.validate(authorization.credentials, x_cardea_auth)

    Credential = Annotated[Proof, Depends(authenticate)]

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": "0.2",
                "code": "http_error",
                "message": "HTTP request cannot be served",
            },
            status_code=error.status_code,
        )

    @app.exception_handler(Fault)
    async def fault_handler(request: Request, error: Fault) -> JSONResponse:
        return JSONResponse(
            {"schema_version": "0.2", "code": error.code, "message": error.message},
            status_code=error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": "0.2",
                "code": "invalid_request",
                "message": "Request does not match the versioned schema",
            },
            status_code=422,
        )

    @app.exception_handler(StoreError)
    @app.exception_handler(ContractError)
    async def conflict_handler(request: Request, error: StoreError | ContractError) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": "0.2",
                "code": str(error.code),
                "message": "Operation cannot be applied to current state",
            },
            status_code=409,
        )

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, error: Exception) -> JSONResponse:
        log.error(
            "unhandled request error",
            exc_info=error,
            extra={"method": request.method, "path": request.url.path},
        )
        return JSONResponse(
            {
                "schema_version": "0.2",
                "code": "internal_error",
                "message": "Operation failed; inspect state before retry",
            },
            status_code=500,
        )

    @app.get("/v0.2/system")
    def describe(proof: Credential) -> dict[str, Any]:
        with kernel.database.read() as conn:
            proof.resolve(conn)
            prefix = Policy(conn).recovery_prefix
        return {
            "schema_version": "0.2",
            "runtime": "canonical-json-v1",
            "host_execution": kernel.host is not None,
            "agent_execution": kernel.model is not None,
            "delegation_execution": kernel.host is not None,
            "new_namespace_prefix": prefix,
            "max_request_bytes": max_request_bytes,
            "max_response_bytes": MAX_RESPONSE_BYTES,
        }

    @app.get("/v0.2/operations")
    def operations(proof: Credential, namespace: Namespace) -> dict[str, Any]:
        with kernel.database.read() as conn:
            principal = proof.resolve(conn)
            policy = Policy(conn)
            allowed = []
            agent_rights = ("models.invoke", "models.disclose", "tools.incidents", "inputs.append")
            for operation in (
                *RIGHTS,
                *(("tasks.delegate",) if kernel.host else ()),
                *(host_operations.OPERATIONS if kernel.host else ()),
                *(agent_rights if kernel.model else ()),
            ):
                try:
                    policy.require(principal, namespace, operation)
                    policy.require("platform", namespace, operation)
                    if operation in agent_rights:
                        for required in (
                            *agent_rights,
                            "objects.read",
                            "objects.write",
                            "tasks.submit",
                            "events.read",
                        ):
                            policy.require(principal, namespace, required)
                            policy.require(principal, namespace, required, "delegate")
                            policy.require("platform", namespace, required)
                        agent_budget.available(
                            conn, principal, namespace, agent_budget.INPUT_RESERVE + 2048
                        )
                        host_budget.available(conn, principal, namespace, reserve=True)
                    if operation in host_operations.OPERATIONS:
                        policy.require(principal, namespace, operation, "delegate")
                        for required in ("objects.read", "objects.write", "tasks.submit"):
                            policy.require(principal, namespace, required)
                            policy.require("platform", namespace, required)
                            policy.require(principal, namespace, required, "delegate")
                        host_budget.available(conn, principal, namespace, reserve=True)
                except Fault:
                    continue
                allowed.append(operation)
            registered = []
            for name, descriptor in kernel.operations.descriptors.items():
                try:
                    for required in (
                        name,
                        "objects.read",
                        "objects.write",
                        "tasks.submit",
                        *descriptor.cardea.requires,
                    ):
                        policy.require(principal, namespace, required)
                        policy.require(principal, namespace, required, "delegate")
                        policy.require("platform", namespace, required)
                except Fault:
                    continue
                allowed.append(name)
                registered.append(
                    {
                        "name": name,
                        "tool": descriptor.tool,
                        "version": descriptor.cardea.version,
                        "effect": descriptor.cardea.effect,
                        "timeout_seconds": descriptor.cardea.timeout_seconds,
                        "max_output_bytes": descriptor.cardea.max_output_bytes,
                        "requires": list(descriptor.cardea.requires),
                        "retry": "read_only"
                        if descriptor.cardea.effect == "read"
                        else "reconciliation_required",
                    }
                )
        return {
            "schema_version": "0.2",
            "operations": allowed,
            "installed": [
                {
                    "name": name,
                    "description": host_operations.OPERATIONS[name],
                    "input_modes": ["inline_json", "object_version"],
                    "max_input_bytes": 16384,
                    "reserved_tool_milliseconds": 10000,
                    "reserved_output_bytes": 65536,
                    "network": "denied",
                    "output_scope": "task-owned",
                    "retry": "reconciliation_required",
                }
                for name in allowed
                if name in host_operations.OPERATIONS
            ],
            "registered": registered,
        }

    @app.get("/v0.2/host-budget")
    def budget(proof: Credential, namespace: Namespace) -> dict[str, Any]:
        with kernel.database.read() as conn:
            principal = proof.resolve(conn)
            policy = Policy(conn)
            policy.require(principal, namespace, "tasks.inspect")
            policy.require("platform", namespace, "tasks.inspect")
            limits = (
                conn.exec_driver_sql(
                    "SELECT tool_ms,output_bytes,concurrency FROM host_budget_limits "
                    "WHERE principal_id=? AND namespace=?",
                    (principal, namespace),
                )
                .mappings()
                .first()
            )
            usage = (
                conn.exec_driver_sql(
                    "SELECT COALESCE(SUM(tool_ms),0) AS allocated_tool_ms,"
                    "COALESCE(SUM(output_bytes),0) AS allocated_output_bytes,"
                    "COALESCE(SUM(1-settled),0) AS active_reservations,"
                    "COALESCE(SUM(CASE WHEN settled=1 THEN tool_ms ELSE 0 END),0) "
                    "AS charged_tool_ms,"
                    "COALESCE(SUM(CASE WHEN settled=1 THEN output_bytes ELSE 0 END),0) "
                    "AS charged_output_bytes,"
                    "COALESCE(SUM(CASE WHEN settled=1 AND cleaned=0 THEN 1 ELSE 0 END),0) "
                    "AS cleanup_pending "
                    "FROM (SELECT tool_ms,output_bytes,settled,cleaned FROM host_reservations "
                    "WHERE principal_id=? AND namespace=? UNION ALL "
                    "SELECT tool_ms,output_bytes,settled,cleaned FROM agent_effects "
                    "WHERE principal_id=? AND namespace=? AND kind='tool')",
                    (principal, namespace, principal, namespace),
                )
                .mappings()
                .one()
            )
        return {"schema_version": "0.2", "limits": dict(limits) if limits else None, **dict(usage)}

    @app.get("/v0.2/model-budget")
    def model_budget(proof: Credential, namespace: Namespace) -> dict[str, Any]:
        with kernel.database.read() as conn:
            principal = proof.resolve(conn)
            policy = Policy(conn)
            policy.require(principal, namespace, "tasks.inspect")
            policy.require("platform", namespace, "tasks.inspect")
            limit = (
                conn.exec_driver_sql(
                    "SELECT calls,tokens FROM model_budget_limits "
                    "WHERE principal_id=? AND namespace=?",
                    (principal, namespace),
                )
                .mappings()
                .first()
            )
            usage = (
                conn.exec_driver_sql(
                    "SELECT COALESCE(SUM(calls),0) AS allocated_calls,"
                    "COALESCE(SUM(tokens),0) AS allocated_tokens FROM agent_effects "
                    "WHERE principal_id=? AND namespace=?",
                    (principal, namespace),
                )
                .mappings()
                .one()
            )
            return {
                "schema_version": "0.2",
                "limits": dict(limit) if limit else None,
                **dict(usage),
            }

    @app.put("/v0.2/triggers/{trigger_id}")
    def schedule(trigger_id: TriggerPath, spec: TriggerSpec, proof: Credential) -> dict[str, Any]:
        if spec.trigger_id != trigger_id:
            raise Fault("trigger_mismatch", "Path and body trigger IDs differ", 422)
        return {"schema_version": "0.2", **kernel.schedule(proof, spec)}

    @app.delete("/v0.2/triggers/{trigger_id}")
    def unschedule(trigger_id: TriggerPath, proof: Credential) -> dict[str, Any]:
        return {"schema_version": "0.2", **kernel.unschedule(proof, trigger_id)}

    @app.get("/v0.2/tasks/{task_id}/run")
    def run_view(task_id: TaskID, proof: Credential) -> dict[str, Any]:
        """What a run did: state, output, every call with its receipt state, and evidence."""
        with kernel.database.read() as conn, borrow(conn) as tx:
            principal = proof.resolve(conn)
            Policy(conn).access(principal, task_id, "tasks.inspect")
            task = tx.get_task(task_id)
            namespace = Policy(conn).binding(task_id)["namespace"]
            documents: dict[str, Any] = {}
            for suffix in ("output", "verification"):
                try:
                    stored = tx.latest_object(
                        ResourceRef(namespace=namespace, resource_id=f"{task_id}.{suffix}")
                    )
                    documents[suffix] = json.loads(tx.read_object(stored))
                except StoreError:
                    documents[suffix] = None
            calls = [
                {
                    "operation_id": r.request.operation_id,
                    "operation": r.request.name,
                    "state": r.state.value,
                    "arguments": r.request.arguments.get("arguments"),
                    "result": r.result,
                    "failure": r.failure.code if r.failure else None,
                    "reconciled": bool(r.reconciliation_evidence),
                    "updated_at": r.updated_at.isoformat(),
                }
                # In the order they were made: an agent run's context calls come first.
                for r in sorted(
                    tx.list_operations(task_id),
                    key=lambda r: (r.created_at, r.request.operation_id),
                )
            ]
        return {
            "schema_version": "0.2",
            "task_id": task_id,
            "state": task.state.value,
            "failure": task.failure.model_dump(mode="json") if task.failure else None,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "attempt": task.checkpoint.state.get("attempt") if task.checkpoint else None,
            "output": documents["output"],
            "verification": documents["verification"],
            "calls": calls,
        }

    @app.get("/v0.2/triggers")
    def triggers(
        proof: Credential,
        after: Annotated[str, Query(max_length=128)] = "",
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        history: Annotated[int, Query(ge=0, le=50)] = 20,
    ) -> dict[str, Any]:
        """Read-only: the caller's own triggers (as supervisor) and recent slot history."""
        with kernel.database.read() as conn:
            principal = proof.resolve(conn)
            policy = Policy(conn)
            rows = (
                conn.exec_driver_sql(
                    "SELECT * FROM triggers WHERE supervisor_id=? AND trigger_id>? "
                    "ORDER BY trigger_id LIMIT ?",
                    (principal, after, limit),
                )
                .mappings()
                .all()
            )
            listed = []
            for row in rows:
                try:
                    policy.require(principal, row["namespace"], "tasks.inspect")
                    policy.require("platform", row["namespace"], "tasks.inspect")
                except Fault:
                    continue
                events = (
                    conn.exec_driver_sql(
                        "SELECT event_id,kind,slot,task_id,code,detail_json,at "
                        "FROM trigger_events WHERE trigger_id=? ORDER BY event_id DESC LIMIT ?",
                        (row["trigger_id"], history),
                    )
                    .mappings()
                    .all()
                )
                listed.append(
                    {
                        "trigger_id": row["trigger_id"],
                        "namespace": row["namespace"],
                        "definition": json.loads(row["definition_json"]),
                        "definition_sha256": row["definition_sha256"],
                        "enabled": bool(row["enabled"]),
                        "removed": bool(row["removed"]),
                        "last_slot": row["last_slot"],
                        "events": [
                            {
                                **{k: e[k] for k in ("event_id", "kind", "slot", "task_id")},
                                "code": e["code"],
                                "at": e["at"],
                                "detail": json.loads(e["detail_json"]),
                            }
                            for e in events
                        ],
                    }
                )
        return {
            "schema_version": "0.2",
            "triggers": listed,
            "next": rows[-1]["trigger_id"] if len(rows) == limit else None,
        }

    @app.post("/v0.2/tasks", response_model=TaskView, status_code=202)
    def submit(request: TaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/host-tasks", response_model=TaskView, status_code=202)
    def submit_host(request: HostTaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/host-object-tasks", response_model=TaskView, status_code=202)
    def submit_host_object(request: HostObjectTaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/agent-runs", response_model=TaskView, status_code=202)
    def submit_agent_run(request: AgentRunRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/operation-tasks", response_model=TaskView, status_code=202)
    def submit_operation(request: OperationTaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/delegation-tasks", response_model=TaskView, status_code=202)
    def submit_delegation(request: DelegationTaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.get("/v0.2/tasks/{task_id}/children")
    def children(task_id: TaskID, proof: Credential) -> dict[str, Any]:
        from cardea.service.delegation import parent_usage

        with kernel.database.read() as conn:
            Policy(conn).access(proof.resolve(conn), task_id, "tasks.inspect")
            return {
                "schema_version": "0.2",
                "children": [
                    dict(r)
                    for r in conn.exec_driver_sql(
                        "SELECT slot,child_id,message_id FROM delegation_links "
                        "WHERE parent_id=? ORDER BY slot",
                        (task_id,),
                    ).mappings()
                ],
                "usage": parent_usage(conn, task_id),
            }

    @app.post("/v0.2/agent-tasks", response_model=TaskView, status_code=202)
    def submit_agent(request: AgentTaskRequest, proof: Credential) -> TaskView:
        return kernel.submit(proof, request)

    @app.post("/v0.2/tasks/{task_id}/inputs", response_model=TaskView)
    def append_input(task_id: TaskID, request: AgentInput, proof: Credential) -> TaskView:
        return kernel.append_input(proof, task_id, request)

    @app.post("/v0.2/tasks/{task_id}/operations/{operation_id}/reconcile")
    def reconcile(task_id: TaskID, operation_id: OperationID, proof: Credential) -> dict[str, Any]:
        from cardea.service.operation_runtime import OperationRuntime

        with kernel.database.read() as conn, borrow(conn) as tx:
            principal = proof.resolve(conn)
            name = tx.get_operation(task_id, operation_id).request.name
            Policy(conn).access(principal, task_id, name)
        state = OperationRuntime(kernel).reconcile(task_id, operation_id)
        return {"schema_version": "0.2", "state": state}

    @app.get("/v0.2/tasks/{task_id}/agent-receipts")
    def agent_receipts(task_id: TaskID, proof: Credential) -> dict[str, Any]:
        """Receipts of an agent's model and tool calls.

        An agent run (ADR-027) receipts each tool call as an operation ``call-NNNN``; an M3
        agent task (ADR-016) lists the operations recorded in its own budget table.
        """
        with kernel.database.read() as conn, borrow(conn) as tx:
            Policy(conn).access(proof.resolve(conn), task_id, "tasks.inspect")
            if tx.get_task(task_id).runtime_id == "agent-run-v1":
                receipts = [
                    r
                    for r in tx.list_operations(task_id)
                    if r.request.operation_id.startswith(("ctx-", "call-"))
                ]
            else:
                rows = conn.exec_driver_sql(
                    "SELECT operation_id FROM agent_effects WHERE task_id=? ORDER BY operation_id",
                    (task_id,),
                ).all()
                receipts = [tx.get_operation(task_id, r[0]) for r in rows]
            return {
                "schema_version": "0.2",
                "receipts": [r.model_dump(mode="json") for r in receipts],
            }

    @app.get("/v0.2/tasks/{task_id}", response_model=TaskView)
    def inspect(task_id: TaskID, proof: Credential) -> TaskView:
        return kernel.inspect(proof, task_id)

    @app.post("/v0.2/tasks/{task_id}/cancel", response_model=TaskView)
    def cancel(task_id: TaskID, signal: Signal, proof: Credential) -> TaskView:
        return kernel.control(proof, task_id, signal, "cancel")

    @app.post("/v0.2/tasks/{task_id}/resume", response_model=TaskView)
    def resume(task_id: TaskID, signal: Signal, proof: Credential) -> TaskView:
        return kernel.control(proof, task_id, signal, "resume")

    @app.get("/v0.2/tasks/{task_id}/events", response_model=list[KernelEvent])
    def events(
        task_id: TaskID,
        proof: Credential,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> list[KernelEvent]:
        with kernel.database.read() as conn, borrow(conn) as tx:
            Policy(conn).access(proof.resolve(conn), task_id, "events.read")
            return list(tx.read_events(task_id=task_id, after_cursor=after, limit=limit))

    @app.get("/v0.2/tasks/{task_id}/output")
    def output(task_id: TaskID, proof: Credential) -> dict[str, Any]:
        with kernel.database.read() as conn, borrow(conn) as tx:
            binding = Policy(conn).access(proof.resolve(conn), task_id, "objects.read")
            version = tx.latest_object(
                ResourceRef(namespace=binding["namespace"], resource_id=f"{task_id}.output")
            )
            return {
                "schema_version": "0.2",
                "artifact": version.model_dump(mode="json"),
                "content": json.loads(tx.read_object(version)),
            }

    @app.get("/v0.2/tasks/{task_id}/receipt")
    def receipt(task_id: TaskID, proof: Credential) -> dict[str, Any]:
        with kernel.database.read() as conn, borrow(conn) as tx:
            Policy(conn).access(proof.resolve(conn), task_id, "tasks.inspect")
            return {
                "schema_version": "0.2",
                "digest_format": "legacy-v0.1",
                "receipt": tx.get_operation(
                    task_id, receipt_operation(conn, tx.get_task(task_id).runtime_id, task_id)
                ).model_dump(mode="json"),
            }

    from cardea.runtime.a2a import routes

    app.router.routes.extend(routes(kernel))
    return app
