"""Profile registry: each request type maps to one installed runtime record."""

from typing import Any

import pytest

from cardea.service.database import Fault
from cardea.service.models import (
    AgentRunRequest,
    ChildWork,
    DelegationTaskRequest,
    HostObjectTaskRequest,
    HostTaskRequest,
    TaskRequest,
)
from cardea.service.profiles import BY_RUNTIME, PROFILES, for_request, for_runtime, runtime_ids

BASE: dict[str, Any] = {
    "request_id": "r",
    "agent_id": "agent",
    "namespace": "test",
    "objective": "o",
    "grants": ["objects.write"],
    "payload": {},
}


def test_runtime_ids_are_unique_and_catch_all_is_last() -> None:
    assert len(BY_RUNTIME) == len(PROFILES)
    assert PROFILES[-1].runtime_id == "canonical-json-v1"
    assert all(not p.matches(TaskRequest.model_construct(**BASE)) for p in PROFILES[:-1])


@pytest.mark.parametrize(
    ("request_", "runtime", "verifier", "limits"),
    [
        (
            TaskRequest.model_construct(**BASE),
            "canonical-json-v1",
            "canonical-json-verifier-v1",
            {"max_steps": 2, "max_output_bytes": 32768},
        ),
        (
            HostTaskRequest.model_construct(**BASE, operation="host.copy"),
            "host-copy-v1",
            "copy-verifier-v1",
            {"max_steps": 2, "max_tool_milliseconds": 10000, "max_output_bytes": 65536},
        ),
        (
            HostObjectTaskRequest.model_construct(**BASE, operation="host.sha256"),
            "host-sha256-v1",
            "sha256-verifier-v1",
            {"max_steps": 2, "max_tool_milliseconds": 10000, "max_output_bytes": 65536},
        ),
    ],
)
def test_request_maps_to_the_pre_registry_runtime(
    request_: TaskRequest, runtime: str, verifier: str, limits: dict[str, int]
) -> None:
    profile = for_request(request_)
    assert (profile.runtime_id, profile.verifier_id) == (runtime, verifier)
    assert profile.limits(request_) == limits


def test_agent_run_and_delegation_limits_follow_the_request() -> None:
    agent = AgentRunRequest.model_construct(**BASE, max_tool_calls=7)
    profile = for_request(agent)
    assert profile.runtime_id == "agent-run-v1" and profile.requires_session
    assert profile.limits(agent)["max_steps"] == 7
    delegation = DelegationTaskRequest.model_construct(
        **BASE,
        children=(ChildWork.model_construct(), ChildWork.model_construct()),
        tool_milliseconds=20000,
        output_bytes=1000,
        concurrent_children=2,
    )
    profile = for_request(delegation)
    assert profile.runtime_id == "delegation-v1" and profile.owns_children
    assert profile.limits(delegation)["max_steps"] == 3
    assert profile.limits(delegation)["max_concurrent_operations"] == 2


def test_cancellation_and_recovery_flags() -> None:
    assert not for_runtime("canonical-json-v1").runtime_cancels_running
    assert for_runtime("host-sha256-v1").runtime_cancels_running
    assert for_runtime("delegation-v1").runtime_cancels
    assert set(runtime_ids(lambda p: p.requires_host)) == {
        "host-sha256-v1",
        "host-copy-v1",
        "delegation-v1",
    }
    assert runtime_ids(lambda p: p.host_inputs) == ("host-sha256-v1", "host-copy-v1")


def test_unknown_runtime_is_an_explicit_fault() -> None:
    with pytest.raises(Fault, match="not installed"):
        for_runtime("shell-v1")
    # The M3 incident agent-task profile was removed (ADR-032).
    with pytest.raises(Fault, match="not installed"):
        for_runtime("incident-agent-v1")
