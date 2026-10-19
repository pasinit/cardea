"""Test-only daemon wrapper; fixtures cannot be enabled through production configuration."""

import json
import os
import time
from pathlib import Path

import httpx
from cardea.runtime.openrouter import OpenRouterModel
from cardea.service import server
from cardea.service.database import private_file
from cardea.service.models import ServiceConfig

config = ServiceConfig.model_validate_json(private_file(Path(os.environ["M1_CONFIG"])))


def response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    context = json.loads(body["messages"][-1]["content"])["authorized_input"]
    return httpx.Response(
        200,
        json={
            "id": "fixture-response",
            "created": 1,
            "model": "fixture/model",
            "object": "chat.completion",
            "system_fingerprint": None,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "logprobs": None,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture-call",
                                "type": "function",
                                "function": {
                                    "name": "compile_incidents",
                                    "arguments": json.dumps({"batch": context}),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
        },
    )


class FixtureModel(OpenRouterModel):
    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(api_key, model, transport=httpx.MockTransport(response))


if os.environ.get("M3_FIXTURE") == "1":
    server.OpenRouterModel = FixtureModel


def barrier(stage: str) -> None:
    if stage != os.environ.get("M1_BARRIER"):
        return
    (config.data_dir / f"{os.getpid()}.{stage}.entered").touch()
    end = time.monotonic() + 60
    while not (config.data_dir / f"{os.getpid()}.{stage}.release").exists():
        if time.monotonic() > end:
            raise RuntimeError("fixture barrier timeout")
        time.sleep(0.02)


server.serve(config, fault=barrier)
