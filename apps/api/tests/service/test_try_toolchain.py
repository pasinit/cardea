"""The env.try toolchain image script (ADR-038 decision 6) and the kit's `try` profile;
portable: mmdebstrap is never run here."""

import configparser
import hashlib
import importlib.util
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from cardea_env_ops import descriptors

from cardea.hosts import launcher
from cardea.service.repositories import Registry

ROOT = Path(__file__).resolve().parents[4]
KIT = ROOT / "deploy" / "small-host" / "cardea"


def script() -> ModuleType:
    path = ROOT / "packaging" / "try_toolchain.py"
    spec = importlib.util.spec_from_file_location("try_toolchain", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tarball(path: Path) -> Path:
    with tarfile.open(path, "w") as archive:
        data = b"#!/bin/sh\necho hi\n"
        info = tarfile.TarInfo("usr/bin/tool")
        info.size, info.mode = len(data), 0o755
        archive.addfile(info, io.BytesIO(data))
    return path


def test_the_build_is_minbase_debian_13_and_dated_to_the_week() -> None:
    module = script()
    argv = module.mmdebstrap_argv(Path("/tmp/x.tar"))
    # In a mount namespace of its own: root mode would otherwise leave the host's `/`
    # private, and systemd then hands no LoadCredential file to any later unit.
    assert argv[:5] == ["/usr/bin/unshare", "--mount", "--propagation", "private", "--"]
    assert argv[5] == "/usr/bin/mmdebstrap" and "trixie" in argv and "--variant=minbase" in argv
    monday = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
    for day in range(7):
        assert module.week_start(monday + day * 86400 + 3600) == int(monday)
    assert module.week_start(monday - 1) == int(monday) - 7 * 86400


def test_an_image_unpacks_read_only_under_its_digest_and_pins(tmp_path: Path) -> None:
    module = script()
    root = tmp_path / "toolchain"
    root.mkdir()
    archive = tarball(tmp_path / "t.tar")
    digest = module.sha256_file(archive)
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    image = module.unpack(archive, root, digest)
    assert image == root / digest and (image / "usr/bin/tool").stat().st_mode & 0o222 == 0
    registry = tmp_path / "repositories.json"
    registry.write_text(
        json.dumps(
            {
                "repositories": {
                    "app": {
                        "source": "/opt/cardea-code/repos/app.git",
                        "environment": {"scenarios": {"try": {"argv": ["/usr/bin/make", "try"]}}},
                    },
                    "mirror": {"source": "/opt/cardea-code/repos/m.git"},
                }
            }
        )
    )
    registry.chmod(0o644)
    module.pin(registry, root, digest)
    pinned = json.loads(registry.read_text())["repositories"]
    assert pinned["app"]["environment"]["toolchain"] == {"path": str(image), "sha256": digest}
    assert "environment" not in pinned["mirror"]
    assert registry.stat().st_mode & 0o777 == 0o644
    # Pruning keeps the pinned image even beyond `keep`.
    other = root / ("e" * 64)
    other.mkdir()
    assert module.prune(root, 0, "e" * 64, module.pinned(registry)) == []


def test_the_kit_runs_env_try_in_the_try_profile() -> None:
    workers = json.loads((KIT / "cardea-host-workers.json").read_text())
    profile, worker = workers["profiles"]["try"], workers["workers"]["env-try"]
    launcher.validate_profile(profile)
    launcher.validate_worker(worker, workers["profiles"])
    (descriptor,) = descriptors()
    assert descriptor.cardea.executor is not None
    assert descriptor.cardea.executor.worker == "env-try" and worker["profiles"] == ["try"]
    assert worker["argv"][-1] == "env.try"
    # No credential, no public web: the package indexes only (ADR-038 section 3).
    assert "credential" not in profile and profile["network"]["policy"] == "allow_list"
    assert profile["concurrency"] == 1
    assert descriptor.cardea.timeout_seconds <= profile["wall_seconds"]
    registry = Registry.model_validate_json((KIT / "repositories.json").read_text())
    assert worker["env"]["CARDEA_REPOSITORIES"] in worker["read_only"]
    environment = next(e.environment for e in registry.repositories.values() if e.environment)
    assert environment is not None and environment.toolchain is not None
    assert environment.toolchain.path.startswith(next(p for p in worker["read_only"] if "try" in p))


def test_the_kit_timer_refreshes_weekly() -> None:
    timer = configparser.ConfigParser()
    timer.read(KIT / "cardea-try-toolchain.timer")
    assert timer["Timer"]["OnCalendar"].startswith("Mon ")
    service = (KIT / "cardea-try-toolchain.service").read_text()
    assert "try_toolchain.py build" in service and "try_toolchain.py pin" in service
