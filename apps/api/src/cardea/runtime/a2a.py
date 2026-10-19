"""Native A2A 1.0 on the private socket, backed by the existing execution owner."""

import json
from collections.abc import AsyncGenerator
from typing import Any, NoReturn

from a2a.client.card_resolver import A2ACardResolver
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError, TaskNotCancelableError, UnsupportedOperationError
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import (
    decode_page_token,
    encode_page_token,
    validate_history_length,
    validate_page_size,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cardea.kernel.models import ResourceRef, TaskState
from cardea.service.auth import Proof
from cardea.service.database import Fault, borrow
from cardea.service.delegation import child_request
from cardea.service.kernel import Kernel
from cardea.service.models import BaseHostTaskRequest, DelegationTaskRequest, Signal
from cardea.service.policy import Policy

URL = "http://cardea/a2a/worker"
CARD_PATH = "/a2a/worker/.well-known/agent-card.json"
CARD = a2a.AgentCard(
    name="Cardea installed host worker",
    description="Bounded hash/copy delegation on this host",
    version="1",
    supported_interfaces=[
        a2a.AgentInterface(url=URL, protocol_binding="JSONRPC", protocol_version="1.0")
    ],
    security_schemes={
        "workload": a2a.SecurityScheme(
            http_auth_security_scheme=a2a.HTTPAuthSecurityScheme(
                scheme="bearer", bearer_format="JWT-SVID"
            )
        )
    },
    security_requirements=[a2a.SecurityRequirement(schemes={"workload": a2a.StringList()})],
    capabilities=a2a.AgentCapabilities(),
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    skills=[
        a2a.AgentSkill(
            id="host",
            name="Hash/copy",
            description="Installed confined operations",
            tags=["host.sha256", "host.copy"],
        )
    ],
)
STATES = {
    TaskState.QUEUED: a2a.TaskState.TASK_STATE_SUBMITTED,
    TaskState.RUNNING: a2a.TaskState.TASK_STATE_WORKING,
    TaskState.WAITING: a2a.TaskState.TASK_STATE_WORKING,
    TaskState.SUSPENDED: a2a.TaskState.TASK_STATE_WORKING,
    TaskState.SUCCEEDED: a2a.TaskState.TASK_STATE_COMPLETED,
    TaskState.FAILED: a2a.TaskState.TASK_STATE_FAILED,
    TaskState.CANCELLED: a2a.TaskState.TASK_STATE_CANCELED,
}


def credential(kernel: Kernel, headers: Any) -> tuple[Proof, str]:
    bearer = headers.get("authorization", "")
    parent = headers.get("x-cardea-parent", "")
    if not bearer.startswith("Bearer ") or not parent:
        raise Fault("invalid_credential", "Bound coordinator proof required", 401)
    proof = kernel.auth.validate(bearer[7:])
    with kernel.database.read() as conn:
        binding = Policy(conn).ceiling(parent, "tasks.delegate")
        if (
            proof.resolve(conn) != binding["runtime_id"]
            or binding["runtime_id"] != kernel.runtime_id
        ):
            raise Fault("delegation_binding", "Caller is not the bound coordinator runtime")
    return proof, parent


class Handler(RequestHandler):
    """SDK extension point: no second task store or in-process execution scheduler."""

    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel

    def bound(self, context: ServerCallContext) -> tuple[Proof, str]:
        return credential(self.kernel, context.state.get("headers", {}))

    async def on_message_send(
        self, params: a2a.SendMessageRequest, context: ServerCallContext
    ) -> a2a.Task | a2a.Message:
        return await run_in_threadpool(self._message_send, params, context)

    def _message_send(
        self, params: a2a.SendMessageRequest, context: ServerCallContext
    ) -> a2a.Task | a2a.Message:
        proof, parent = self.bound(context)
        validate_proto_required_fields(params)
        message = params.message
        if (
            not params.configuration.return_immediately
            or message.task_id
            or message.context_id != parent
            or message.role != a2a.Role.ROLE_USER
            or len(message.parts) != 1
            or not message.parts[0].text
        ):
            raise InvalidParamsError("Use one bounded JSON message and returnImmediately")
        try:
            request = BaseHostTaskRequest.model_validate_json(message.parts[0].text)
            slot = int(message.message_id.rsplit(".", 1)[1])
            if message.message_id != request.request_id or request.parent_task_id != parent:
                raise ValueError("Message identity mismatch")
        except (ValueError, IndexError):
            raise InvalidParamsError("Invalid bounded delegation message") from None
        try:
            task = self.kernel.submit(proof, request, delegation=(parent, slot))
        except Fault as error:
            raise InvalidParamsError(error.code) from None
        self.kernel.fault("delegation.child_admitted")
        return self._get_task(a2a.GetTaskRequest(id=task.task.task_id), context)

    async def on_get_task(self, params: a2a.GetTaskRequest, context: ServerCallContext) -> a2a.Task:
        return await run_in_threadpool(self._get_task, params, context)

    def _get_task(self, params: a2a.GetTaskRequest, context: ServerCallContext) -> a2a.Task:
        validate_proto_required_fields(params)
        validate_history_length(params)
        proof, parent = self.bound(context)
        with self.kernel.database.read() as conn, borrow(conn) as tx:
            policy = Policy(conn)
            binding = policy.access(proof.resolve(conn), params.id, "tasks.inspect")
            link = conn.exec_driver_sql(
                "SELECT slot,message_id FROM delegation_links WHERE child_id=? AND parent_id=?",
                (params.id, parent),
            ).first()
            if link is None:
                raise Fault("task_not_found", "Unknown peer task", 404)
            task = tx.get_task(params.id)
            result = a2a.Task(
                id=task.task_id, context_id=parent, status=a2a.TaskStatus(state=STATES[task.state])
            )
            result.status.timestamp.FromDatetime(task.updated_at)
            if not params.HasField("history_length") or params.history_length > 0:
                result.history.append(
                    a2a.Message(
                        message_id=link[1],
                        context_id=parent,
                        role=a2a.Role.ROLE_USER,
                        parts=[a2a.Part(text=binding["request_json"])],
                    )
                )
            if task.state is TaskState.SUCCEEDED:
                policy.ceiling(task.task_id, "objects.read")
                output = tx.latest_object(
                    ResourceRef(
                        namespace=binding["namespace"], resource_id=f"{task.task_id}.output"
                    )
                )
                artifact = ParseDict(
                    {
                        "artifactId": "output",
                        "parts": [
                            {
                                "data": {
                                    "reference": output.model_dump(mode="json"),
                                    "content": json.loads(tx.read_object(output)),
                                }
                            }
                        ],
                    },
                    a2a.Artifact(),
                )
                result.artifacts.append(artifact)
            return result

    async def on_cancel_task(
        self, params: a2a.CancelTaskRequest, context: ServerCallContext
    ) -> a2a.Task:
        return await run_in_threadpool(self._cancel_task, params, context)

    def _cancel_task(self, params: a2a.CancelTaskRequest, context: ServerCallContext) -> a2a.Task:
        validate_proto_required_fields(params)
        task = self._get_task(a2a.GetTaskRequest(id=params.id, history_length=0), context)
        if task.status.state in {
            a2a.TaskState.TASK_STATE_COMPLETED,
            a2a.TaskState.TASK_STATE_FAILED,
        }:
            raise TaskNotCancelableError("Task has already terminated")
        proof, parent = self.bound(context)
        self.kernel.control(proof, params.id, Signal(request_id=f"a2a-cancel.{parent}"), "cancel")
        # A running worker remains working until stop/reconciliation is confirmed.
        return self._get_task(a2a.GetTaskRequest(id=params.id), context)

    async def on_list_tasks(
        self, params: a2a.ListTasksRequest, context: ServerCallContext
    ) -> a2a.ListTasksResponse:
        return await run_in_threadpool(self._list_tasks, params, context)

    def _list_tasks(
        self, params: a2a.ListTasksRequest, context: ServerCallContext
    ) -> a2a.ListTasksResponse:
        _, parent = self.bound(context)
        validate_proto_required_fields(params)
        validate_history_length(params)
        size = params.page_size if params.HasField("page_size") else 50
        validate_page_size(size)
        if params.context_id and params.context_id != parent:
            return a2a.ListTasksResponse(page_size=size, total_size=0)
        with self.kernel.database.read() as conn:
            ids = [
                str(r[0])
                for r in conn.exec_driver_sql(
                    "SELECT child_id FROM delegation_links WHERE parent_id=?", (parent,)
                )
            ]
        tasks = []
        for identity in ids:
            query = a2a.GetTaskRequest(id=identity)
            if params.HasField("history_length"):
                query.history_length = params.history_length
            task = self._get_task(query, context)
            if params.status and task.status.state != params.status:
                continue
            if (
                params.HasField("status_timestamp_after")
                and task.status.timestamp.ToNanoseconds()
                <= params.status_timestamp_after.ToNanoseconds()
            ):
                continue
            if not params.include_artifacts:
                task.ClearField("artifacts")
            tasks.append(task)
        tasks.sort(key=lambda t: (t.status.timestamp.ToNanoseconds(), t.id), reverse=True)
        total = len(tasks)
        if params.page_token:
            identity = decode_page_token(params.page_token)
            positions = [i for i, t in enumerate(tasks) if t.id == identity]
            if not positions:
                raise InvalidParamsError("Cursor is outside this task collection")
            tasks = tasks[positions[0] + 1 :]
        page = tasks[:size]
        return a2a.ListTasksResponse(
            tasks=page,
            page_size=size,
            total_size=total,
            next_page_token=encode_page_token(page[-1].id) if len(tasks) > size else "",
        )

    async def unsupported(self, params: Any, context: ServerCallContext) -> NoReturn:
        await run_in_threadpool(self.bound, context)
        raise UnsupportedOperationError("Not supported by this installed profile")

    async def no_stream(self, params: Any, context: ServerCallContext) -> AsyncGenerator[Any, None]:
        await run_in_threadpool(self.bound, context)
        raise UnsupportedOperationError("Use GetTask polling; streaming is not advertised")
        yield  # pragma: no cover

    on_create_task_push_notification_config = unsupported
    on_get_task_push_notification_config = unsupported
    on_list_task_push_notification_configs = unsupported
    on_delete_task_push_notification_config = unsupported
    on_get_extended_agent_card = unsupported
    on_message_send_stream = no_stream
    on_subscribe_to_task = no_stream


def routes(kernel: Kernel) -> list[Route]:
    native_card = create_agent_card_routes(CARD, card_url=CARD_PATH)[0]

    async def discovery(request: Request) -> Any:
        try:
            await run_in_threadpool(credential, kernel, request.headers)
        except Fault as error:
            return JSONResponse({"code": error.code}, status_code=error.status)
        return await native_card.endpoint(request)

    native_rpc = create_jsonrpc_routes(Handler(kernel), "/a2a/worker")[0]

    async def rpc(request: Request) -> Any:
        try:
            await run_in_threadpool(credential, kernel, request.headers)
        except Fault as error:
            return JSONResponse(
                {"code": error.code},
                status_code=error.status,
                headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None,
            )
        return await native_rpc.endpoint(request)

    return [Route(CARD_PATH, discovery), Route("/a2a/worker", rpc, methods=["POST"])]


async def discover(client: Any) -> JsonRpcTransport:
    client.headers["A2A-Version"] = "1.0"
    card = await A2ACardResolver(client, "http://cardea").get_agent_card(
        relative_card_path=CARD_PATH
    )
    # A card is discovery data, never permission to redirect credentials/disclosures.
    if card != CARD:
        raise Fault("peer_card_changed", "Installed peer contract differs", 503)
    return JsonRpcTransport(client, card, URL)


def message(parent: str, request: DelegationTaskRequest, slot: int) -> a2a.SendMessageRequest:
    child = child_request(parent, request, slot)
    return a2a.SendMessageRequest(
        message=a2a.Message(
            message_id=child.request_id,
            context_id=parent,
            role=a2a.Role.ROLE_USER,
            parts=[a2a.Part(text=child.model_dump_json())],
        ),
        configuration=a2a.SendMessageConfiguration(return_immediately=True),
    )


def artifact_content(task: a2a.Task) -> dict[str, Any]:
    if len(task.artifacts) != 1 or len(task.artifacts[0].parts) != 1:
        raise Fault("verification_failed", "Peer omitted the expected artifact")
    return dict(MessageToDict(task.artifacts[0].parts[0].data))
