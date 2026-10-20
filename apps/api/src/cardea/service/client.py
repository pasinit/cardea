"""Typed HTTPX client over the Unix socket or HTTPS (TLS 1.3, explicit CA bundle).

Credentials remain outside requests' JSON bodies.
"""

import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from spiffe.spiffe_id.spiffe_id import SpiffeId
from spiffe.workloadapi.workload_api_client import WorkloadApiClient

from cardea.service.models import (
    AgentRunRequest,
    DelegationTaskRequest,
    HostObjectTaskRequest,
    HostTaskRequest,
    Signal,
    TaskRequest,
    TaskView,
)


class ClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def network_transport(
    url: str, ca_bundle: Path | None, client_cert: tuple[Path, Path] | None
) -> httpx.HTTPTransport:
    """HTTPS transport for ADR-025's network listener; plaintext HTTP is never offered."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ClientError("insecure_transport", 0)
    if ca_bundle is None:
        raise ClientError("ca_bundle_required", 0)
    context = ssl.create_default_context(cafile=str(ca_bundle))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if client_cert:
        context.load_cert_chain(*client_cert)
    return httpx.HTTPTransport(verify=context)


class KernelClient:
    def __init__(
        self,
        target: Path | str,
        credentials: Callable[[], tuple[str, str]],
        *,
        ca_bundle: Path | None = None,
        client_cert: tuple[Path, Path] | None = None,
    ) -> None:
        """`target` is the Unix socket path, or an `https://host:port` URL with a CA bundle."""
        self.credentials = credentials
        if isinstance(target, Path):
            transport = httpx.HTTPTransport(uds=str(target))
            base_url = "http://cardea"
        else:
            transport = network_transport(target, ca_bundle, client_cert)
            base_url = target.rstrip("/")
        self.http = httpx.Client(
            transport=transport,
            base_url=base_url,
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        )
        self.workload: WorkloadApiClient | None = None

    @classmethod
    def for_workload(
        cls,
        target: Path | str,
        endpoint: str,
        identity: str,
        audience: str,
        *,
        ca_bundle: Path | None = None,
        client_cert: tuple[Path, Path] | None = None,
    ) -> "KernelClient":
        workload = WorkloadApiClient(socket_path=endpoint, default_timeout=2)

        def credential() -> tuple[str, str]:
            return "spiffe", workload.fetch_jwt_svid({audience}, SpiffeId(identity)).token

        try:
            client = cls(target, credential, ca_bundle=ca_bundle, client_cert=client_cert)
        except BaseException:
            workload.close()
            raise
        client.workload = workload
        return client

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        kind, token = self.credentials()
        with self.http.stream(
            method,
            "/v0.2" + path,
            headers={"Authorization": "Bearer " + token, "X-Cardea-Auth": kind},
            **kwargs,
        ) as response:
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 262144:
                    raise ClientError("response_too_large", 502)
            try:
                data = httpx.Response(response.status_code, content=bytes(chunks)).json()
            except ValueError:
                raise ClientError("invalid_http_response", response.status_code) from None
            if response.status_code >= 400:
                raise ClientError(str(data.get("code", "request_failed")), response.status_code)
            return data

    def submit(self, request: TaskRequest) -> TaskView:
        return TaskView.model_validate(
            self.request("POST", "/tasks", json=request.model_dump(mode="json"))
        )

    def submit_host(self, request: HostTaskRequest) -> TaskView:
        return TaskView.model_validate(
            self.request("POST", "/host-tasks", json=request.model_dump(mode="json"))
        )

    def submit_host_object(self, request: HostObjectTaskRequest) -> TaskView:
        return TaskView.model_validate(
            self.request("POST", "/host-object-tasks", json=request.model_dump(mode="json"))
        )

    def submit_agent_run(self, request: AgentRunRequest) -> TaskView:
        return TaskView.model_validate(
            self.request("POST", "/agent-runs", json=request.model_dump(mode="json"))
        )

    def submit_delegation(self, request: DelegationTaskRequest) -> TaskView:
        return TaskView.model_validate(
            self.request("POST", "/delegation-tasks", json=request.model_dump(mode="json"))
        )

    def inspect(self, task_id: str) -> TaskView:
        return TaskView.model_validate(self.request("GET", f"/tasks/{task_id}"))

    def control(self, task_id: str, signal: Signal, *, cancel: bool = False) -> TaskView:
        action = "cancel" if cancel else "resume"
        return TaskView.model_validate(
            self.request("POST", f"/tasks/{task_id}/{action}", json=signal.model_dump(mode="json"))
        )

    def close(self) -> None:
        self.http.close()
        if self.workload:
            self.workload.close()
