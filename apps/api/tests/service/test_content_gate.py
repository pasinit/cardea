"""The content gate (ADR-037 decision 4): what may leave the host.

Scanner tests need the pinned Gitleaks binary (`packaging/fetch_gitleaks.py`) in
`CARDEA_TEST_GITLEAKS`; CI sets it and `CARDEA_REQUIRE_GITLEAKS=1`. Every other test uses a
stand-in scanner that reports nothing, so the denylist and the refusal path run anywhere.
Fake secrets are assembled at runtime, so no secret-shaped literal is committed.
"""

import hashlib
import json
import os
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pydantic import JsonValue
from test_records import FakeClient

from cardea.kernel.store import StoreError
from cardea.service import content_gate, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.auth import Authentication, Proof
from cardea.service.content_gate import Denylist, Gate, added_lines, text_lines
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    ContentGateConfig,
    OperationTaskRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationContext, OperationDescriptor, Registry

GITLEAKS = os.environ.get("CARDEA_TEST_GITLEAKS")
needs_gitleaks = pytest.mark.skipif(
    not GITLEAKS and os.environ.get("CARDEA_REQUIRE_GITLEAKS") != "1",
    reason="set CARDEA_TEST_GITLEAKS to the pinned Gitleaks binary",
)
# A fake access key id, assembled at runtime in the shape Gitleaks's `aws-access-token`
# rule looks for. It is not a credential.
FAKE_KEY = "AKIA" + "Z" * 4 + "7" * 4 + "QWERTYUI"
# A denylist term, as a host owner would list a private project or a person.
TERM = "Project" + "-Nightjar"

DIFF = """diff --git a/app/settings.py b/app/settings.py
--- a/app/settings.py
+++ b/app/settings.py
@@ -1,3 +1,4 @@
 import os
-DEBUG = True
+DEBUG = False
+REGION = "eu-west-1"
 TIMEOUT = 30
@@ -10,2 +11,3 @@ def load():
     return os.environ
+    # cached
diff --git a/docs/notes.md b/docs/notes.md
new file mode 100644
--- /dev/null
+++ b/docs/notes.md
@@ -0,0 +1,2 @@
+# Notes
+Nothing secret here.
"""


