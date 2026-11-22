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
    # The pull request, its reviews, its comments and the head's checks: no files.
    assert len(host.authorizations) - before == 4


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


MARKER = "\n[cardea: patch continues, bytes "


def read_whole(vcs: Vcs, number: int) -> tuple[dict[str, str], int]:
    """Every patch of the change, reassembled by following the resuming cursor."""
    texts: dict[str, str] = {}
    offset: int | None = 0
    within: int | None = 0
    calls = 0
    while offset is not None:
        assert within is not None
        output = read(vcs, number, diff=True, diff_offset=offset, patch_offset=within)
        calls += 1
        assert valid(output)
        assert len(canonical_json(output)) <= MAX_RECORDED_OUTPUT_BYTES
        diff = output["diff"]
        assert diff["offset"] == offset and diff["patch_offset"] == within
        assert _size_of(diff["patches"]) <= vcs_ops.MAX_DIFF_BYTES
        for patch in diff["patches"]:
            text = patch["patch"]
            if text is None:
                continue
            assert len(canonical_json(text)) <= vcs_ops.MAX_PATCH
            seen = texts.get(patch["path"], "")
            # Each page starts exactly where the last one stopped: no gap, no overlap.
            assert patch["patch_offset"] == len(seen.encode())
            if patch["truncated"]:
                assert patch is diff["patches"][-1]
                text, _, tail = text.rpartition(MARKER)
                end = int(tail.split(" of ")[0].split("-")[1])
                assert (
                    diff["next_patch_offset"] == end == patch["patch_offset"] + len(text.encode())
                )
            texts[patch["path"]] = seen + text
        offset, within = diff["next_offset"], diff["next_patch_offset"]
        assert (offset is None) == (within is None)
        assert calls < 100
    return texts, calls


def _size_of(value: object) -> int:
    return len(canonical_json(value))


