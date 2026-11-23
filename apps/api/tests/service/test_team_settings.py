"""The team settings model (deploy/team-kit/teamsettings.py): each setting is validated with an
error naming its field, a preview diffs a change against the host file without writing it, no
secret is shown or logged, and authority is left to the kernel."""

import json
import logging
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_team_schema import KIT, load

DATA = Path(__file__).parent / "data" / "team_settings"
SETTING_FILES = sorted(p.stem for p in DATA.glob("*.json") if p.stem != "host")


def fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text())
    return document


def secret() -> str:
    """A fake, low-entropy secret-shaped sentinel, assembled so no file holds it whole."""
    return "-".join(["not", "a", "real", "secret", "value"])


@pytest.fixture
def settings() -> ModuleType:
    return load("teamsettings")


@pytest.fixture
def host_file(tmp_path: Path) -> Path:
    """The fixture host file, copied with a private credentials section and a provisioning
    path inside the temporary directory (where the pause record would be kept)."""
    document = fixture("host")
    document["credentials"] = {
        "author": "github",
        "reviewer": "github-reviewer",
        "expires_on": "2027-01-31",
        "reviewer_login": secret(),
    }
    document["kernel"] = {"socket": f"/run/{secret()}.sock"}
    document["provisioning"] = {"out": str(tmp_path / "provision.json")}
    path = tmp_path / "team.host.json"
    path.write_text(json.dumps(document))
    return path


def test_every_setting_has_a_fixture(settings: ModuleType) -> None:
    assert sorted(settings.SETTINGS) == SETTING_FILES


@pytest.mark.parametrize("name", SETTING_FILES)
def test_a_valid_setting_is_accepted(settings: ModuleType, name: str) -> None:
    valid = fixture(name)["valid"]
    assert set(settings.validate(valid)) == {name}


@pytest.mark.parametrize(
    ("name", "index"),
    [(n, i) for n in SETTING_FILES for i in range(len(fixture(n)["invalid"]))],
)
def test_an_invalid_setting_is_refused_naming_its_field(
    settings: ModuleType, name: str, index: int
) -> None:
    case = fixture(name)["invalid"][index]
    with pytest.raises(settings.SettingsError) as raised:
        settings.validate(case["settings"])
    assert case["field"] in str(raised.value)


def test_over_long_values_are_refused_naming_the_field(settings: ModuleType) -> None:
    cases = {
        "model": {"model": "a" * 9000},
        "product_goals.goals": {"product_goals": {"goals": "a" * 9000}},
        "paused.reason": {"paused": {"paused": True, "reason": "a" * 9000}},
        "schedules.pm": {"schedules": {"pm": ["* * * * *"] * 3 + ["0 0 * * *"] * 40}},
    }
    for field, value in cases.items():
        with pytest.raises(settings.SettingsError, match=field.replace(".", r"\.")):
            settings.validate(value)
    goals = {"product_goals": {"goals": "a" * 5000, "non_goals": "b" * 5000}}
    with pytest.raises(settings.SettingsError, match="product_goals"):
        settings.validate(goals)


@pytest.mark.parametrize("bad", [None, [], {}, "model", {"colour": "red"}])
def test_the_document_must_name_known_settings(settings: ModuleType, bad: Any) -> None:
    with pytest.raises(settings.SettingsError, match="settings"):
        settings.validate(bad)


def test_the_preview_diffs_each_setting_against_the_host_file(
    settings: ModuleType, host_file: Path
) -> None:
    wanted: dict[str, Any] = {}
    for name in SETTING_FILES:
        if name != "host":
            wanted.update(fixture(name)["valid"])
    result = settings.preview(host_file, wanted)
    changes = {c["field"]: (c["from"], c["to"]) for c in result["changes"]}
    assert changes["run_limits.team"] == (30, 40)
    assert changes["run_limits.roles.dev"] == (14, 20)
    assert changes["run_limits.roles.pm"] == (4, 5)
    assert changes["spend_limits.team_usd"] == (20.0, 25.5)
    assert changes["spend_limits.per_run_usd"] == (1.0, 2.0)
    assert changes["spend_limits.roles_usd.dev"] == (None, 10.0)
    assert changes["paused.paused"] == (False, True)
    assert changes["paused.reason"] == (None, "release freeze")
    assert changes["schedules.pm"] == (["0 9 * * 1-5"], ["0 10 * * 1-5"])
    assert changes["schedules.dev"] == ([], ["*/30 * * * *"])
    assert changes["developers"] == (2, 4)
    assert changes["model"] == ("claude-sonnet-4-5", "claude-opus-5-5")
    assert changes["product_goals.goals"][0] == "Original goals."
    assert changes["product_goals.non_goals"][1] == "No mobile app."
    assert changes["email_rate_limit.count"] == (10, 5)
    assert changes["email_rate_limit.window_days"] == (1, 2)
    assert result["problems"] == []


def test_an_email_rate_limit_needs_the_email_alias(settings: ModuleType, host_file: Path) -> None:
    document = json.loads(host_file.read_text())
    del document["email"]
    host_file.write_text(json.dumps(document))
    result = settings.preview(host_file, fixture("email_rate_limit")["valid"])
    assert result["problems"] == ["email_rate_limit needs the host file's email alias"]


