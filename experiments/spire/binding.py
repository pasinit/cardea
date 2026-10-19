"""Experiment-only authentication adapter and trusted binding fixture; no grants."""

from dataclasses import dataclass
from time import time

from spiffe.workloadapi.workload_api_client import WorkloadApiClient

AUDIENCE = "cardea-kernel-fit"
DOMAIN = "cardea.test"
RUNTIME = f"spiffe://{DOMAIN}/runtime/a"


class Denied(Exception):
    """Bounded rejection with no credential or provider exception in its text."""


@dataclass(frozen=True)
class Execution:
    task: str
    runtime: str
    agent: str
    supervisor: str
    ancestors: tuple[str, ...]
    lease: str
    expires: float


class Bindings:
    """Admin-provisioned fixture state, not a production repository/policy engine."""

    def __init__(self, socket: str) -> None:
        self.client = WorkloadApiClient(socket_path=socket, default_timeout=0.5)
        self.identities = {RUNTIME: "runtime-a"}
        self.disabled: set[str] = set()
        self.executions: dict[str, Execution] = {}

    def authenticate(self, token: str) -> str:
        if not token or len(token) > 16384:
            raise Denied("invalid_proof")
        try:
            # Provider signature/claim validation must succeed; no offline fallback.
            svid = self.client.validate_jwt_svid(token, AUDIENCE)
        except Exception:
            raise Denied("proof_validation_failed") from None
        if str(svid.spiffe_id.trust_domain) != DOMAIN:
            raise Denied("untrusted_domain")
        principal = self.identities.get(str(svid.spiffe_id))
        if principal is None or principal in self.disabled:
            raise Denied("principal_unavailable")
        return principal

    def resolve(self, token: str, task: str, lease: str) -> Execution:
        principal = self.authenticate(token)
        execution = self.executions.get(task)
        if execution is None or execution.runtime != principal:
            raise Denied("execution_mismatch")
        if execution.lease != lease or execution.expires <= time():
            raise Denied("stale_lease")
        if any(
            p in self.disabled
            for p in (execution.agent, execution.supervisor, *execution.ancestors)
        ):
            raise Denied("binding_disabled")
        # Returning a binding authorizes no operation. Live policy intersection is still required.
        return execution

    def close(self) -> None:
        self.client.close()
