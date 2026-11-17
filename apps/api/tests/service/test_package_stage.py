"""The package build refuses a staged tree that differs from its source manifest.

A copy of the stage from macOS added an AppleDouble `._NAME` file beside each staged file.
`dh_install` shipped them, and the installed `cardea init` failed: Alembic loaded
`versions/._m1_001.py` as a migration ("source code string cannot contain null bytes").
`packaging/debian/verify_stage.py` runs before the build and refuses such a tree.
"""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[4]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_stage", ROOT / "packaging" / "debian" / "verify_stage.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    """A real stage of this checkout, as `make m5-stage` writes it."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to stage the package inputs")
    destination = tmp_path / "stage"
    subprocess.run(
        ["python3", str(ROOT / "packaging" / "stage.py"), str(destination), "--uv", uv],
        check=True,
        capture_output=True,
    )
    return destination


def test_a_fresh_stage_matches_its_manifest(stage: Path) -> None:
    assert load().problems(stage) == []


def test_bytecode_is_never_staged(stage: Path) -> None:
    # `load()` imports verify_stage.py from packaging/debian, which can leave a __pycache__
    # there; `debian/rules clean` deletes it on the builder, so a staged copy would fail the
    # manifest check as "missing".
    load()
    manifest = json.loads((stage / "source-manifest.json").read_text())
    assert not [name for name in manifest if "__pycache__" in name or name.endswith(".pyc")]


def test_the_rules_verify_the_stage_before_building() -> None:
    rules = (ROOT / "packaging" / "debian" / "rules").read_text()
    assert "execute_before_dh_auto_configure:\n\tpython3 debian/verify_stage.py ." in rules


def test_appledouble_files_are_refused(stage: Path, capsys: pytest.CaptureFixture[str]) -> None:
    versions = stage / "src" / "cardea" / "service" / "migrations" / "versions"
    (versions / "._m1_001.py").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X")
    verify = load()
    assert verify.problems(stage) == [
        "not in the manifest: src/cardea/service/migrations/versions/._m1_001.py"
    ]
    assert verify.main(["verify_stage", str(stage)]) == 1
    assert "COPYFILE_DISABLE=1" in capsys.readouterr().err


def test_changed_missing_and_linked_files_are_refused(stage: Path) -> None:
    (stage / "uv.lock").write_text("changed\n")
    (stage / "debian" / "control").unlink()
    launcher = stage / "src" / "cardea" / "hosts" / "launcher.py"
    launcher.unlink()
    launcher.symlink_to("/etc/passwd")
    assert sorted(load().problems(stage)) == [
        "a symbolic link: src/cardea/hosts/launcher.py",
        "changed: uv.lock",
        "missing: debian/control",
    ]


def test_debhelper_outputs_are_allowed(stage: Path) -> None:
    (stage / "debian" / ".debhelper").mkdir()
    (stage / "debian" / ".debhelper" / "generated").write_text("x")
    (stage / "debian" / "files").write_text("x")
    (stage / "debian" / "debhelper-build-stamp").write_text("x")
    (stage / "debian" / "cardea-kernel.substvars").write_text("x")
    (stage / "debian" / "cardea-kernel").mkdir()
    (stage / "debian" / "cardea-kernel" / "anything").write_text("x")
    assert load().problems(stage) == []
    manifest = json.loads((stage / "source-manifest.json").read_text())
    assert "debian/verify_stage.py" in manifest
