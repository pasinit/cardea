"""Confirm and apply a change to the team settings (no UI): the second half of `teamsettings.py`.

    preview = TeamSettings(host_path).preview(settings)       # shows the change and `confirm`
    TeamSettings(host_path).apply(settings, confirm)           # changes it, only with `confirm`

A change is applied only when the caller returns the `confirm` token its preview gave. The token
is a digest of the exact changes (and the team), so a change that differs from what was shown,
or a host file that changed since, is refused. Nothing is written without it.

An apply runs the kit's own path, so nothing new can reach the kernel: the host file is staged
and validated, the kernel checks the supervisor's live authority (`PUT /groups/{name}` with
`dry_run=true`, which changes nothing), then the host file is replaced and the team is provisioned
(`teamkit.provision`, which renders the provisioning fragment) and applied (`teamkit.apply`).
If a later step fails the host file is put back and the team provisioned from it again.

Resume is not done here: `resume_by_host_owner`. A paused team is not changed here either.

Every change that reaches the host file is appended to a private audit file beside the pause
record (`<group>.settings-audit.jsonl`, mode 0600, append-only): who (the supervisor principal),
when, and the before and after of each changed setting. Goals text is recorded by size and
digest. Only validated setting values are ever recorded or logged; the host file's credentials
and the kernel socket are not read into any message, log or audit entry.
"""

import hashlib
import importlib.util
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

log = logging.getLogger("teamapply")
_LOCK = threading.Lock()


