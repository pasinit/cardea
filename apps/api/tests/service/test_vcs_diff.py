"""`vcs.read_change` with `diff: true`: the bounded diff reviewers read (ADR-037), against
the fake host over real git repositories."""

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cardea_vcs_ops import Vcs, descriptors
from cardea_vcs_ops import ops as vcs_ops
from cardea_vcs_ops.config import FixedTokenConfig, VcsConfig
from cardea_vcs_ops.fake import FakeGitHub
from pydantic import JsonValue, SecretStr

from cardea.kernel.models import canonical_json
from cardea.service.operations import MAX_RECORDED_OUTPUT_BYTES, OperationContext, Registry

TOKEN = "ghs_TEST" + "x7Q2" * 8
LEAKED = "ghp_" + "A1b2C3d4" * 5  # a credential-shaped string a commit should not carry
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


@pytest.fixture
def host(tmp_path: Path) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "host")
    fake.root.mkdir()
    fake.add("acme", "cardea")
    return fake


@pytest.fixture
def vcs(host: FakeGitHub) -> Vcs:
    config = VcsConfig(credentials=FixedTokenConfig(kind="test", token=SecretStr(TOKEN)))
    return Vcs(config, transport=host.transport(), registry=host.registry())


def context() -> OperationContext:
    return OperationContext(
        task_id="t1",
        namespace="eng",
        receipt_id="r1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        effect_key="k1",
        agent_id="pe",
        group="eng",
    )


def change(
    host: FakeGitHub, tmp_path: Path, base: dict[str, bytes], head: dict[str, bytes | None]
) -> int:
    """`main` with `base`, a branch changing it to `head` (None deletes), and its pull."""
    repo = host.repositories["acme/cardea"]
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)

    def commit(files: dict[str, bytes | None], message: str) -> None:
        for name, data in files.items():
            path = work / name
            if data is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        subprocess.run([*GIT, "-C", str(work), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(work), "commit", "-q", "-m", message], check=True)

    commit(dict(base), "base")
    subprocess.run(["git", "-C", str(work), "push", "-q", str(repo.path), "main"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "agent/eng/x"], check=True)
    commit(head, "change")
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(repo.path), "agent/eng/x"], check=True
    )
    response = host._create_pull(
        repo,
        {"head": "agent/eng/x", "base": "main", "title": "Change", "body": f"token {LEAKED}"},
        "dev",
    )
    number: int = response.json()["number"]
    return number


def read(vcs: Vcs, number: int, **extra: JsonValue) -> dict[str, Any]:
    arguments: dict[str, JsonValue] = {"repository": "cardea", "number": number, **extra}
    result: dict[str, Any] = asyncio.run(vcs.read_change(arguments, context()))
    return result


def valid(output: dict[str, Any]) -> bool:
    """The output passes the checks the kernel applies before recording it."""
    registry = Registry(descriptors(), {})
    return registry.check_output("vcs.read_change", output) is not None


def test_without_the_flag_there_is_no_diff(vcs: Vcs, host: FakeGitHub, tmp_path: Path) -> None:
    number = change(host, tmp_path, {"a.py": b"x = 1\n"}, {"a.py": b"x = 2\n"})
    before = len(host.authorizations)
    output = read(vcs, number)
    assert "diff" not in output and valid(output)
    # The pull request, its reviews and its comments: the files are not even requested.
    assert len(host.authorizations) - before == 3


def test_the_diff_lists_files_and_shows_patches_as_scrubbed_data(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    number = change(
        host,
        tmp_path,
        {
            "app.py": b"def f():\n    return 1\n",
            "old.txt": b"gone\n",
            "logo.png": b"\x89PNG\x00\x01",
        },
        {
            "app.py": b'def f():\n    password = form["password"]\n'
            + f'    key = "{LEAKED}"\n    return 2\n'.encode(),
            "old.txt": None,
            "logo.png": b"\x89PNG\x00\x02\x03",
            "new.md": b"# New\n",
        },
    )
    output = read(vcs, number, diff=True)
    assert valid(output)
    assert LEAKED not in str(output) and "[redacted]" in output["body"]
    diff = output["diff"]
    assert "Untrusted" in diff["note"] and diff["files_total"] == 4
    files = {f["path"]: f for f in diff["files"]}
    assert files["app.py"]["status"] == "modified" and files["app.py"]["additions"] == 3
    assert files["old.txt"]["status"] == "removed" and files["new.md"]["status"] == "added"
    assert files["logo.png"]["binary"] is True and not files["app.py"]["binary"]
    patches = {p["path"]: p for p in diff["patches"]}
    app = patches["app.py"]["patch"]
    assert app.startswith("@@") and "+    return 2" in app
    # Ordinary code reads unchanged; only the credential shape is replaced.
    assert 'password = form["password"]' in app and 'key = "[redacted]"' in app
    assert patches["logo.png"] == {"path": "logo.png", "patch": None, "omitted": "binary"}
    assert diff["next_offset"] is None and diff["offset"] == 0 and not diff["files_truncated"]


def test_large_patches_are_truncated_and_paged_by_offset(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    big = "".join(f'line {i} "quoted" \\ escaped\n' for i in range(4000)).encode()
    head: dict[str, bytes | None] = {f"f{i}.txt": big for i in range(6)}
    number = change(host, tmp_path, {"README": b"r\n"}, head)
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        output = read(vcs, number, diff=True, diff_offset=offset)
        assert valid(output)
        assert len(canonical_json(output)) <= MAX_RECORDED_OUTPUT_BYTES
        diff = output["diff"]
        assert diff["files_total"] == 6 and len(diff["files"]) == 6 and diff["patches"]
        for patch in diff["patches"]:
            assert patch["truncated"] is True
            assert "[cardea: patch truncated," in patch["patch"]
            assert len(canonical_json(patch["patch"])) <= vcs_ops.MAX_PATCH
            seen.append(patch["path"])
        assert diff["offset"] == offset
        offset = diff["next_offset"]
    # Every file's patch was shown exactly once across the pages, in order.
    assert seen == [f"f{i}.txt" for i in range(6)]


def test_file_list_and_host_omissions_are_bounded(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    number = change(host, tmp_path, {"a": b"1\n"}, {"a": b"2\n"})
    many = [
        {
            "filename": f"src/module_{i:04d}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "@@ -1 +1 @@\n-a\n+b",
        }
        for i in range(300)
    ]
    huge = {"filename": "data.json", "status": "modified", "additions": 90000, "deletions": 0}
    host.repositories["acme/cardea"].files[number] = [huge, *many]
    output = read(vcs, number, diff=True)
    assert valid(output)
    diff = output["diff"]
    assert diff["files_total"] == 301 and diff["files_truncated"] is True
    assert 0 < len(diff["files"]) <= vcs_ops.MAX_DIFF_FILES
    # A diff the host would not show is listed as omitted, not mistaken for a binary file.
    assert diff["patches"][0] == {"path": "data.json", "patch": None, "omitted": "too_large"}
    assert len(diff["patches"]) <= vcs_ops.MAX_PATCHES
    assert diff["next_offset"] == len(diff["patches"])
    # An offset past the end returns no patches and nothing more to read.
    past = read(vcs, number, diff=True, diff_offset=1000)["diff"]
    assert past["patches"] == [] and past["next_offset"] is None
