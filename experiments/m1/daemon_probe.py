"""Subprocess-only fault injection; the installed daemon exposes no fault controls."""

import os
import time
from pathlib import Path

from cardea.service.database import private_file
from cardea.service.models import ServiceConfig
from cardea.service.server import serve

config = ServiceConfig.model_validate_json(private_file(Path(os.environ["M1_CONFIG"])))


def barrier(stage: str) -> None:
    if stage != os.environ.get("M1_BARRIER"):
        return
    root = config.data_dir
    (root / f"{os.getpid()}.{stage}.entered").touch()
    deadline = time.monotonic() + 45
    while not (root / f"{os.getpid()}.{stage}.release").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("fault barrier timed out")
        time.sleep(0.02)


serve(config, fault=barrier)
