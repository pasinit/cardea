"""The package ships `cardea team`: the kit's modules beside the kernel's code, a root-owned
helper that only the `cardea-operators` group may run, as `cardea-kernel`, with no arguments, a
man page, and a postinst that creates the group and the state directory."""

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from cardea.service import team_cli

ROOT = Path(__file__).resolve().parents[4]
DEBIAN = ROOT / "packaging" / "debian"
KIT = ROOT / "deploy" / "team-kit"


def rules(name: str) -> list[str]:
    """A file's lines without comments and blanks."""
    return [
        line.strip()
        for line in (DEBIAN / name).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_sudoers_rule_lets_one_group_run_only_the_helper_as_the_kernel_account() -> None:
    assert rules("cardea-team.sudoers") == [
        '%cardea-operators ALL=(cardea-kernel) NOPASSWD: /usr/libexec/cardea-team ""'
    ]
    # `""` is the no-arguments form: the helper may not be given any.
    assert team_cli.SUDO == (
        "/usr/bin/sudo",
        "-n",
        "-u",
        "cardea-kernel",
        "/usr/libexec/cardea-team",
    )
    assert (team_cli.OPERATORS_GROUP, team_cli.KERNEL_USER) == ("cardea-operators", "cardea-kernel")


def test_the_helper_runs_the_kernels_own_code_with_a_fixed_command_line() -> None:
    lines = rules("cardea-team")
    assert (DEBIAN / "cardea-team").read_text().startswith("#!/bin/sh\n")
    assert lines == [
        "exec /opt/venvs/cardea-kernel/bin/python -I /usr/lib/cardea/main.py team --helper"
    ]
    # Never the caller's arguments, environment or host file.
    text = "\n".join(lines)
    assert not re.search(r"\$[@*0-9{]|--host|PYTHONPATH", text)
    assert team_cli.HELPER == "/usr/libexec/cardea-team"
    assert team_cli.HOST_FILE == Path("/etc/cardea/team.host.json")


def test_the_package_installs_the_kit_the_helper_the_rule_and_the_man_page() -> None:
    install = rules("cardea-kernel.install")
    assert "teamkit/*.py usr/lib/cardea/teamkit" in install
    assert "teamkit/team.host.example.json usr/share/doc/cardea-kernel" in install
    assert rules("cardea-kernel.manpages") == ["debian/cardea-team.1"]
    text = (DEBIAN / "rules").read_text()
    assert (
        "install -D -m 0755 debian/cardea-team debian/cardea-kernel/usr/libexec/cardea-team"
        in text
    )
    # The rule file has no dot in its name (sudo skips those) and is not writable by anyone.
    assert (
        "install -D -m 0440 debian/cardea-team.sudoers "
        "debian/cardea-kernel/etc/sudoers.d/cardea-team" in text
    )


def test_postinst_creates_the_group_the_state_directory_and_checks_the_rule() -> None:
    text = (DEBIAN / "cardea-kernel.postinst").read_text()
    assert "getent group cardea-operators" in text
    assert "addgroup --system cardea-operators" in text
    assert "install -d -o cardea-kernel -g cardea-kernel -m 0700 /var/lib/cardea/team" in text
    assert "chmod 0440 /etc/sudoers.d/cardea-team" in text
    assert "visudo -cf /etc/sudoers.d/cardea-team" in text
    assert team_cli.STATE_DIR == Path("/var/lib/cardea/team")
    assert subprocess.run(["sh", "-n", str(DEBIAN / "cardea-kernel.postinst")]).returncode == 0
    assert subprocess.run(["sh", "-n", str(DEBIAN / "cardea-team")]).returncode == 0


def test_the_man_page_documents_every_command() -> None:
    page = (DEBIAN / "cardea-team.1").read_text()
    assert page.startswith(".TH CARDEA-TEAM 1 ")
    for name, _ in team_cli.COMMANDS:
        assert re.search(rf"^\\fB{name}\b|^\.B {name}$", page, re.MULTILINE), name
    for text in ("mail list", "cardea-operators", "/usr/libexec/cardea-team", "team.host.json"):
        assert text in page.replace("\\-", "-")
    man = shutil.which("man")
    if man:
        shown = subprocess.run([man, "-l", str(DEBIAN / "cardea-team.1")], capture_output=True)
        assert shown.returncode == 0 and b"cardea" in shown.stdout


def test_the_kit_modules_the_command_loads_are_the_ones_the_package_ships() -> None:
    for name in ("teamkit", "teamsettings", "teamsettings_apply", "teamschema", "dashboard"):
        assert (KIT / f"{name}.py").is_file()
    assert (KIT / "team.host.example.json").is_file()


@pytest.fixture
def stage(tmp_path: Path) -> Path:
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


def test_the_stage_carries_the_kit_and_the_helper(stage: Path) -> None:
    for path in sorted(KIT.glob("*.py")):
        assert (stage / "teamkit" / path.name).read_bytes() == path.read_bytes()
    assert (stage / "teamkit" / "team.host.example.json").is_file()
    helper = stage / "debian" / "cardea-team"
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    for name in ("cardea-team.sudoers", "cardea-team.1", "cardea-kernel.manpages"):
        assert (stage / "debian" / name).is_file()
    assert (stage / "src" / "cardea" / "service" / "team_cli.py").is_file()
