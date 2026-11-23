"""The host's mirror refresh (`packaging/mirror_refresh.py`): `env.try` can stage a pull
request head or an agent branch head once the mirror holds it, and still refuses an unknown
ID. Portable: the repository host is a local bare repository, no TCP."""

import configparser
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from cardea_env_ops.try_ import stage

from cardea.service.operations import OperationRejected

ROOT = Path(__file__).resolve().parents[4]
KIT = ROOT / "deploy" / "small-host" / "cardea"


def script() -> ModuleType:
    path = ROOT / "packaging" / "mirror_refresh.py"
    spec = importlib.util.spec_from_file_location("mirror_refresh", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def hosts(tmp_path: Path) -> dict[str, Path | str]:
    """A repository host holding a PR head and an agent branch, and a mirror of main only."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    (work / "f").write_text("main\n")
    git(work, "add", "f")
    git(work, "commit", "--quiet", "-m", "main")
    host = tmp_path / "host.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(work), str(host))
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(host), str(mirror))
    heads = {}
    for ref, name in (("refs/pull/7/head", "pr"), ("refs/heads/agent/g/change", "agent")):
        (work / "f").write_text(name + "\n")
        git(work, "commit", "--quiet", "-am", name)
        heads[name] = git(work, "rev-parse", "HEAD")
        git(work, "push", "--quiet", str(host), f"HEAD:{ref}")
    return {"host": host, "mirror": mirror, **heads}


def staged(source: object, commit: str, tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir()
    return stage(str(source), commit, scratch)


def test_a_pr_head_is_unknown_until_the_mirror_is_refreshed(
    hosts: dict[str, Path | str], tmp_path: Path
) -> None:
    with pytest.raises(OperationRejected) as refused:
        staged(hosts["mirror"], str(hosts["pr"]), tmp_path, "before")
    assert refused.value.code == "env_commit_unknown"
    script().refresh(Path(hosts["mirror"]), str(hosts["host"]))
    for name in ("pr", "agent"):
        checkout = staged(hosts["mirror"], str(hosts[name]), tmp_path, name)
        assert git(checkout, "rev-parse", "HEAD") == hosts[name]
        assert (checkout / "f").read_text() == name + "\n"


def test_an_unknown_id_is_still_refused(hosts: dict[str, Path | str], tmp_path: Path) -> None:
    script().refresh(Path(hosts["mirror"]), str(hosts["host"]))
    with pytest.raises(OperationRejected) as refused:
        staged(hosts["mirror"], "f" * 40, tmp_path, "unknown")
    assert refused.value.code == "env_commit_unknown"


def test_a_named_commit_is_fetched_and_an_unknown_one_is_not(
    hosts: dict[str, Path | str], tmp_path: Path
) -> None:
    module = script()
    work = tmp_path / "work"
    git(work, "checkout", "--quiet", "-b", "loose")
    (work / "f").write_text("loose\n")
    git(work, "commit", "--quiet", "-am", "loose")
    loose = git(work, "rev-parse", "HEAD")
    git(work, "push", "--quiet", str(hosts["host"]), "loose:refs/heads/loose")
    git(Path(hosts["host"]), "update-ref", "-d", "refs/heads/loose")  # reachable from no ref
    git(Path(hosts["host"]), "config", "uploadpack.allowAnySHA1InWant", "true")
    module.refresh(Path(hosts["mirror"]), str(hosts["host"]), loose)
    assert git(staged(hosts["mirror"], loose, tmp_path, "loose"), "rev-parse", "HEAD") == loose
    with pytest.raises(LookupError):
        module.refresh(Path(hosts["mirror"]), str(hosts["host"]), "f" * 40)
    with pytest.raises(ValueError):
        module.refresh(Path(hosts["mirror"]), str(hosts["host"]), "main")


def test_the_cli_refreshes_hosted_repositories_only(
    hosts: dict[str, Path | str], tmp_path: Path
) -> None:
    registry = tmp_path / "repositories.json"
    registry.write_text(
        json.dumps(
            {
                "repositories": {
                    "app": {"source": str(hosts["mirror"]), "git_url": str(hosts["host"])},
                    "plain": {"source": str(tmp_path / "missing.git")},
                }
            }
        )
    )
    assert script().main([str(registry)]) == 0
    refs = git(Path(hosts["mirror"]), "for-each-ref", "--format=%(refname)")
    assert "refs/pull/7/head" in refs and "refs/heads/agent/g/change" in refs


def test_the_kit_timer_runs_the_refresh() -> None:
    timer = configparser.ConfigParser()
    timer.read(KIT / "cardea-mirror-refresh.timer")
    assert "OnUnitActiveSec" in timer["Timer"]
    service = (KIT / "cardea-mirror-refresh.service").read_text()
    assert "mirror_refresh.py /opt/cardea-code/repositories.json" in service
