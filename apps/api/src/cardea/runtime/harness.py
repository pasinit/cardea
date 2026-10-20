"""The agent harness contract (ADR-033).

A harness is the program that runs one model loop for an agent-run attempt. Cardea hands it
instructions, the per-attempt MCP endpoint and its bearer token, the exposed tool names, the
bounds and a model id; the harness returns a `HarnessResult`. The harness owns the provider:
which API it calls, with which credential, is host-owner configuration of that harness and
never part of a request. Every tool the harness may use is served by the MCP endpoint, so
everything it does outside its own model calls is a receipted Cardea operation.
"""

from typing import Annotated, Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HarnessRequest(_Frozen):
    """Input of one harness run: one attempt of an agent run."""

    system_prompt: str
    prompt: str
    # Per-attempt loopback MCP Streamable HTTP endpoint and its bearer token (ADR-027).
    mcp_url: str
    mcp_token: str
    # MCP tool names the endpoint lists; the harness may allow nothing else.
    tool_names: tuple[str, ...]
    # Bounds. The tool-call budget is also enforced by the endpoint itself; turns and cost
    # are enforced by the harness (or by Cardea's metering, ADR-033).
    max_turns: Annotated[int, Field(ge=1)]
    max_cost_usd: Annotated[float, Field(gt=0)]
    timeout_seconds: float
    max_tool_calls: Annotated[int, Field(ge=1)] | None = None
    # Provider model id, already checked against the harness's allowed models. None means
    # the harness's own default.
    model: str | None = None
    # Stable identity of this attempt (task and attempt number); a confined harness's
    # worker job derives from it so recovery can stop it (ADR-028).
    job_key: str = ""


class HarnessResult(_Frozen):
    """Outcome of one harness run.

    `cost_usd` is None when neither the harness nor Cardea's metering could price the run.
    `failure_reason` is a short machine-readable code when `success` is false.
    """

    text: str
    success: bool
    turns: Annotated[int, Field(ge=0)] = 0
    cost_usd: float | None = None
    failure_reason: str | None = None
    # The harness's own stop detail (for example the provider's stop reason).
    stop_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data: Any) -> Any:
        # Earlier adapters reported `is_error` plus a `stop_reason` doubling as the failure
        # code; accept that shape so older confined workers and callers keep working.
        if isinstance(data, dict) and "is_error" in data:
            data = dict(data)
            failed = bool(data.pop("is_error"))
            data.setdefault("success", not failed)
            if failed and data.get("failure_reason") is None:
                data["failure_reason"] = str(data.get("stop_reason") or "error")
        return data

    @model_validator(mode="after")
    def _reason(self) -> Self:
        if self.success and self.failure_reason is not None:
            raise ValueError("A successful run has no failure reason")
        if not self.success and not self.failure_reason:
            raise ValueError("A failed run needs a failure reason")
        return self

    def __eq__(self, other: object) -> bool:
        # Results compare by content, so a subclass built from legacy fields still matches.
        if not isinstance(other, HarnessResult):
            return NotImplemented
        return self.model_dump() == other.model_dump()

    __hash__ = None  # type: ignore[assignment]

    @property
    def is_error(self) -> bool:
        return not self.success

    @classmethod
    def failed(cls, reason: str, text: str = "") -> "HarnessResult":
        return HarnessResult(text=text, success=False, failure_reason=reason, stop_reason=reason)


class Harness(Protocol):
    """One configured harness. Implementations must not expose any tool but the endpoint's."""

    async def run(self, request: HarnessRequest) -> HarnessResult: ...