def private(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def silent_scanner(directory: Path) -> tuple[Path, str]:
    """A stand-in for the Gitleaks binary that reads its input and reports no finding."""
    script = directory / "no-findings"
    script.write_text("#!/bin/sh\ncat >/dev/null\necho '[]'\n")
    script.chmod(0o700)
    return script, hashlib.sha256(script.read_bytes()).hexdigest()


def config(directory: Path, **changes: Any) -> ContentGateConfig:
    if GITLEAKS and changes.pop("real", False):
        binary = Path(GITLEAKS)
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    else:
        changes.pop("real", None)
        binary, digest = silent_scanner(directory)
    return ContentGateConfig.model_validate(
        {"gitleaks_binary": str(binary), "gitleaks_sha256": digest, **changes}
    )


# Inputs


def test_only_added_lines_are_scanned_and_located() -> None:
    assert added_lines(DIFF) == [
        ("app/settings.py:2", "DEBUG = False"),
        ("app/settings.py:3", 'REGION = "eu-west-1"'),
        ("app/settings.py:12", "    # cached"),
        ("docs/notes.md:1", "# Notes"),
        ("docs/notes.md:2", "Nothing secret here."),
    ]
    with pytest.raises(Fault) as malformed:
        added_lines("+an added line with no file or hunk\n")
    assert malformed.value.code == "invalid_diff"


def test_text_fields_are_located_by_field_and_line() -> None:
    assert text_lines({"title": "Fix login", "body": "Line one\nLine two"}) == [
        ("title", "Fix login"),
        ("body:1", "Line one"),
        ("body:2", "Line two"),
    ]


# The denylist


def test_the_denylist_matches_terms_and_patterns_case_insensitively(tmp_path: Path) -> None:
    listed = private(
        tmp_path / "denylist",
        f"# private names\n{TERM}\n\nre:internal-[0-9]{{4}}\n",
    )
    denylist = Denylist.load(listed)
    found = denylist.check(
        [("title", f"About {TERM.upper()}"), ("body:2", "see INTERNAL-2026"), ("body:3", "ok")]
    )
    assert [(f.rule, f.location) for f in found] == [
        ("denylist:2", "title"),
        ("denylist:4", "body:2"),
    ]


@pytest.mark.parametrize(
    "content",
    ["re:unclosed(\n", "re:.*\n", "x" * 600 + "\n", "\udcff"],
    ids=["invalid-regex", "matches-everything", "too-long", "not-utf8"],
)
def test_a_malformed_denylist_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "denylist"
    path.write_bytes(content.encode("utf-8", "surrogateescape"))
    path.chmod(0o600)
    with pytest.raises(Fault) as refused:
        Denylist.load(path)
    assert refused.value.code == "content_gate_unavailable"
    # The message names the line at most, never what is on it.
    assert "unclosed" not in refused.value.message and "x" * 20 not in refused.value.message


def test_a_readable_denylist_must_be_private(tmp_path: Path) -> None:
    path = private(tmp_path / "denylist", f"{TERM}\n")
    path.chmod(0o644)
    with pytest.raises(Fault) as refused:
        Denylist.load(path)
    assert refused.value.code == "content_gate_unavailable"


# The scanner


def test_the_scanner_must_be_the_pinned_binary(tmp_path: Path) -> None:
    pinned = config(tmp_path)
    tampered = pinned.model_copy(update={"gitleaks_sha256": "0" * 64})
    with pytest.raises(Fault) as refused:
        Gate.load(tampered)
    assert refused.value.code == "content_gate_unavailable"
    missing = pinned.model_copy(update={"gitleaks_binary": tmp_path / "absent"})
    with pytest.raises(Fault):
        Gate.load(missing)


@needs_gitleaks
def test_gitleaks_finds_a_secret_in_an_added_line_and_ignores_inline_exemptions(
    tmp_path: Path,
) -> None:
    assert GITLEAKS, "CARDEA_REQUIRE_GITLEAKS=1 but CARDEA_TEST_GITLEAKS is not set"
    gate = Gate.load(config(tmp_path, real=True))
    leaked = DIFF.replace('+REGION = "eu-west-1"', f"+AWS_KEY = '{FAKE_KEY}'  # gitleaks:allow")
    found = gate.check(leaked)
    assert [(f.rule, f.location) for f in found] == [("aws-access-token", "app/settings.py:3")]
    assert gate.check(DIFF) == []
    texts = gate.check(texts={"title": "Rotate keys", "body": f"Old key:\n{FAKE_KEY}"})
    assert [(f.rule, f.location) for f in texts] == [("aws-access-token", "body:2")]


# The check operations call


SEND = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "vcs.push_branch",
            "description": "A fake push: records what it would send, after the content gate.",
            "inputSchema": {
                "type": "object",
                "properties": {"diff": {"type": "string"}, "message": {"type": "string"}},
                "required": ["diff", "message"],
            },
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.push"},
    }
)


class Remote:
    """What the fake repository host received."""

    def __init__(self) -> None:
        self.pushed: list[dict[str, JsonValue]] = []

    async def push(self, arguments: dict[str, JsonValue], context: OperationContext) -> Any:
        assert context.check_content is not None, "the kernel binds the content gate"
        context.check_content(
            operation="vcs.push_branch",
            diff=str(arguments["diff"]),
            texts={"commit_message": str(arguments["message"])},
        )
        self.pushed.append(arguments)
        return {"pushed": True}


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        return Proof("spiffe", "test", token, time.time() + 60)


