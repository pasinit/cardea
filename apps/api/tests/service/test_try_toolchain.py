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

import pytest
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
    argv = module.mmdebstrap_argv(Path("/tmp/x.tar"), Path("/tmp/b/uv"))
    # In a mount namespace of its own: root mode would otherwise leave the host's `/`
    # private, and systemd then hands no LoadCredential file to any later unit.
    assert argv[:5] == ["/usr/bin/unshare", "--mount", "--propagation", "private", "--"]
    assert argv[5] == "/usr/bin/mmdebstrap" and "trixie" in argv and "--variant=minbase" in argv
    monday = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
    for day in range(7):
        assert module.week_start(monday + day * 86400 + 3600) == int(monday)
    assert module.week_start(monday - 1) == int(monday) - 7 * 86400


def uv_archive(member: str, data: bytes = b"\x7fELF uv", *, link: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
            archive.addfile(info)
        else:
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def test_uv_is_pinned_by_version_and_digest_for_both_build_architectures() -> None:
    module = script()
    for machine in ("aarch64", "x86_64"):
        triple = f"{machine}-unknown-linux-gnu"
        url, member, digest = module.uv_archive(machine)
        releases = "https://github.com/astral-sh/uv/releases/download"
        assert url == f"{releases}/{module.UV_VERSION}/uv-{triple}.tar.gz"
        assert member == f"uv-{triple}/uv"
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    with pytest.raises(SystemExit, match="no pinned uv archive"):
        module.uv_archive("riscv64")


def test_uv_is_verified_before_it_is_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = script()
    triple = "aarch64-unknown-linux-gnu"
    member = f"uv-{triple}/uv"
    archive = uv_archive(member)
    urls: list[str] = []

    def download(url: str) -> bytes:
        urls.append(url)
        return archive

    # The real pin does not match this archive: refused, nothing written.
    with pytest.raises(SystemExit, match="expected"):
        module.fetch_uv(tmp_path / "bad", 1, "aarch64", download)
    assert not (tmp_path / "bad").exists()
    pins = module.UV_ARCHIVES
    monkeypatch.setitem(pins, "aarch64", (triple, hashlib.sha256(archive).hexdigest()))
    epoch = 1_763_337_600
    path = module.fetch_uv(tmp_path / "good", epoch, "aarch64", download)
    assert path == tmp_path / "good" / "uv" and path.read_bytes() == b"\x7fELF uv"
    assert path.stat().st_mode & 0o777 == 0o755 and int(path.stat().st_mtime) == epoch
    assert urls[-1].endswith(f"/{module.UV_VERSION}/uv-{triple}.tar.gz")
    # A verified archive without the binary, or with a link in its place, is refused too.
    for wrong in (uv_archive(f"uv-{triple}/uvx"), uv_archive(member, link=True)):
        monkeypatch.setitem(pins, "aarch64", (triple, hashlib.sha256(wrong).hexdigest()))
        with pytest.raises(SystemExit):
            module.fetch_uv(tmp_path / "wrong", 1, "aarch64", lambda url, data=wrong: data)
        assert not (tmp_path / "wrong" / "uv").exists()


def test_the_build_copies_the_verified_uv_onto_path() -> None:
    module = script()
    argv = module.mmdebstrap_argv(Path("/tmp/x.tar"), Path("/tmp/b/uv"))
    hooks = [a.removeprefix("--customize-hook=") for a in argv if a.startswith("--customize-hook=")]
    assert hooks[0] == "copy-in /tmp/b/uv /usr/local/bin"
    # The copy changes the directory's date; it is reset to SOURCE_DATE_EPOCH.
    assert hooks[1] == 'touch --no-dereference --date="@$SOURCE_DATE_EPOCH" "$1/usr/local/bin"'
    assert hooks[2] == '"$1/usr/local/bin/uv" --version'
    assert argv[-2:] == ["trixie", "/tmp/x.tar"]
    # env.try puts the image's /usr/local/bin first on PATH, so `make` finds this uv.
    assert module.UV_PATH == "/usr/local/bin/uv"


def test_build_verifies_uv_before_running_mmdebstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = script()
    calls: list[str] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def refuse(destination: Path, epoch: int) -> Path:
        calls.append("fetch_uv")
        raise SystemExit("bad digest")

    monkeypatch.setattr(module, "fetch_uv", refuse)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: calls.append("mmdebstrap"))
    with pytest.raises(SystemExit, match="bad digest"):
        module.build(tmp_path / "toolchain", 2, None)
    assert calls == ["fetch_uv"]

    def fetched(destination: Path, epoch: int) -> Path:
        calls.append(f"fetch_uv@{epoch}")
        destination.mkdir(parents=True)
        (destination / "uv").write_bytes(b"uv")
        return destination / "uv"

    def mmdebstrap(argv: list[str], env: dict[str, str], check: bool) -> None:
        calls.append(f"mmdebstrap@{env['SOURCE_DATE_EPOCH']}")
        assert f"--customize-hook=copy-in {destination(argv)} /usr/local/bin" in argv
        tarball(Path(argv[-1]))

    def destination(argv: list[str]) -> str:
        return str(Path(argv[-1]).parent / "uv" / "uv")

    calls.clear()
    monkeypatch.setattr(module, "fetch_uv", fetched)
    monkeypatch.setattr(module.subprocess, "run", mmdebstrap)
    digest = module.build(tmp_path / "toolchain", 2, None)
    epoch = calls[0].partition("@")[2]
    assert calls == [f"fetch_uv@{epoch}", f"mmdebstrap@{epoch}"]
    assert (tmp_path / "toolchain" / digest / "usr/bin/tool").exists()


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