def test_a_large_file_is_read_whole_over_several_calls(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    # About 59 KB of added lines, with escapes and multi-byte text, and a secret mid-file.
    lines = [f'line {i} "quoted" \\ escaped é ✓ {"x" * (i % 37)}\n' for i in range(1090)]
    lines[700] = f'token = "{LEAKED}"\n'
    big = "".join(lines).encode()
    assert 55_000 < len(big) < 65_000
    number = change(host, tmp_path, {"README": b"r\n"}, {"big.txt": big, "small.py": b"y = 1\n"})
    patches = host.repositories["acme/cardea"].changed_files(number)  # what the host sends
    expected = {p["filename"]: vcs_ops.scrub(p["patch"]) for p in patches}
    texts, calls = read_whole(vcs, number)
    assert calls >= 5  # 59 KB at most 12 KiB a page
    assert texts == expected
    assert LEAKED not in texts["big.txt"] and 'token = "[redacted]"' in texts["big.txt"]


def test_resuming_pages_cut_on_line_boundaries(vcs: Vcs, host: FakeGitHub, tmp_path: Path) -> None:
    big = "".join(f"row {i:05d} {'abc' * 20}\n" for i in range(800)).encode()
    number = change(host, tmp_path, {"README": b"r\n"}, {"a.txt": big})
    output = read(vcs, number, diff=True, patch_offset=0)
    patch = output["diff"]["patches"][0]
    assert patch["truncated"] is True and patch["patch_offset"] == 0
    text = patch["patch"].rpartition(MARKER)[0]
    assert text.endswith("\n")  # a whole line, never half of one
    later = read(vcs, number, diff=True, patch_offset=output["diff"]["next_patch_offset"])
    assert later["diff"]["patches"][0]["patch"].startswith("+row ")


def test_resuming_reads_many_files_and_large_ones_without_gaps(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    big = "".join(f'line {i} "quoted" \\ escaped\n' for i in range(3000)).encode()
    head: dict[str, bytes | None] = {f"f{i}.txt": big for i in range(3)}
    head.update({f"s{i}.py": f"v = {i}\n".encode() for i in range(5)})
    head["logo.png"] = b"\x89PNG\x00\x02"
    number = change(host, tmp_path, {"README": b"r\n"}, head)
    expected = {
        p["filename"]: vcs_ops.scrub(p["patch"])
        for p in host.repositories["acme/cardea"].changed_files(number)
        if isinstance(p.get("patch"), str)
    }
    texts, _ = read_whole(vcs, number)
    assert texts == expected


def test_old_style_calls_are_unchanged(vcs: Vcs, host: FakeGitHub, tmp_path: Path) -> None:
    big = "".join(f"row {i}\n" for i in range(9000)).encode()
    number = change(host, tmp_path, {"README": b"r\n"}, {"a.txt": big, "b.txt": big})
    diff = read(vcs, number, diff=True)["diff"]
    assert "patch_offset" not in diff and "next_patch_offset" not in diff
    first = diff["patches"][0]
    assert set(first) == {"path", "patch", "truncated"} and first["truncated"] is True
    assert "[cardea: patch truncated," in first["patch"]
    # Per-file paging: both cut patches on one page, nothing more to read.
    assert [p["path"] for p in diff["patches"]] == ["a.txt", "b.txt"]
    assert diff["next_offset"] is None


def test_patch_offset_past_the_end_or_mid_character(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    number = change(host, tmp_path, {"a.txt": b"x\n"}, {"a.txt": "é✓\n".encode()})
    whole = read(vcs, number, diff=True, patch_offset=0)["diff"]["patches"][0]
    start = whole["patch"].index("é")
    byte = len(whole["patch"][:start].encode()) + 1  # inside "é"
    mid = read(vcs, number, diff=True, patch_offset=byte)["diff"]
    assert mid["patches"][0]["patch"].startswith("✓")
    assert mid["patches"][0]["patch_offset"] == byte + 1
    past = read(vcs, number, diff=True, patch_offset=10**6)["diff"]
    assert past["patches"][0]["patch"] == "" and past["next_offset"] is None
    assert past["next_patch_offset"] is None


def test_the_heads_checks_come_failed_first_with_ci_annotations(
    vcs: Vcs, host: FakeGitHub, tmp_path: Path
) -> None:
    number = change(host, tmp_path, {"a.py": b"x = 1\n"}, {"a.py": b"x = 2\n"})
    repo = host.repositories["acme/cardea"]
    head = str(repo.head("agent/eng/x"))
    failing = {"annotations_count": 3}
    repo.checks[head] = [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "python", "status": "completed", "conclusion": "failure",
         "html_url": "https://github.test/run/2", "output": failing},
        {"id": 3, "name": "build", "status": "in_progress", "conclusion": None},
    ]  # fmt: skip
    repo.annotations[2] = [
        {"annotation_level": "failure", "title": "Failures (1)",
         "message": f"FAILED tests/t.py::test_x - assert 1 == 2 ({LEAKED})"},
        {"annotation_level": "warning", "title": "Node 16", "message": "deprecated"},
        {"annotation_level": "failure", "title": "Log tail", "message": "x" * 50_000},
    ]  # fmt: skip
    output = read(vcs, number)
    assert valid(output)
    checks = output["checks"]
    assert [c["name"] for c in checks] == ["python", "build", "lint"]
    python = checks[0]
    assert python["conclusion"] == "failure" and python["url"] == "https://github.test/run/2"
    # Failures only, credential shapes scrubbed, bounded.
    assert [a["title"] for a in python["annotations"]] == ["Failures (1)", "Log tail"]
    assert "test_x" in python["annotations"][0]["message"]
    assert LEAKED not in str(python["annotations"])
    assert len(python["annotations"][1]["message"]) < 5_000
    # Passing and unfinished runs carry no annotations, and none are requested for them.
    assert "annotations" not in checks[1] and "annotations" not in checks[2]


def test_annotations_leave_room_for_the_diff(vcs: Vcs, host: FakeGitHub, tmp_path: Path) -> None:
    number = change(host, tmp_path, {"a.py": b"x = 1\n"}, {"a.py": b"x = 2\n"})
    repo = host.repositories["acme/cardea"]
    head = str(repo.head("agent/eng/x"))
    repo.checks[head] = [
        {"id": n, "name": f"job{n}", "status": "completed", "conclusion": "failure",
         "output": {"annotations_count": 5}}
        for n in range(1, 11)
    ]  # fmt: skip
    for n in range(1, 11):
        repo.annotations[n] = [
            {"annotation_level": "failure", "title": "t", "message": "y" * 4000}
        ] * 5
    output = read(vcs, number, diff=True)
    assert valid(output)
    kept = sum(len(a["message"]) for c in output["checks"] for a in c.get("annotations", []))
    assert kept <= vcs_ops.MAX_ANNOTATION_BYTES
    assert output["diff"]["patches"][0]["patch"]
    assert _size_of(output) <= MAX_RECORDED_OUTPUT_BYTES
