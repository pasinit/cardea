"""The `screenshots` scenario of `env.try`: the mock pages a commit adds (or a fixture page),
served on loopback, captured in headless Chromium full page at 390 px and at desktop width.

Portable: the runner is exercised with a stand-in browser (a script that fetches the page,
answers the height measurement and writes a PNG). The real capture runs only where Chromium
is installed.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

from cardea.service.repositories import Registry

ROOT = Path(__file__).resolve().parents[4]
PNG = b"\x89PNG\r\n\x1a\n"


def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shots", ROOT / "packaging" / "screenshots.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stand_in(directory: Path, bulk: int = 0) -> Path:
    """A `chromium` that fetches the page it is given, as a real one would. It answers the
    height measurement with 3000 and writes a PNG whose size shrinks with the scale factor
    (`bulk` bytes at scale 1)."""
    script = directory / "chromium"
    script.write_text(
        f"#!{sys.executable}\n"
        "import re, sys, urllib.request\n"
        "args = sys.argv[1:]\n"
        "assert '--no-proxy-server' in args\n"
        "get = urllib.request.build_opener(urllib.request.ProxyHandler({})).open\n"
        "url = args[-1]\n"
        "body = get(url).read().decode()\n"
        "if '--dump-dom' in args:\n"
        "    assert 'iframe' in body\n"
        "    print('<body>HEIGHT=3000</body>')\n"
        "    sys.exit(0)\n"
        "target = next(a for a in args if a.startswith('--screenshot=')).split('=', 1)[1]\n"
        "size = next(a for a in args if a.startswith('--window-size=')).split('=', 1)[1]\n"
        "scale = float(next(a for a in args if a.startswith('--force-device-scale')).split('=')[1])\n"
        "title = re.search('<h1>(.*?)</h1>', body).group(1)\n"
        "filler = b'x' * int(" + str(bulk) + " * scale)\n"
        "open(target, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + f'{size} {scale} {title}'.encode() + filler)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def mock_repo(path: Path, mocks: dict[str, str]) -> None:
    """A repository whose last commit adds `mocks` under docs/mocks, beside an older file."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path)]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "README.md").write_text("old")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    for name, title in mocks.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(f"<html><body><h1>{title}</h1></body></html>")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "mocks"], check=True)


def test_the_catalog_names_screenshots_as_a_loopback_scenario_with_both_artifacts() -> None:
    registry = Registry.model_validate_json(
        (ROOT / "deploy/small-host/cardea/repositories.json").read_text()
    )
    environment = next(e.environment for e in registry.repositories.values() if e.environment)
    assert environment is not None
    scenario = environment.scenarios["screenshots"]
    assert scenario.argv == ("/usr/bin/make", "screenshots")
    # Served on a loopback in the second phase: no fetch, so no egress is asked for.
    assert scenario.network == "loopback" and scenario.fetch is None
    assert scenario.artifacts == tuple(
        f"screenshots/page-{n}-{w}.png" for n in range(1, 5) for w in ("mobile-390", "desktop-1280")
    )
    makefile = (ROOT / "Makefile").read_text()
    assert "screenshots:\n\tpython3 packaging/screenshots.py $(PAGES)\n" in makefile
    # The egress allow-list of the try profile is not touched by this scenario.
    workers = json.loads((ROOT / "deploy/small-host/cardea/cardea-host-workers.json").read_text())
    assert workers["profiles"]["try"]["network"]["hosts"] == [
        "pypi.org",
        "files.pythonhosted.org",
    ]
    assert workers["profiles"]["try-loopback"]["network"] == {"policy": "loopback"}


@pytest.mark.loopback
def test_with_no_mocks_the_runner_captures_the_fixture_page_at_both_widths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    out = tmp_path / "screenshots"
    # No commit adds a mock here, so the fixture is the page.
    monkeypatch.setattr(module, "added_mocks", lambda root: [])
    assert module.main(["--out", str(out)]) == 0
    assert capsys.readouterr().out.endswith("screenshots: PASS\n")
    assert (out / "page-1-mobile-390.png").read_bytes().startswith(PNG + b"390,3000 1.0")
    assert (out / "page-1-desktop-1280.png").read_bytes().startswith(PNG + b"1280,3000 1.0")


