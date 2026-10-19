"""R02: test the actual Unix socket rather than only an in-process ASGI client."""

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def socket_client() -> Iterator[httpx.Client]:
    # /tmp keeps the macOS sockaddr_un path below its length limit.
    with tempfile.TemporaryDirectory(prefix="cardea-fit-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "api.sock")
        api_dir = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONPATH=str(api_dir))
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.standards.socket_app:app",
                    "--uds",
                    socket_path,
                    "--no-access-log",
                    "--log-level",
                    "warning",
                ],
                cwd=api_dir,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=socket_path),
                    base_url="http://cardea.local",
                    timeout=2,
                ) as client:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            log.seek(0)
                            pytest.fail(log.read().decode())
                        try:
                            if client.get("/openapi.json").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            pytest.fail("Unix-socket fixture failed to start within 10 seconds")
                        time.sleep(0.05)
                    yield client
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_openapi_31_and_auth_hook_work_over_unix_socket(socket_client: httpx.Client) -> None:
    schema = socket_client.get("/openapi.json").json()
    assert schema["openapi"] == "3.1.0"
    assert schema["components"]["schemas"]["Echo"]["additionalProperties"] is False
    assert schema["paths"]["/fit/echo"]["post"]["security"] == [{"HTTPBearer": []}]
    assert socket_client.post("/fit/echo", json={"value": "hello"}).status_code == 401
    response = socket_client.post(
        "/fit/echo",
        json={"value": "hello"},
        headers={"Authorization": "Bearer fictional-fit-proof"},
    )
    assert response.status_code == 200
    assert response.json() == {"value": "hello"}


@pytest.mark.parametrize(
    "body",
    [
        {"value": "hello", "actor_id": "supervisor"},
        {"value": "x" * 65},
        {"value": 17},
    ],
)
def test_wire_rejects_identity_overrides_and_invalid_fields(
    socket_client: httpx.Client,
    body: dict[str, object],
) -> None:
    response = socket_client.post(
        "/fit/echo",
        json=body,
        headers={"Authorization": "Bearer fictional-fit-proof"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]
