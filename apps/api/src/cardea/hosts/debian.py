"""Unprivileged client of the fixed sudo helper; no caller-controlled command line."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from cardea.hosts import launcher
from cardea.service.database import Fault


class Host(Protocol):
    def probe(self) -> None: ...
    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None: ...
    def status(self, job: str) -> dict[str, Any]: ...
    def stop(self, job: str) -> dict[str, Any]: ...
    def cleanup(self, job: str) -> dict[str, Any]: ...


class WorkerHost(Protocol):
    """Confined workers under host-owner profiles (ADR-028); callers name, never configure."""

    def profiles(self) -> dict[str, Any]: ...
    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None: ...
    def status(self, job: str) -> dict[str, Any]: ...
    def stop(self, job: str) -> dict[str, Any]: ...
    def cleanup(self, job: str) -> dict[str, Any]: ...


# Starts run under the kernel's SQLite write lock (dispatch must serialize with policy
# mutation), so they must give up before other writers' 10 s busy_timeout expires.
START_TIMEOUT = 8
CALL_TIMEOUT = 12
# Base64 of the helper's largest output plus framing.
MAX_RESPONSE = 4 * launcher.MAX_OUTPUT // 3 + 65536


class DebianHost:
    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        starting = request.get("action") in {"start", "start_confined"}
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "--", "/usr/libexec/cardea-host"],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=START_TIMEOUT if starting else CALL_TIMEOUT,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                check=False,
            )
            if len(result.stdout) > MAX_RESPONSE:
                raise ValueError("oversized helper response")
            response: dict[str, Any] = json.loads(result.stdout)
            if result.returncode:
                reason = response.get("error")
                if response.get("rejected") is True and isinstance(reason, str):
                    # The helper refused before its no-relaunch fence: nothing started.
                    raise Fault("host_rejected", f"Host refused the job: {reason[:64]}", 409)
                raise ValueError("helper failed")
            return response
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise Fault("host_unavailable", "Host result could not be established", 503) from error

    def probe(self) -> None:
        result = self.call({"action": "probe"})
        digest = hashlib.sha256(Path(launcher.__file__).read_bytes()).hexdigest()
        if result != {"profile": launcher.PROFILE, "source_sha256": digest}:
            raise Fault("host_profile_mismatch", "Installed helper differs from tested source", 503)

    def profiles(self) -> dict[str, Any]:
        return self.call({"action": "profiles"})

    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None:
        self.call(
            {
                "action": "start",
                "job": job,
                "operation": operation,
                "deadline": deadline,
                "input": base64.b64encode(content).decode(),
            }
        )

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        self.call(
            {
                "action": "start_confined",
                "job": job,
                "profile": profile,
                "worker": worker,
                "deadline": deadline,
                "input": base64.b64encode(content).decode(),
            }
        )

    def status(self, job: str) -> dict[str, Any]:
        return self.call({"action": "status", "job": job})

    def stop(self, job: str) -> dict[str, Any]:
        return self.call({"action": "stop", "job": job})

    def cleanup(self, job: str) -> dict[str, Any]:
        return self.call({"action": "cleanup", "job": job})
