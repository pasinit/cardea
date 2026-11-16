"""The host's one repository registry (ADR-037, rehearsal finding 3)."""

import json
from pathlib import Path
from typing import Any

import pytest

from cardea.service import repositories
from cardea.service.database import Fault

ENTRY = {
    "source": "/opt/cardea-code/repos/app.git",
    "owner": "acme",
    "name": "app",
    "api_url": "https://api.github.com",
    "git_url": "https://github.com/acme/app.git",
    "gates": [{"name": "test", "argv": ["/usr/bin/make", "test"], "timeout_seconds": 900}],
    "branch_prefix": "agent/{group}/",
}


def write(path: Path, document: Any, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(document))
    path.chmod(mode)
    return path


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    return write(tmp_path / "repositories.json", {"repositories": {"app": ENTRY}})


def env(**variables: Path) -> dict[str, str]:
    return {name: str(path) for name, path in variables.items()}


def test_the_registry_holds_every_field_of_an_alias(registry: Path) -> None:
    loaded = repositories.load(env(CARDEA_REPOSITORIES=registry))
    assert loaded is not None
    app = loaded.repositories["app"]
    assert app.hosted and app.source.endswith("app.git") and app.branch_prefix == "agent/{group}/"
    assert app.gate_documents() == ENTRY["gates"]
    assert repositories.load({}) is None


def test_a_read_only_alias_has_no_host(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"repositories": {"fixture": {"source": "/opt/x.git"}}})
    loaded = repositories.load(env(CARDEA_REPOSITORIES=path))
    assert loaded is not None and not loaded.repositories["fixture"].hosted


@pytest.mark.parametrize(
    "change",
    [
        {"owner": None},  # host fields go together
        {"branch_prefix": "agent/"},  # must name the group
        {"gates": [{"name": "x", "argv": []}]},
        {"unexpected": 1},
    ],
)
def test_invalid_entries_are_refused(tmp_path: Path, change: dict[str, Any]) -> None:
    entry = {k: v for k, v in {**ENTRY, **change}.items() if v is not None}
    path = write(tmp_path / "r.json", {"repositories": {"app": entry}})
    with pytest.raises(Fault) as refused:
        repositories.load(env(CARDEA_REPOSITORIES=path))
    assert refused.value.code == "invalid_repositories"


def test_a_registry_anyone_can_change_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "r.json", {"repositories": {"app": ENTRY}}, mode=0o666)
    with pytest.raises(Fault) as refused:
        repositories.load(env(CARDEA_REPOSITORIES=path))
    assert refused.value.code == "unsafe_repositories"


def coding_legacy(**change: Any) -> dict[str, Any]:
    return {"app": {"source": ENTRY["source"], "gates": ENTRY["gates"], **change}}


def vcs_legacy(**change: Any) -> dict[str, Any]:
    host = {k: ENTRY[k] for k in ("owner", "name", "api_url", "git_url")}
    return {"app": {**host, **change}}


def test_package_files_alone_still_work_with_a_deprecation_warning() -> None:
    with pytest.warns(DeprecationWarning, match="CARDEA_CODING_CONFIG"):
        assert repositories.resolve("code.session", coding_legacy(), environ={}) is None
    with pytest.warns(DeprecationWarning, match="CARDEA_VCS_CONFIG"):
        assert repositories.resolve("vcs", vcs_legacy(), environ={}) is None


def test_agreeing_package_files_warn_and_use_the_registry(registry: Path) -> None:
    environ = env(CARDEA_REPOSITORIES=registry)
    with pytest.warns(DeprecationWarning):
        found = repositories.resolve("code.session", coding_legacy(), environ=environ)
    assert found is not None and "app" in found.repositories
    with pytest.warns(DeprecationWarning):
        repositories.resolve("vcs", vcs_legacy(), legacy_prefix="agent/{group}/", environ=environ)


@pytest.mark.parametrize(
    ("package", "legacy", "prefix"),
    [
        ("code.session", coding_legacy(source="/elsewhere.git"), None),
        ("code.session", coding_legacy(gates=[]), None),
        ("code.session", {**coding_legacy(), "other": {"source": "/x.git"}}, None),
        ("vcs", vcs_legacy(owner="someone-else"), "agent/{group}/"),
        ("vcs", vcs_legacy(), "bots/{group}/"),
    ],
)
def test_disagreeing_package_files_are_refused(
    registry: Path, package: str, legacy: dict[str, Any], prefix: str | None
) -> None:
    with pytest.warns(DeprecationWarning), pytest.raises(Fault) as refused:
        repositories.resolve(
            package, legacy, legacy_prefix=prefix, environ=env(CARDEA_REPOSITORIES=registry)
        )
    assert refused.value.code == "repositories_disagree"
    assert "app" in refused.value.message or "other" in refused.value.message


def test_the_kernel_refuses_to_start_when_they_disagree(tmp_path: Path, registry: Path) -> None:
    vcs = write(
        tmp_path / "vcs.json",
        {"repositories": vcs_legacy(owner="someone-else"), "credentials": {}},
    )
    coding = write(tmp_path / "coding.json", {"repositories": coding_legacy(), "harness": {}})
    agreeing = env(CARDEA_REPOSITORIES=registry, CARDEA_CODING_CONFIG=coding)
    with pytest.warns(DeprecationWarning):
        repositories.check(agreeing)
    with pytest.warns(DeprecationWarning), pytest.raises(Fault) as refused:
        repositories.check({**agreeing, "CARDEA_VCS_CONFIG": str(vcs)})
    assert refused.value.code == "repositories_disagree"
    # The registry alone, or package files without repositories: nothing to compare.
    repositories.check(env(CARDEA_REPOSITORIES=registry))
    plain = write(tmp_path / "plain.json", {"harness": {}})
    repositories.check(env(CARDEA_REPOSITORIES=registry, CARDEA_CODING_CONFIG=plain))


def test_serve_checks_the_registry_before_anything_starts() -> None:
    import inspect

    from cardea.service import server

    source = inspect.getsource(server.serve)
    assert source.index("repositories.check()") < source.index("service_lock(")
