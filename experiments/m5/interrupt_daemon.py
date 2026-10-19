"""Test-only barrier after a real host start; never included in the package."""

import os
import threading
from pathlib import Path

from cardea.service.models import ServiceConfig
from cardea.service.server import serve


def barrier(stage: str) -> None:
    if stage == "host.started":
        path = Path("/var/lib/cardea/m5-host-started")
        with path.open("w") as stream:
            stream.write(stage)
            stream.flush()
            os.fsync(stream.fileno())
        threading.Event().wait()


if __name__ == "__main__":
    assert Path("/etc/cardea-disposable-m2-vm").read_text().strip() == "yes"
    serve(
        ServiceConfig.model_validate_json(Path("/etc/cardea/config.json").read_bytes()),
        barrier,
    )