@pytest.fixture
def remote() -> Remote:
    return Remote()


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: Remote) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    registry = Registry((SEND,), {"test.push": remote.push})
    db = Database(tmp_path / "data")
    db.migrate()
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*RIGHTS, "vcs.push_branch")
    )
    provision(
        db,
        Provisioning(
            principals=(
                Principal(
                    principal_id="runtime",
                    kind="service",
                    auth_kind="spiffe",
                    issuer="test",
                    subject="runtime",
                ),
                Principal(
                    principal_id="supervisor",
                    kind="human",
                    auth_kind="spiffe",
                    issuer="test",
                    subject="supervisor",
                ),
                Principal(principal_id="coder", kind="agent", runtime_id="runtime"),
            ),
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        db.close()


def push(kernel: Kernel, diff: str, message: str = "Tidy settings") -> tuple[str, Any]:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"push-{time.monotonic_ns()}",
            "agent_id": "coder",
            "namespace": "test",
            "objective": "Push a change",
            "operation": "vcs.push_branch",
            "arguments": {"diff": diff, "message": message},
            "grants": ["objects.read", "objects.write", "vcs.push_branch"],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    state = OperationRuntime(kernel).execute(task_id)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
    return state, receipt


def mail(kernel: Kernel) -> list[tuple[str, str, Any]]:
    with kernel.database.read() as conn:
        return [
            (str(r[0]), str(r[1]), json.loads(r[2]))
            for r in conn.exec_driver_sql(
                "SELECT sender_id,to_address,parts_json FROM mailbox_messages ORDER BY created_at"
            )
        ]


def test_a_clean_change_is_sent(kernel: Kernel, remote: Remote, tmp_path: Path) -> None:
    kernel.content_gate = config(
        tmp_path, real=True, denylist_file=str(private(tmp_path / "deny", f"{TERM}\n"))
    )
    state, receipt = push(kernel, DIFF)
    assert state == "succeeded" and len(remote.pushed) == 1 and mail(kernel) == []


def test_a_denylisted_term_refuses_the_call_without_echoing_it(
    kernel: Kernel, remote: Remote, tmp_path: Path
) -> None:
    kernel.content_gate = config(
        tmp_path, denylist_file=str(private(tmp_path / "deny", f"# names\n{TERM.lower()}\n"))
    )
    leaked = DIFF.replace("+# Notes", f"+# Notes for {TERM}")
    state, receipt = push(kernel, leaked, message=f"Prepare the {TERM} release")
    assert state == "failed" and remote.pushed == []
    assert receipt.failure is not None and receipt.failure.code == "content_refused"
    recorded = receipt.model_dump_json()
    summary = str(receipt.failure.details["summary"])
    assert "denylist:2 at docs/notes.md:1" in summary and "denylist:2 at commit_message" in summary
    # Neither the refusal nor the stored receipt keeps the refused content.
    assert TERM.casefold() not in summary.casefold()
    assert TERM.casefold() not in recorded.casefold()
    # The supervisor is told by kernel mail: the rules and locations, never the text.
    ((sender, to, parts),) = mail(kernel)
    assert (sender, to) == ("runtime", "supervisor")
    assert TERM.casefold() not in json.dumps(parts).casefold()
    assert parts[1]["data"]["findings"] == [
        {"rule": "denylist:2", "location": "docs/notes.md:1"},
        {"rule": "denylist:2", "location": "commit_message"},
    ]


def test_the_supervisor_notice_can_be_turned_off(
    kernel: Kernel, remote: Remote, tmp_path: Path
) -> None:
    kernel.content_gate = config(
        tmp_path,
        denylist_file=str(private(tmp_path / "deny", f"{TERM}\n")),
        notify_supervisor=False,
    )
    state, receipt = push(kernel, DIFF, message=TERM)
    assert state == "failed" and receipt.failure.code == "content_refused"
    assert mail(kernel) == [] and remote.pushed == []


@needs_gitleaks
def test_a_secret_refuses_the_call_without_echoing_it(
    kernel: Kernel, remote: Remote, tmp_path: Path
) -> None:
    assert GITLEAKS, "CARDEA_REQUIRE_GITLEAKS=1 but CARDEA_TEST_GITLEAKS is not set"
    kernel.content_gate = config(tmp_path, real=True)
    leaked = DIFF.replace('+REGION = "eu-west-1"', f"+AWS_KEY = '{FAKE_KEY}'")
    state, receipt = push(kernel, leaked)
    assert state == "failed" and remote.pushed == []
    assert receipt.failure.code == "content_refused"
    assert "aws-access-token at app/settings.py:3" in str(receipt.failure.details["summary"])
    assert FAKE_KEY not in str(receipt.failure.model_dump()) and FAKE_KEY not in str(mail(kernel))


@pytest.mark.parametrize("broken", ["unconfigured", "malformed"])
def test_without_a_working_gate_nothing_is_sent(
    kernel: Kernel, remote: Remote, tmp_path: Path, broken: str
) -> None:
    if broken == "malformed":
        kernel.content_gate = config(
            tmp_path, denylist_file=str(private(tmp_path / "deny", "re:broken(\n"))
        )
    state, receipt = push(kernel, DIFF)
    assert state == "failed" and remote.pushed == []
    assert receipt.failure.code == "content_gate_unavailable"


def test_content_gate_settings_need_absolute_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ContentGateConfig.model_validate(
            {"gitleaks_binary": "gitleaks", "gitleaks_sha256": "0" * 64}
        )
    assert content_gate.MAX_FINDINGS_SHOWN >= 1


def leaves(directory: Path) -> bytes:
    """Every byte the kernel stored: the SQLite file, its log, objects and attempt logs."""
    return b"".join(path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file())


@pytest.mark.parametrize("leak", ["denylist", "secret"])
def test_a_refused_call_leaves_no_copy_anywhere(
    kernel: Kernel,
    remote: Remote,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    leak: str,
) -> None:
    if leak == "secret" and not GITLEAKS:
        pytest.skip("set CARDEA_TEST_GITLEAKS to the pinned Gitleaks binary")
    kernel.content_gate = config(
        tmp_path, real=True, denylist_file=str(private(tmp_path / "deny", f"{TERM}\n"))
    )
    needle = TERM if leak == "denylist" else FAKE_KEY
    leaked = DIFF.replace('+REGION = "eu-west-1"', f"+NOTE = '{needle}'")
    caplog.set_level("DEBUG")
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"leak-{leak}",
            "agent_id": "coder",
            "namespace": "test",
            "objective": "Push a change",
            "operation": "vcs.push_branch",
            "arguments": {"diff": leaked, "message": f"Ship {needle}"},
            # `tasks.inspect` lets the supervisor resubmit the same request ID.
            "grants": ["objects.read", "objects.write", "tasks.inspect", "vcs.push_branch"],
        }
    )
    supervisor = kernel.auth.validate("supervisor")
    view = kernel.submit(supervisor, request)
    assert OperationRuntime(kernel).execute(view.task.task_id) == "failed"
    assert remote.pushed == []
    # Identity holds: the receipt keeps its request digest and a digest of what it
    # replaced, and the same request ID still names the same task (ADR-026).
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(view.task.task_id, "execute")
        bound = conn.exec_driver_sql(
            "SELECT request_digest,request_json FROM task_bindings WHERE task_id=?",
            (view.task.task_id,),
        ).one()
    assert receipt.redacted() and receipt.failure is not None
    marker = receipt.request.arguments["redaction"]
    assert isinstance(marker, dict) and marker["request_digest"] == receipt.request_digest
    arguments = receipt.request.arguments["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["diff"] == {
        "redacted": "content_refused",
        "sha256": hashlib.sha256(leaked.encode()).hexdigest(),
        "bytes": len(leaked.encode()),
    }
    assert bound[0] == view.request_digest and '"redacted": "content_refused"' in bound[1]
    assert kernel.submit(supervisor, request).task.task_id == view.task.task_id
    # Nothing stored, logged or mailed contains the refused content.
    kernel.database.close()
    stored = leaves(tmp_path / "data")
    assert needle.encode() not in stored
    assert needle.casefold().encode() not in stored.lower()
    assert needle not in caplog.text
    assert len(stored) > 0


def test_only_a_refused_call_may_be_redacted(kernel: Kernel, tmp_path: Path) -> None:
    kernel.content_gate = config(tmp_path)
    state, receipt = push(kernel, DIFF)
    assert state == "succeeded"
    with (
        pytest.raises(StoreError) as refused,
        kernel.database.engine.begin() as conn,
        borrow(conn) as tx,
    ):
        tx.redact_refused_operation(receipt.request.task_id, "execute", {}, at=now())
    assert refused.value.code in {"not_refused", "transaction_aborted"}
    with kernel.database.engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
        conn.exec_driver_sql(
            "UPDATE task_bindings SET request_json=? WHERE task_id=?",
            ('{"redacted": "content_refused"}', receipt.request.task_id),
        )