@pytest.mark.loopback
def test_a_commit_adding_several_mocks_gets_each_page_full_height_at_both_widths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    mock_repo(
        repo,
        {"docs/mocks/a/home.html": "Home", "docs/mocks/b.html": "Settings", "src/x.html": "No"},
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    out = tmp_path / "out"
    assert module.main(["--root", str(repo), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "page-1: docs/mocks/a/home.html" in printed and "page-2: docs/mocks/b.html" in printed
    assert "x.html" not in printed and printed.endswith("screenshots: PASS\n")
    assert sorted(p.name for p in out.iterdir()) == [
        "page-1-desktop-1280.png",
        "page-1-mobile-390.png",
        "page-2-desktop-1280.png",
        "page-2-mobile-390.png",
    ]
    # Height is the measured one, not the window's 844 or 800.
    assert (out / "page-1-mobile-390.png").read_bytes().endswith(b"390,3000 1.0 Home")
    assert (out / "page-2-desktop-1280.png").read_bytes().endswith(b"1280,3000 1.0 Settings")


@pytest.mark.loopback
def test_a_page_argument_replaces_the_commits_mocks_and_must_stay_inside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    mock_repo(repo, {"docs/mocks/a.html": "A", "docs/other/b.html": "B"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    out = tmp_path / "out"
    assert module.main(["--root", str(repo), "--out", str(out), "docs/other/b.html"]) == 0
    assert (out / "page-1-mobile-390.png").read_bytes().endswith(b" B")
    assert not (out / "page-2-mobile-390.png").exists()
    (tmp_path / "secret.html").write_text("<h1>S</h1>")
    assert module.main(["--root", str(repo), "--out", str(out), "../secret.html"]) == 1
    assert "no page ../secret.html" in capsys.readouterr().out


@pytest.mark.loopback
def test_a_large_screenshot_is_retaken_at_a_lower_scale_to_fit_the_artifact_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    mock_repo(repo, {f"docs/mocks/p{n}.html": f"P{n}" for n in range(1, 4)})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # 3 pages x 2 widths: each file may be 1 MiB / 6 = 174762 bytes. 300000 only fits at 0.5.
    stand_in(bin_dir, bulk=300_000)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    out = tmp_path / "out"
    assert module.main(["--root", str(repo), "--out", str(out)]) == 0
    files = sorted(out.iterdir())
    assert len(files) == 6
    assert sum(f.stat().st_size for f in files) <= module.TOTAL_BYTES
    assert all(f.stat().st_size <= module.MAX_BYTES for f in files)
    assert all(b" 0.5 " in f.read_bytes() for f in files)
    capsys.readouterr()
    # A page that does not fit even at the lowest scale fails, and leaves no oversize artifact.
    stand_in(bin_dir, bulk=10_000_000)
    assert module.main(["--root", str(repo), "--out", str(out)]) == 1
    assert "over the" in capsys.readouterr().out and not list(out.iterdir())


@pytest.mark.loopback
def test_only_four_pages_are_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    mock_repo(repo, {f"docs/mocks/p{n}.html": f"P{n}" for n in range(1, 7)})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    assert module.main(["--root", str(repo), "--out", str(tmp_path / "o")]) == 0
    assert "skipped, over 4 pages: docs/mocks/p5.html, docs/mocks/p6.html" in capsys.readouterr().out
    assert len(list((tmp_path / "o").iterdir())) == 8


def test_chromium_is_confined_to_the_loopback_host() -> None:
    module = runner()
    argv = module.command("chromium", Path("/p"), Path("/o.png"), "http://127.0.0.1:1/", 390, 844)
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in argv
    assert "--no-proxy-server" in argv
    assert "--window-size=390,844" in argv and argv[-1] == "http://127.0.0.1:1/"


@pytest.mark.loopback
def test_the_server_is_loopback_only_and_serves_the_fixture() -> None:
    module = runner()
    server = module.serve(ROOT / "packaging" / "screenshot_fixture")
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = direct.open(f"http://127.0.0.1:{port}/index.html").read()
        assert b"Team overview" in body
    finally:
        server.shutdown()


def test_a_missing_browser_or_page_fails_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert module.main(["--out", str(tmp_path / "o")]) == 1
    out = capsys.readouterr().out
    assert "no Chromium on PATH" in out and out.endswith("screenshots: FAIL\n")
    stand_in(tmp_path)
    assert module.main(["--root", str(tmp_path), "--out", str(tmp_path / "o")]) == 1
    assert "no page index.html" in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("chromium") is None and shutil.which("chromium-browser") is None,
    reason="Chromium is not installed here",
)
def test_real_chromium_returns_screenshots_of_the_fixture_page(tmp_path: Path) -> None:
    module = runner()
    out = tmp_path / "screenshots"
    assert module.main(["--out", str(out)]) == 0
    for name in ("page-1-mobile-390.png", "page-1-desktop-1280.png"):
        data = (out / name).read_bytes()
        assert data.startswith(PNG) and 0 < len(data) <= module.MAX_BYTES
