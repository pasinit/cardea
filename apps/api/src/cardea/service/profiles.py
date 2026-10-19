"""Installed task profiles: one record per runtime instead of runtime-id string matching."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cardea.service import agent_budget
from cardea.service.database import Fault
from cardea.service.models import (
    AgentRunRequest,
    AgentTaskRequest,
    BaseHostTaskRequest,
    DelegationTaskRequest,
    OperationTaskRequest,
    TaskRequest,
)

QUEUE = "cardea-m1"
WORKFLOW = "cardea.m1.task"
HOST_WORKFLOW = "cardea.m2.host"
AGENT_WORKFLOW = "cardea.m3.agent"
DELEGATION_WORKFLOW = "cardea.m4.delegation"
DELEGATION_QUEUE = "cardea-coordinators"
OPERATION_WORKFLOW = "cardea.m6.operation"
AGENT_RUN_WORKFLOW = "cardea.m6.agent_run"
AGENT_RUN_QUEUE = "cardea-agent-runs"


@dataclass(frozen=True)
class Profile:
    runtime_id: str
    verifier_id: str
    matches: Callable[[TaskRequest], bool]
    # BudgetLimits fields other than the deadline, which admission derives from the request.
    limits: Callable[[Any], dict[str, int]]
    workflow: str = WORKFLOW
    queue: str = QUEUE
    requires_host: bool = False
    requires_model: bool = False
    # Needs the configured agent session runtime (ADR-027).
    requires_session: bool = False
    # The runtime, not admission, cancels a RUNNING task: it must stop an effect first.
    runtime_cancels_running: bool = False
    # The runtime owns every cancellation, e.g. a coordinator that must cancel children.
    runtime_cancels: bool = False
    # Children may only be admitted through this profile's installed peer path.
    owns_children: bool = False
    accepts_input: bool = False
    # Operation whose receipt is the task's receipt; None means its latest operation.
    receipt_operation: str | None = "publish"
    # Admission checks the supervisor's host allocation before queueing.
    reserves_host_budget: bool = False
    # Host-scoped request records reference stored objects that backups must retain.
    host_inputs: bool = False


def host_profile(operation: str) -> Profile:
    name = operation.removeprefix("host.")
    return Profile(
        runtime_id=f"host-{name}-v1",
        verifier_id=f"{name}-verifier-v1",
        matches=lambda r: isinstance(r, BaseHostTaskRequest) and r.operation == operation,
        limits=lambda r: {
            "max_steps": 2,
            "max_tool_milliseconds": 10000,
            "max_output_bytes": 65536,
        },
        workflow=HOST_WORKFLOW,
        requires_host=True,
        reserves_host_budget=True,
        runtime_cancels_running=True,
        receipt_operation="execute",
        host_inputs=True,
    )


PROFILES: tuple[Profile, ...] = (
    host_profile("host.sha256"),
    host_profile("host.copy"),
    Profile(
        runtime_id="incident-agent-v1",
        verifier_id="incident-verifier-v1",
        matches=lambda r: isinstance(r, AgentTaskRequest),
        limits=lambda r: {
            "max_steps": 4,
            "max_model_calls": 2,
            "max_model_tokens": 2 * (agent_budget.INPUT_RESERVE + r.max_output_tokens),
            "max_tool_milliseconds": 20000,
            "max_output_bytes": 131072,
        },
        workflow=AGENT_WORKFLOW,
        requires_host=True,
        requires_model=True,
        reserves_host_budget=True,
        runtime_cancels_running=True,
        accepts_input=True,
        receipt_operation=None,
    ),
    Profile(
        runtime_id="delegation-v1",
        verifier_id="delegation-verifier-v1",
        matches=lambda r: isinstance(r, DelegationTaskRequest),
        limits=lambda r: {
            "max_steps": len(r.children) + 1,
            "max_tool_milliseconds": r.tool_milliseconds,
            "max_output_bytes": r.output_bytes,
            "max_concurrent_operations": r.concurrent_children,
        },
        workflow=DELEGATION_WORKFLOW,
        queue=DELEGATION_QUEUE,
        requires_host=True,
        runtime_cancels=True,
        owns_children=True,
    ),
    Profile(
        runtime_id="operation-v1",
        verifier_id="operation-schema-verifier-v1",
        matches=lambda r: isinstance(r, OperationTaskRequest),
        # The descriptor's own output cap is enforced at settlement.
        limits=lambda r: {"max_steps": 2, "max_output_bytes": 262144},
        workflow=OPERATION_WORKFLOW,
        runtime_cancels_running=True,
        receipt_operation="execute",
    ),
    Profile(
        runtime_id="agent-run-v1",
        verifier_id="agent-run-verifier-v1",
        matches=lambda r: isinstance(r, AgentRunRequest),
        limits=lambda r: {
            "max_steps": r.max_tool_calls,
            "max_output_bytes": 262144,
        },
        workflow=AGENT_RUN_WORKFLOW,
        queue=AGENT_RUN_QUEUE,
        requires_session=True,
        runtime_cancels=True,
        receipt_operation=None,
    ),
    # The deterministic M1 profile accepts any remaining TaskRequest; keep it last.
    Profile(
        runtime_id="canonical-json-v1",
        verifier_id="canonical-json-verifier-v1",
        matches=lambda r: True,
        limits=lambda r: {"max_steps": 2, "max_output_bytes": 32768},
    ),
)
BY_RUNTIME = {profile.runtime_id: profile for profile in PROFILES}


def for_request(request: TaskRequest) -> Profile:
    return next(profile for profile in PROFILES if profile.matches(request))


def for_runtime(runtime_id: str) -> Profile:
    try:
        return BY_RUNTIME[runtime_id]
    except KeyError:
        raise Fault("unknown_profile", "Task runtime profile is not installed", 500) from None


def runtime_ids(predicate: Callable[[Profile], bool]) -> tuple[str, ...]:
    return tuple(profile.runtime_id for profile in PROFILES if predicate(profile))