def test_the_preview_leaves_unnamed_and_unchanged_settings_alone(
    settings: ModuleType, host_file: Path
) -> None:
    result = settings.preview(host_file, {"developers": 2, "run_limits": {"roles": {"tl": 9}}})
    assert [c["field"] for c in result["changes"]] == ["run_limits.roles.tl"]
    assert result["changes"][0]["from"] == 6  # the kit's default for a role the file omits
    assert result["unchanged"] == ["developers"]
    assert settings.preview(host_file, {"model": "claude-sonnet-4-5"})["changes"] == []


def test_the_preview_replaces_goals_whole(settings: ModuleType, host_file: Path) -> None:
    result = settings.preview(host_file, {"product_goals": {"goals": "New goals."}})
    changes = {c["field"]: c for c in result["changes"]}
    assert changes["product_goals.goals"]["to"] == "New goals."
    assert changes["product_goals.non_goals"]["to"] is None


def test_the_preview_reads_the_pause_record_and_writes_nothing(
    settings: ModuleType, host_file: Path, tmp_path: Path
) -> None:
    (tmp_path / "dev-team.pause.json").write_text(json.dumps({"reason": "freeze"}))
    before = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir())
    resume = settings.preview(host_file, {"paused": {"paused": False}})
    assert [(c["field"], c["from"], c["to"]) for c in resume["changes"]] == [
        ("paused.paused", True, False)
    ]
    assert settings.preview(host_file, {"paused": {"paused": True}})["changes"] == []
    assert sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir()) == before


def test_the_preview_reports_what_the_host_file_would_refuse(
    settings: ModuleType, host_file: Path
) -> None:
    result = settings.preview(host_file, {"run_limits": {"roles": {"dev": 31}}})
    assert any(
        "budgets.roles.dev.runs (31) exceeds budgets.team.runs (30)" in p
        for p in result["problems"]
    )
    result = settings.preview(host_file, {"spend_limits": {"roles_usd": {"dev": 21}}})
    assert any("budgets.roles.dev.usd" in p for p in result["problems"])


def test_a_preview_of_an_invalid_setting_is_refused(settings: ModuleType, host_file: Path) -> None:
    with pytest.raises(settings.SettingsError, match=r"developers"):
        settings.preview(host_file, {"developers": 0})


def test_the_host_file_is_not_modified(settings: ModuleType, host_file: Path) -> None:
    before = host_file.read_bytes()
    settings.preview(host_file, fixture("developers")["valid"])
    assert host_file.read_bytes() == before


def test_secret_bearing_fields_are_not_settings(settings: ModuleType) -> None:
    for key in ("credentials", "kernel", "supervisor", "token"):
        with pytest.raises(settings.SettingsError, match=key) as raised:
            settings.validate({key: {"reviewer_login": secret()}})
        assert secret() not in str(raised.value)


def test_errors_never_echo_the_offending_value(settings: ModuleType) -> None:
    pasted = f"{secret()} with spaces"
    for bad in (
        {"model": pasted},
        {"developers": pasted},
        {"schedules": {"pm": [pasted]}},
        {"run_limits": {"team": pasted}},
        {"spend_limits": {"per_run_usd": pasted}},
        {"paused": {"paused": pasted}},
        {"product_goals": {"goals": 3}},
    ):
        with pytest.raises(settings.SettingsError) as raised:
            settings.validate(bad)
        assert secret() not in str(raised.value)


def test_a_preview_shows_and_logs_no_secret(
    settings: ModuleType,
    host_file: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    result = settings.preview(
        host_file, {"developers": 3, "model": "claude-opus-5-5", "run_limits": {"team": 35}}
    )
    lines = settings.render(result)
    shown = json.dumps(result) + "\n".join(lines)
    out = capsys.readouterr()
    for text in (shown, out.out, out.err, caplog.text):
        assert secret() not in text
        assert "reviewer_login" not in text
        assert "kernel.sock" not in text
    assert "github-reviewer" not in shown


def test_the_render_shows_goals_by_size_only(settings: ModuleType, host_file: Path) -> None:
    result = settings.preview(host_file, {"product_goals": {"goals": "Private plans."}})
    lines = "\n".join(settings.render(result))
    assert "Private plans." not in lines
    assert "Original goals." not in lines
    assert "14 bytes" in lines


def test_the_preview_defers_authority_to_the_kernel(settings: ModuleType, host_file: Path) -> None:
    result = settings.preview(host_file, {"developers": 3})
    assert "kernel checks" in result["authority"]
    assert settings.render(result)[-1] == result["authority"]
    # The model holds no principal and never calls the kernel: it only diffs files.
    source = (KIT / "teamsettings.py").read_text()
    for call in ("kit.client(", "kit.overview(", "kit.provision(", "kit.apply("):
        assert call not in source


def test_the_example_host_file_can_be_previewed(settings: ModuleType, tmp_path: Path) -> None:
    kit = KIT
    copy = tmp_path / "team.host.json"
    shutil.copy(kit / "team.host.example.json", copy)
    result = settings.preview(copy, {"developers": 5}, paused=False)
    assert [(c["from"], c["to"]) for c in result["changes"]] == [(2, 5)]
