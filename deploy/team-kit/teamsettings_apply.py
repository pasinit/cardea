"""Confirm and apply a team settings change through the team kit (render, provision, apply).

    python3 -I deploy/team-kit/teamsettings_apply.py --host HOST.json --settings CHANGE.json
    python3 -I deploy/team-kit/teamsettings_apply.py --host HOST.json --settings CHANGE.json \\
        --confirm TOKEN

Without `--confirm` the command prints the preview and the confirmation token and changes
nothing. The token is a digest of the exact changes the preview showed against the host file as
it is now, so confirming applies what was reviewed: if the file moved on, the token no longer
matches and the change is refused.

A confirmed change is applied the way the kit applies a team:
1. the new host file is rendered and validated beside the old one (`render` into a scratch
   directory, so a document the kit cannot render never reaches the file);
2. the supervisor's authority is read live from the kernel (the kernel is the only judge; this
   module holds no principal and caches no permission);
3. the host file is replaced, `provision` runs with it, and `apply` puts the collections and
   the group to the kernel as the supervisor, where the kernel checks the supervisor's live
   authority for every write. If provisioning or the kernel refuses, the old host file is put
   back and the old team provisioned again, and the refusal is reported;
4. a pause or resume goes through the kit's own `pause` and `resume`.

Every attempt is appended to an audit file beside the provisioning file: who, when, and each
field's before and after, written before the change (a change that cannot be recorded is not
made) and again with the outcome. Goals text is recorded by size and digest, never in full.
Re-applying a change that is already in effect changes and records nothing.

Secrets are outside the settings (`teamsettings.validate` refuses them). The audit, results and
errors hold only validated setting fields; a failure is reported by exception type and kernel
error code, never by the message of a lower layer.
"""

import argparse
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

MAX_WHO = 128
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ApplyError(RuntimeError):
    """The change was not applied; the message says why and holds no secret."""


