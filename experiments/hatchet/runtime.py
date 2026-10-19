"""Explicit local connection data; never fall back to ambient Hatchet credentials."""

import json
from pathlib import Path

from hatchet_sdk import Hatchet
from hatchet_sdk.config import ClientConfig, ClientTLSConfig, HealthcheckConfig
from pydantic import BaseModel


class Input(BaseModel):
    root: str
    scenario: str
    scope: str
    value: int = 1
    operation_key: str = "operation"


def client(path: Path, namespace: str) -> Hatchet:
    data = json.loads(path.read_text())
    return Hatchet(
        config=ClientConfig(
            token=data["token"],
            tenant_id=data["tenant_id"],
            host_port=data["grpc_address"],
            server_url=data["api_url"],
            namespace=namespace,
            tls_config=ClientTLSConfig(strategy="none"),
            healthcheck=HealthcheckConfig(enabled=False),
            disable_log_capture=True,
        )
    )
