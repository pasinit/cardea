"""Fixed installed operation descriptions and independent result verification."""

import base64
import hashlib
from typing import Any

OPERATIONS = {
    "host.sha256": "Compute SHA-256 and byte count of declared input",
    "host.copy": "Copy declared bytes into a verified task-owned base64 artifact",
}


def expected(operation: str, source: bytes) -> dict[str, Any]:
    if operation == "host.sha256":
        return {"sha256": hashlib.sha256(source).hexdigest(), "bytes": len(source)}
    if operation == "host.copy":
        return {"content_base64": base64.b64encode(source).decode(), "bytes": len(source)}
    raise ValueError("Unknown installed operation")