def _load(name: str, sibling: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{sibling}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return sys.modules[name]


def _modules() -> tuple[ModuleType, ModuleType]:
    return _load("teamkit_for_settings", "teamkit"), _load("teamsettings_for_apply", "teamsettings")


def token(changes: list[dict[str, Any]]) -> str:
    """The confirmation token for a preview's changes."""
    return hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()


def audit_path(kit: ModuleType, host: dict[str, Any]) -> Path:
    path: Path = kit.pause_record_path(host)
    return path.with_name(f"{kit._names(host)[1]}.settings-audit.jsonl")


def _digest(text: object) -> dict[str, Any]:
    raw = str(text).encode()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def audit_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The changes as the audit keeps them: goals by size and digest, everything else as is."""
    out = []
    for change in changes:
        hide = change["setting"] == "product_goals"
        out.append(
            {
                "setting": change["setting"],
                "field": change["field"],
                "before": (None if change["from"] is None else _digest(change["from"]))
                if hide
                else change["from"],
                "after": (None if change["to"] is None else _digest(change["to"]))
                if hide
                else change["to"],
            }
        )
    return out


def _who(host: dict[str, Any], by: str | None) -> str:
    name = _CONTROL.sub(" ", by or str(host["supervisor"]["principal_id"])).strip()
    return name[:MAX_WHO] or str(host["supervisor"]["principal_id"])


def _record(path: Path, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def read_audit(kit: ModuleType, host: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        lines = audit_path(kit, host).read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One settings change at a time per team."""
    fd = os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _failure(error: Exception) -> str:
    code = getattr(error, "code", None)
    return type(error).__name__ + (f" ({code})" if isinstance(code, str) else "")


def _write_host(path: Path, document: dict[str, Any]) -> None:
    mode = path.stat().st_mode & 0o777
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    staged.write_text(json.dumps(document, indent=2) + "\n")
    staged.chmod(mode)
    os.replace(staged, path)


def apply_settings(
    host_path: Path,
    settings: object,
    *,
    confirm: str | None = None,
    by: str | None = None,
    connect: Callable[[dict[str, Any]], Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Preview `settings` and, when `confirm` is the preview's token, apply them.

    Returns `{"status", "confirm", "preview", "audit_id"}`; status is `needs_confirmation` (no
    token, or a wrong one: nothing changed), `unchanged` (already in effect: nothing changed or
    recorded) or `applied`. Raises `ApplyError` when the kit, the kernel or the audit refuses;
    the host file is then as it was."""
    kit, model = _modules()
    connect = connect or kit.client
    host = kit.load_host(host_path)
    with _locked(audit_path(kit, host)):
        paused = bool(kit.read_pause_record(host))
        result = model.preview(host_path, settings, paused=paused)
        wanted = model.validate(settings)
        changes = result["changes"]
        out: dict[str, Any] = {"preview": result, "confirm": token(changes), "audit_id": None}
        if result["problems"]:
            raise ApplyError("the change would be refused: " + "; ".join(result["problems"]))
        if not changes:
            return {**out, "status": "unchanged"}
        if not confirm or not hmac.compare_digest(confirm, out["confirm"]):
            return {**out, "status": "needs_confirmation"}
        fields = {c["setting"] for c in changes}
        others = fields - {"paused"}
        if paused and others and wanted.get("paused", {}).get("paused") is not False:
            raise ApplyError("the team is paused: resume it first, or resume in the same change")

        original = host_path.read_bytes()
        raw = json.loads(original)
        new_raw = model.apply_to(raw, {k: v for k, v in wanted.items() if k != "paused"})
        audit = audit_path(kit, host)
        entry = {
            "id": uuid.uuid4().hex,
            "at": now().isoformat(timespec="seconds"),
            "by": _who(host, by),
            "team": f"{host['namespace']}/{kit._names(host)[1]}",
            "changes": audit_changes(changes),
        }
        try:
            _record(audit, {**entry, "outcome": "started"})
        except OSError as error:
            raise ApplyError(
                f"the audit cannot be written, nothing changed: {_failure(error)}"
            ) from None
        out["audit_id"] = entry["id"]
        outcome = "applied"
        try:
            _make(kit, host, host_path, new_raw, wanted, paused, others, connect)
        except Exception as error:
            outcome = "refused"
            problem = _failure(error)
            restored = _restore(kit, host_path, original, paused)
            _record(audit, {**entry, "outcome": outcome, "error": problem, "restored": restored})
            raise ApplyError(
                f"not applied ({problem}); "
                + (
                    "the old settings are back"
                    if restored
                    else "the old settings could NOT be restored: check the host file, re-provision"
                )
            ) from None
        _record(audit, {**entry, "at": now().isoformat(timespec="seconds"), "outcome": outcome})
        return {**out, "status": "applied"}


def _make(
    kit: ModuleType,
    host: dict[str, Any],
    host_path: Path,
    new_raw: dict[str, Any],
    wanted: dict[str, Any],
    paused: bool,
    others: set[str],
    connect: Callable[[dict[str, Any]], Any],
) -> None:
    if others:
        # Render the new document first, so one the kit cannot render never reaches the file.
        with tempfile.TemporaryDirectory() as scratch:
            staged = Path(scratch) / "host.json"
            staged.write_text(json.dumps(new_raw))
            staged.chmod(0o600)
            candidate = kit.load_host(staged)
            kit.render(candidate, Path(scratch) / "rendered")
        if not paused:
            _authority(kit, host, connect)
        _write_host(host_path, new_raw)
    changed = kit.load_host(host_path)
    resume = wanted.get("paused", {}).get("paused") is False
    if resume:
        kit.resume(changed)
    elif others and not paused:
        kit.provision(changed, paused=False)
    if others:
        kit.apply(changed, connect)
    if wanted.get("paused", {}).get("paused") is True:
        kit.pause(changed, wanted["paused"].get("reason"))


def _authority(kit: ModuleType, host: dict[str, Any], connect: Callable[..., Any]) -> None:
    """The supervisor's live standing, read from the kernel before anything is changed. The
    kernel decides; a supervisor it refuses stops the change here."""
    _, group, _, _ = kit._names(host)
    kernel = connect(host)
    try:
        kernel.request("GET", f"/groups/{group}/overview", params={"namespace": host["namespace"]})
    finally:
        kernel.close()


def _restore(kit: ModuleType, host_path: Path, original: bytes, paused: bool) -> bool:
    """Put the old host file back, byte for byte, and provision the old team again; nothing
    is done if the file was never replaced. True when the old settings are in force."""
    try:
        if host_path.read_bytes() != original:
            staged = host_path.with_name(f".{host_path.name}.{os.getpid()}")
            staged.write_bytes(original)
            staged.chmod(host_path.stat().st_mode & 0o777)
            os.replace(staged, host_path)
            kit.provision(kit.load_host(host_path), paused=paused)
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", type=Path, required=True, help="the private host file")
    parser.add_argument("--settings", type=Path, required=True, help="JSON of the settings to set")
    parser.add_argument("--confirm", help="the token a preview printed")
    args = parser.parse_args(argv)
    _, model = _modules()
    try:
        found = apply_settings(
            args.host, json.loads(args.settings.read_text()), confirm=args.confirm
        )
    except (ApplyError, model.SettingsError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print("\n".join(model.render(found["preview"])))
    if found["status"] == "needs_confirmation":
        print(
            f"nothing changed; to apply exactly this, run again with --confirm {found['confirm']}"
        )
        return 2
    print("already in effect; nothing changed" if found["status"] == "unchanged" else "applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