class ApplyError(Exception):
    """A refusal with a stable `code` and HTTP `status`; `detail` is safe to show."""

    def __init__(self, code: str, status: int, detail: str | list[str] | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


def _settings_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "teamsettings_for_apply", Path(__file__).with_name("teamsettings.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("teamsettings_for_apply", module)
    spec.loader.exec_module(module)
    return sys.modules["teamsettings_for_apply"]


def confirmation(host: dict[str, Any], changes: list[dict[str, Any]]) -> str:
    """The token that confirms exactly these changes to this team."""
    document = {"namespace": host["namespace"], "group": host.get("group"), "changes": changes}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def audit_path(kit: ModuleType, host: dict[str, Any]) -> Path:
    _, group, _, _ = kit._names(host)
    return kit.pause_record_path(host).with_name(f"{group}.settings-audit.jsonl")


def _recorded(change: dict[str, Any]) -> dict[str, Any]:
    """A change as the audit keeps it: goals text by size and digest, never in full."""
    if change["setting"] != "product_goals":
        return dict(change)

    def stand_in(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return {"bytes": len(value.encode()), "sha256": hashlib.sha256(value.encode()).hexdigest()}

    return {**change, "from": stand_in(change["from"]), "to": stand_in(change["to"])}


class TeamSettings:
    """Preview and apply settings for the team named by one host file. Holds no principal:
    the kernel checks the supervisor's authority on every apply."""

    def __init__(
        self,
        host_path: Path,
        connect: Callable[[dict[str, Any]], Any] | None = None,
        *,
        provision: Callable[..., Any] | None = None,
        pause: Callable[..., Any] | None = None,
        apply_team: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = Path(host_path)
        self.settings = _settings_module()
        self.kit = self.settings._kit()
        self.connect = connect or self.kit.client
        self.provision = provision or self.kit.provision
        self.pause = pause or self.kit.pause
        self.apply_team = apply_team or self.kit.apply
        self.clock = clock

    def preview(self, settings: object) -> dict[str, Any]:
        """The settings module's preview, plus `confirm` when there is a change to confirm."""
        try:
            result: dict[str, Any] = self.settings.preview(self.path, settings)
            host = self.kit.load_host(self.path)
        except (self.settings.SettingsError, self.kit.HostError) as error:
            raise ApplyError("invalid_settings", 422, str(error)) from None
        ready = result["changes"] and not result["problems"]
        result["confirm"] = confirmation(host, result["changes"]) if ready else None
        return result

    def apply(self, settings: object, confirm: object) -> dict[str, Any]:
        with _LOCK:
            return self._apply(settings, confirm)

    def _apply(self, settings: object, confirm: object) -> dict[str, Any]:
        try:
            wanted = self.settings.validate(settings)
        except self.settings.SettingsError as error:
            raise ApplyError("invalid_settings", 422, str(error)) from None
        if wanted.get("paused", {}).get("paused") is False:
            raise ApplyError("resume_by_host_owner", 403)
        result = self.preview(wanted)
        if result["problems"]:
            raise ApplyError("settings_refused", 422, result["problems"])
        if not result["changes"]:
            return {"change": "unchanged", "changes": [], "unchanged": result["unchanged"]}
        if not isinstance(confirm, str) or confirm != result["confirm"]:
            raise ApplyError("confirmation_required", 428)
        kit = self.kit
        if kit.read_pause_record(kit.load_host(self.path)):
            raise ApplyError("team_paused", 409)

        original = self.path.read_bytes()
        document = json.loads(original)
        changed = self.settings.apply_to(document, wanted)
        pausing = wanted.get("paused", {}).get("paused") is True
        staged = self.path.with_name(f".{self.path.name}.{os.getpid()}.staged")
        who = str(document["supervisor"]["principal_id"])
        audit = self._open_audit(kit.load_host(self.path))
        try:
            fresh = self._stage(staged, changed, self.path.stat().st_mode & 0o777, pausing)
            self._authorise(fresh)
            if changed != document:
                os.replace(staged, self.path)
            try:
                self._provision(fresh, pausing, wanted)
            except Exception as error:
                self._revert(original)
                self._record(audit, who, "reverted", result["changes"])
                code = getattr(error, "code", None)
                raise ApplyError("apply_failed", 502, str(code) if code else None) from None
            self._record(audit, who, "applied", result["changes"])
        finally:
            staged.unlink(missing_ok=True)
            os.close(audit)
        log.info("team settings applied: %s", ", ".join(c["field"] for c in result["changes"]))
        return {"change": "applied", "changes": result["changes"], "unchanged": result["unchanged"]}

    def _stage(
        self, staged: Path, changed: dict[str, Any], mode: int, pausing: bool
    ) -> dict[str, Any]:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(changed, indent=2) + "\n")
        try:
            fresh: dict[str, Any] = self.kit.load_host(staged)
            self.kit.check_placeholders(fresh)
            if not pausing:
                self.kit.check_walls(fresh)
        except (self.kit.HostError, ValueError) as error:
            raise ApplyError("settings_refused", 422, str(error)) from None
        return fresh

    def _authorise(self, fresh: dict[str, Any]) -> None:
        """The kernel's own check, live: the supervisor may manage this group. A dry run
        changes nothing; a refusal reaches the caller with the kernel's code."""
        _, group, _, _ = self.kit._names(fresh)
        kernel = self.connect(fresh)
        try:
            kernel.request(
                "PUT",
                f"/groups/{group}",
                params={"dry_run": True},
                json=self.kit.group_document(fresh),
            )
        finally:
            kernel.close()

    def _provision(self, fresh: dict[str, Any], pausing: bool, wanted: dict[str, Any]) -> None:
        if pausing:
            self.pause(fresh, wanted["paused"].get("reason"))
            return
        self.provision(fresh, paused=False)
        self.apply_team(fresh, self.connect)

    def _revert(self, original: bytes) -> None:
        """Put the host file back and provision from it, best effort."""
        restore = self.path.with_name(f".{self.path.name}.{os.getpid()}.restore")
        try:
            fd = os.open(restore, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
            os.replace(restore, self.path)
            self.provision(self.kit.load_host(self.path), paused=False)
        except Exception:
            log.error("team settings: the host file was restored but provisioning was not")
        finally:
            restore.unlink(missing_ok=True)

    def _open_audit(self, host: dict[str, Any]) -> int:
        """The audit file, open for append before anything changes: no audit, no change."""
        path = audit_path(self.kit, host)
        try:
            return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            raise ApplyError("audit_unavailable", 500) from None

    def _record(self, fd: int, who: str, outcome: str, changes: list[dict[str, Any]]) -> None:
        entry = {
            "at": self.clock().isoformat(timespec="seconds"),
            "who": who,
            "outcome": outcome,
            "changes": [_recorded(c) for c in changes],
        }
        os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
        os.fsync(fd)
