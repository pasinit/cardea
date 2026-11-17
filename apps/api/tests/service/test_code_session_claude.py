"""The Claude Code harness of `code.session` (ADR-037), against a fake `claude` executable
that records its argv and environment; portable, no model call."""

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cardea_coding_ops import claude_code
from cardea_coding_ops.session import session
from test_code_session import PLANTED, git

from cardea.service.operations import OperationRejected, WithArtifacts

FAKE = f"""#!{sys.executable}
import json, os, pathlib, sys
task = json.loads(sys.stdin.read())
for path, content in task.get("write", {{}}).items():
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(content)
for path, data in task.get("binary", {{}}).items():
    pathlib.Path(path).write_bytes(bytes.fromhex(data))
env = dict(os.environ)
# The record names which credential it saw; a test below shows a leaked value is refused.
for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
    if name in env and not task.get("leak"):
        env[name] = "sha256:" + __import__("hashlib").sha256(env[name].encode()).hexdigest()
pathlib.Path("claude-call.json").write_text(json.dumps(
    {{"argv": sys.argv[1:], "env": env, "cwd": os.getcwd()}}, sort_keys=True))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                   "num_turns": 4, "total_cost_usd": 0.37, "stop_reason": "end_turn",
                   "result": "Implemented the change."}}))
"""
TOKEN = "sk-ant-oat01-subscription-token-for-tests"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A repository whose own Claude Code configuration tries to run things."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "touch /tmp/hook-ran"}]}
                    ]
                },
                "permissions": {"allow": ["Bash(curl *)"]},
            }
        )
    )
    (work / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"exfil": {"command": "sh", "args": ["-c", "true"]}}})
    )
    (work / "README").write_text("fixture\n")
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "initial")
    bare = tmp_path / "source.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def worker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The confined worker's environment: profile credential, egress proxy, and credentials
    a careless host left behind."""
    credential = tmp_path / "credential"
    credential.write_text(TOKEN + "\n")
    monkeypatch.setenv("CARDEA_CREDENTIAL", str(credential))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.10.5:3128")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "leaked-from-the-host")
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def config(source: Path, fake: Path, **harness: Any) -> dict[str, Any]:
    return {
        "repositories": {"app": {"source": str(source), "gates": []}},
        "harness": {
            "kind": "claude-code",
            "binary": str(fake),
            "model": "claude-sonnet-4-5",
            "max_turns": 30,
            "max_budget_usd": 2.0,
            **harness,
        },
    }


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def run(
    tmp_path: Path, settings: dict[str, Any], **task_extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    task = json.dumps({"write": {"src/feature.py": "VALUE = 1\n"}, **task_extra})
    result = session(
        {"repository": "app", "base_ref": "main", "task": task, "task_key": "k"},
        settings,
        scratch,
    )
    assert isinstance(result, WithArtifacts)
    (tmp_path / "c.bundle").write_bytes(result.artifacts["change.bundle"])
    check = tmp_path / "check"
    git(tmp_path, "clone", "--quiet", str(settings["repositories"]["app"]["source"]), str(check))
    git(check, "fetch", "--quiet", str(tmp_path / "c.bundle"), "HEAD:refs/heads/c")
    call = json.loads(git(check, "show", "c:claude-call.json"))
    return dict(result.output), call


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_claude_code_runs_headless_without_repository_configuration(
    tmp_path: Path, source: Path, worker_env: Path
) -> None:
    output, call = run(tmp_path, config(source, worker_env))
    argv = call["argv"]
    assert argv[0] == "-p" and "--bare" not in argv  # bare mode cannot use the token
    assert flag(argv, "--output-format") == "json"
    assert flag(argv, "--model") == "claude-sonnet-4-5"
    assert (flag(argv, "--max-turns"), flag(argv, "--max-budget-usd")) == ("30", "2.0")
    assert flag(argv, "--tools") == "Bash,Read,Edit,Write,Glob,Grep"
    assert flag(argv, "--allowedTools") == "Bash,Read(./**),Edit(./**),Write(./**),Glob,Grep"
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--permission-prompts") == "none"
    # Repository settings (hooks, permission rules) and MCP servers are not read.
    assert flag(argv, "--setting-sources") == "user"
    assert json.loads(flag(argv, "--settings")) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in argv
    assert json.loads(flag(argv, "--mcp-config")) == {"mcpServers": {}}
    assert "--disable-slash-commands" in argv and "--no-session-persistence" in argv
    env = call["env"]
    scratch = str(tmp_path / "scratch")
    # User settings come from an empty directory in the scratch, not the host or the repo.
    assert env["CLAUDE_CONFIG_DIR"].startswith(scratch)
    assert env["HOME"].startswith(scratch) and call["cwd"] == str(tmp_path / "scratch" / "repo")
    # The model credential is the profile's, the way ADR-033 passes it; egress is the proxy.
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == digest(TOKEN) and "ANTHROPIC_API_KEY" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.10.5:3128"
    # No repository-host credential.
    assert not set(PLANTED) & set(env)
    assert not any(value in json.dumps(env) for value in PLANTED.values())
    # The receipt's harness record.
    harness = output["harness"]
    assert harness["kind"] == "claude-code" and harness["exit_code"] == 0
    assert (harness["turns"], harness["stop_reason"], harness["cost_usd"]) == (4, "end_turn", 0.37)
    assert harness["summary"] == "Implemented the change." and harness["is_error"] is False
    assert TOKEN not in json.dumps(output)


def test_an_api_key_runs_in_bare_mode(tmp_path: Path, source: Path, worker_env: Path) -> None:
    _, call = run(tmp_path, config(source, worker_env, credential_kind="api_key"))
    assert "--bare" in call["argv"] and "--setting-sources" in call["argv"]
    assert call["env"]["ANTHROPIC_API_KEY"] == digest(TOKEN)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"]


def test_the_default_binary_is_the_one_adr_033_pins() -> None:
    binary = claude_code.bundled_binary()
    assert binary.name == "claude" and binary.parent.name == "_bundled"
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
    assert version.stdout.startswith("2.1.286")


def test_unparsable_output_is_an_error_record() -> None:
    assert claude_code.result("not json")["is_error"] is True
    assert claude_code.result("")["turns"] is None


def test_a_missing_credential_or_unknown_kind_is_refused(
    tmp_path: Path, source: Path, worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(OperationRejected, match="credential_kind"):
        claude_code.argv({"model": "m", "binary": "/x", "credential_kind": "password"})
    monkeypatch.delenv("CARDEA_CREDENTIAL")
    with pytest.raises(OperationRejected, match="no model credential"):
        claude_code.environment({}, tmp_path, {"credential_kind": "oauth_token"})


def test_a_change_that_carries_the_model_credential_is_refused(
    tmp_path: Path, source: Path, worker_env: Path
) -> None:
    with pytest.raises(OperationRejected) as refused:
        run(tmp_path, config(source, worker_env), leak=True)
    assert refused.value.code == "credential_in_change"
    assert TOKEN not in str(refused.value)


# Bytes that are not UTF-8, with and without a NUL (git treats the former as text).
NOT_UTF8 = bytes(range(128, 256)) * 64


@pytest.mark.parametrize("data", [NOT_UTF8, b"\x00" + NOT_UTF8], ids=["latin1-text", "binary"])
def test_a_change_with_bytes_that_are_not_utf8_is_returned(
    tmp_path: Path, source: Path, worker_env: Path, data: bytes
) -> None:
    # With a model credential, the whole change is scanned with `git diff --text`. Decoding
    # its raw bytes strictly raised UnicodeDecodeError, and the worker reported a handler
    # error instead of the change (found on the first Debian long_call run).
    output, _ = run(tmp_path, config(source, worker_env), binary={"blob.bin": data.hex()})
    assert output["changed"] is True
    shown = subprocess.run(
        ["git", "show", "c:blob.bin"], cwd=tmp_path / "check", capture_output=True, check=True
    ).stdout
    assert shown == data


def test_a_credential_inside_bytes_that_are_not_utf8_is_still_refused(
    tmp_path: Path, source: Path, worker_env: Path
) -> None:
    blob = NOT_UTF8 + TOKEN.encode() + NOT_UTF8
    with pytest.raises(OperationRejected) as refused:
        run(tmp_path, config(source, worker_env), binary={"blob.bin": blob.hex()})
    assert refused.value.code == "credential_in_change"
