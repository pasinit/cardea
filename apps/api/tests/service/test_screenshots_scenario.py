"""The `screenshots` scenario of `env.try`: a fixture page, served on loopback, captured in
headless Chromium at 390 px and at desktop width.

Portable: the runner is exercised with a stand-in browser (a script that records its
arguments and writes a PNG). The real capture runs only where Chromium is installed.
"""

import importlib.util
import json
import shutil
import stat
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


def stand_in(directory: Path) -> Path:
    """A `chromium` that fetches the page it is given, as a real one would, and writes a PNG."""
    script = directory / "chromium"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, urllib.request\n"
        "args = sys.argv[1:]\n"
        "assert '--no-proxy-server' in args\n"
        "get = urllib.request.build_opener(urllib.request.ProxyHandler({})).open\n"
        "url = args[-1]\n"
        "body = get(url).read().decode()\n"
        "data = get(url.rsplit('/', 1)[0] + '/data/teams.json').read()\n"
        "target = next(a for a in args if a.startswith('--screenshot=')).split('=', 1)[1]\n"
        "size = next(a for a in args if a.startswith('--window-size=')).split('=', 1)[1]\n"
        "open(target, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + size.encode() + data)\n"
        "sys.exit(0 if '<h1>Team overview</h1>' in body else 3)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


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
    assert scenario.artifacts == ("screenshots/mobile-390.png", "screenshots/desktop-1280.png")
    makefile = (ROOT / "Makefile").read_text()
    assert "screenshots:\n\tpython3 packaging/screenshots.py\n" in makefile
    # The egress allow-list of the try profile is not touched by this scenario.
    workers = json.loads((ROOT / "deploy/small-host/cardea/cardea-host-workers.json").read_text())
    assert workers["profiles"]["try"]["network"]["hosts"] == [
        "pypi.org",
        "files.pythonhosted.org",
    ]
    assert workers["profiles"]["try-loopback"]["network"] == {"policy": "loopback"}


def test_the_runner_serves_the_fixture_and_writes_both_widths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = tmp_path / "screenshots"
    assert module.main(["--out", str(out)]) == 0
    assert capsys.readouterr().out.endswith("screenshots: PASS\n")
    mobile, desktop = out / "mobile-390.png", out / "desktop-1280.png"
    assert mobile.read_bytes().startswith(PNG + b"390,844")
    assert desktop.read_bytes().startswith(PNG + b"1280,800")
    # The page was served the recorded data, not anything live.
    recorded = (ROOT / "packaging/screenshot_fixture/data/teams.json").read_bytes()
    assert mobile.read_bytes().endswith(recorded) and desktop.read_bytes().endswith(recorded)


def test_chromium_is_confined_to_the_loopback_host() -> None:
    module = runner()
    argv = module.command("chromium", Path("/p"), Path("/o.png"), "http://127.0.0.1:1/", 390, 844)
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in argv
    assert "--no-proxy-server" in argv
    assert "--window-size=390,844" in argv and argv[-1] == "http://127.0.0.1:1/"


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
    for name in ("mobile-390.png", "desktop-1280.png"):
        data = (out / name).read_bytes()
        assert data.startswith(PNG) and 0 < len(data) <= module.MAX_BYTES
