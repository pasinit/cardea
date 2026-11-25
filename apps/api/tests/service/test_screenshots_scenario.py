"""The `screenshots` scenario of `env.try`: the mock pages a commit adds, served on loopback,
captured full page in headless Chromium at 390 px and at desktop width.

Portable: the runner is exercised with a stand-in browser (a script that records its
arguments and writes a PNG). The real capture runs only where Chromium is installed.
"""

import importlib.util
import json
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


def stand_in(directory: Path) -> Path:
    """A `chromium` that fetches the page it is given, as a real one would.

    With `--dump-dom` it prints the page height the page's `data-height="N"` names (the
    served script that would record it is checked separately); otherwise it writes a PNG:
    the window size, the scale, and `data-bytes="N"` bytes scaled by the scale squared."""
    script = directory / "chromium"
    script.write_text(
        f"#!{sys.executable}\n"
        "import re, sys, urllib.request\n"
        "args = sys.argv[1:]\n"
        "assert '--no-proxy-server' in args\n"
        "get = urllib.request.build_opener(urllib.request.ProxyHandler({})).open\n"
        "url = args[-1]\n"
        "body = get(url).read().decode()\n"
        "if 'setAttribute' not in body:\n"
        "    sys.exit(3)\n"
        "number = lambda name, default: int((re.search(name + r'=\"(\\d+)\"', body) or "
        "[0, default])[1])\n"
        "if '--dump-dom' in args:\n"
        "    print('<html data-page-height=\"%d\">' % number('data-height', 500))\n"
        "    sys.exit(0)\n"
        "data = b''\n"
        "if url.endswith('/index.html'):\n"
        "    data = get(url.rsplit('/', 1)[0] + '/data/teams.json').read()\n"
        "flag = lambda name: next(a for a in args if a.startswith(name + '=')).split('=', 1)[1]\n"
        "scale = float(flag('--force-device-scale-factor'))\n"
        "filler = b'x' * int(number('data-bytes', 0) * scale * scale)\n"
        "size = flag('--window-size').encode() + b' %s ' % str(scale).encode()\n"
        "head = b'\\x89PNG\\r\\n\\x1a\\n' + size\n"
        "open(flag('--screenshot'), 'wb').write(head + data + filler)\n"
        "sys.exit(0 if '<h1>Team overview</h1>' in body or 'mock' in url else 4)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    for argv in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", *argv],
            cwd=repo, check=True, capture_output=True,
        )  # fmt: skip


def mock(height: int = 500, size: int = 0) -> str:
    return f'<!doctype html><h1>mock</h1><i data-height="{height}" data-bytes="{size}"></i>'


@pytest.fixture
def chromium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(shutil.which('git') or '/usr/bin/git').parent}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def test_the_catalog_names_screenshots_as_a_loopback_scenario_with_its_artifacts() -> None:
    registry = Registry.model_validate_json(
        (ROOT / "deploy/small-host/cardea/repositories.json").read_text()
    )
    environment = next(e.environment for e in registry.repositories.values() if e.environment)
    assert environment is not None
    scenario = environment.scenarios["screenshots"]
    assert scenario.argv == ("/usr/bin/make", "screenshots")
    # Served on a loopback in the second phase: no fetch, so no egress is asked for.
    assert scenario.network == "loopback" and scenario.fetch is None
    # Two per page, for as many pages as the runner takes, and no more than env.try keeps.
    module = runner()
    assert scenario.artifacts == (
        *(
            f"screenshots/p{n}-{kind}.png"
            for n in range(1, module.MAX_PAGES + 1)
            for kind in ("mobile-390", "desktop-1280")
        ),
        f"screenshots/{module.REPORT}",
    )
    assert len(scenario.artifacts) <= 16
    makefile = (ROOT / "Makefile").read_text()
    assert "screenshots:\n\tpython3 packaging/screenshots.py\n" in makefile
    # The egress allow-list of the try profile is not touched by this scenario.
    workers = json.loads((ROOT / "deploy/small-host/cardea/cardea-host-workers.json").read_text())
    assert workers["profiles"]["try"]["network"]["hosts"] == [
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
    ]
    assert workers["profiles"]["try-loopback"]["network"] == {"policy": "loopback"}


@pytest.mark.loopback
@pytest.mark.usefixtures("chromium")
def test_a_commit_that_adds_several_mocks_gets_both_widths_of_each_full_page(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    commit(repo, "earlier", {"docs/mocks/old.html": mock(), "src/app.html": mock()})
    commit(
        repo,
        "mocks",
        {
            "docs/mocks/b/two.html": mock(height=2400),
            "docs/mocks/one.html": mock(height=1300),
            "docs/mocks/notes.md": "not a page",
            "src/page.html": mock(),
            "docs/mocks/old.html": mock(height=9),  # changed, not added
        },
    )
    out = tmp_path / "screenshots"
    assert module.main(["--root", str(repo), "--out", str(out)]) == 0
    log = capsys.readouterr().out
    assert log.endswith("screenshots: PASS\n")
    assert "p1: docs/mocks/b/two.html" in log and "p2: docs/mocks/one.html" in log
    assert sorted(f.name for f in out.iterdir()) == [
        "p1-desktop-1280.png", "p1-mobile-390.png", "p2-desktop-1280.png", "p2-mobile-390.png",
        "report.txt",
    ]  # fmt: skip
    # Each is as tall as its page, at its width.
    for name, size in (
        ("p1-mobile-390", b"390,2400"), ("p1-desktop-1280", b"1280,2400"),
        ("p2-mobile-390", b"390,1300"), ("p2-desktop-1280", b"1280,1300"),
    ):  # fmt: skip
        assert (out / f"{name}.png").read_bytes().startswith(PNG + size)


@pytest.mark.loopback
@pytest.mark.usefixtures("chromium")
def test_screenshots_are_retaken_smaller_to_fit_the_artifact_limit(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    # Over the limit at full size; fits at half the device pixel ratio.
    commit(repo, "big", {"docs/mocks/big.html": mock(size=600_000)})
    out = tmp_path / "one"
    assert module.main(["--root", str(repo), "--out", str(out)]) == 0
    for name in ("p1-mobile-390.png", "p1-desktop-1280.png"):
        data = (out / name).read_bytes()
        assert len(data) <= module.MAX_BYTES
        assert b" 0.5 " in data or b" 0.35 " in data
    # Seven such pages share the 1 MiB an env.try call keeps.
    pages = {f"docs/mocks/p{n}.html": mock(size=600_000) for n in range(7)}
    commit(repo, "many", pages)
    many = tmp_path / "many"
    assert module.main(["--root", str(repo), "--out", str(many)]) == 0
    sizes = [f.stat().st_size for f in many.iterdir()]
    assert len(sizes) == 15 and sum(sizes) <= module.MAX_TOTAL
    # A page that cannot be made to fit fails, naming the limit.
    commit(repo, "huge", {"docs/mocks/huge.html": mock(size=50_000_000)})
    capsys.readouterr()
    assert module.main(["--root", str(repo), "--out", str(tmp_path / "huge")]) == 1
    assert "byte share" in capsys.readouterr().out


@pytest.mark.loopback
@pytest.mark.usefixtures("chromium")
def test_more_pages_than_the_catalog_has_artifact_paths_for_fail_loudly(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    commit(repo, "nine", {f"docs/mocks/m{n}.html": mock() for n in range(9)})
    assert module.main(["--root", str(repo), "--out", str(tmp_path / "o")]) == 1
    out = capsys.readouterr().out
    assert "not captured, over 7 pages: docs/mocks/m7.html" in out
    assert out.endswith("screenshots: FAIL\n")
    assert len(list((tmp_path / "o").iterdir())) == 15


@pytest.mark.loopback
@pytest.mark.usefixtures("chromium")
def test_a_page_given_as_an_argument_is_captured_instead(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    out = tmp_path / "screenshots"
    fixture = ROOT / "packaging" / "screenshot_fixture"
    assert module.main(["--root", str(fixture), "--page", "index.html", "--out", str(out)]) == 0
    assert capsys.readouterr().out.endswith("screenshots: PASS\n")
    mobile, desktop = out / "p1-mobile-390.png", out / "p1-desktop-1280.png"
    assert mobile.read_bytes().startswith(PNG + b"390,")
    assert desktop.read_bytes().startswith(PNG + b"1280,")
    # The page was served the recorded data, not anything live.
    recorded = (fixture / "data/teams.json").read_bytes()
    assert recorded in mobile.read_bytes() and recorded in desktop.read_bytes()


def test_chromium_is_confined_to_the_loopback_host() -> None:
    module = runner()
    argv = module.command("chromium", Path("/p"), "http://127.0.0.1:1/", 390, 844)
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in argv
    assert "--no-proxy-server" in argv and "--dump-dom" in argv
    assert "--window-size=390,844" in argv and argv[-1] == "http://127.0.0.1:1/"
    url = "http://127.0.0.1:1/"
    shot = module.command("chromium", Path("/p"), url, 390, 3000, Path("/o.png"), 0.5)
    assert "--screenshot=/o.png" in shot and "--window-size=390,3000" in shot
    assert "--force-device-scale-factor=0.5" in shot and "--dump-dom" not in shot


@pytest.mark.loopback
def test_the_server_is_loopback_only_and_measures_the_pages_it_serves() -> None:
    module = runner()
    server = module.serve(ROOT / "packaging" / "screenshot_fixture")
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = direct.open(f"http://127.0.0.1:{port}/index.html").read()
        assert b"Team overview" in body and b"data-page-height" in body
        data = direct.open(f"http://127.0.0.1:{port}/data/teams.json").read()
        assert b"data-page-height" not in data
    finally:
        server.shutdown()


def test_a_missing_browser_or_page_fails_with_a_reason(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runner()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert module.main(["--out", str(tmp_path / "o")]) == 1
    out = capsys.readouterr().out
    assert "no Chromium on PATH" in out and out.endswith("screenshots: FAIL\n")
    stand_in(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{Path(shutil.which('git') or '/usr/bin/git').parent}")
    commit(repo, "no mock", {"src/page.html": mock()})
    assert module.main(["--root", str(repo), "--out", str(tmp_path / "o")]) == 0
    out = capsys.readouterr().out
    assert "nothing to render: HEAD adds nothing under docs/mocks/" in out
    assert out.endswith("screenshots: PASS\n")
    args = ["--root", str(tmp_path), "--page", "gone.html", "--out", str(tmp_path / "o")]
    assert module.main(args) == 1
    assert "no page gone.html" in capsys.readouterr().out


@pytest.mark.loopback
@pytest.mark.skipif(
    shutil.which("chromium") is None and shutil.which("chromium-browser") is None,
    reason="Chromium is not installed here",
)
def test_real_chromium_returns_screenshots_of_the_fixture_page(tmp_path: Path) -> None:
    module = runner()
    out = tmp_path / "screenshots"
    fixture = ROOT / "packaging" / "screenshot_fixture"
    assert module.main(["--root", str(fixture), "--page", "index.html", "--out", str(out)]) == 0
    for name in ("p1-mobile-390.png", "p1-desktop-1280.png"):
        data = (out / name).read_bytes()
        assert data.startswith(PNG) and 0 < len(data) <= module.MAX_BYTES


def test_a_toolchain_chromium_starts_without_its_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env.try mounts the toolchain under its own root and the worker sees no /etc: Debian's
    # wrapper sources /etc/chromium.d/* and dies, so the real binary runs directly with the
    # toolchain's libraries and fontconfig.
    shots = runner()
    root = tmp_path / "toolchain"
    for folder in ("usr/bin", "usr/lib/chromium", "usr/lib/aarch64-linux-gnu/pulseaudio",
                   "lib/aarch64-linux-gnu", "etc/fonts"):  # fmt: skip
        (root / folder).mkdir(parents=True)
    wrapper, real = root / "usr/bin/chromium", root / "usr/lib/chromium/chromium"
    for path in (wrapper, real):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    (root / "etc/fonts/fonts.conf").write_text("<fontconfig/>")
    monkeypatch.setenv("PATH", str(root / "usr/bin"))
    assert shots.browser() == str(real.resolve())
    libraries = shots.EXTRA_ENV["LD_LIBRARY_PATH"].split(":")
    assert str((root / "usr/lib/aarch64-linux-gnu").resolve()) in libraries
    assert str((root / "usr/lib/aarch64-linux-gnu/pulseaudio").resolve()) in libraries
    assert libraries[-1] == str(real.parent.resolve())
    assert shots.EXTRA_ENV["FONTCONFIG_FILE"] == str((root / "etc/fonts/fonts.conf").resolve())
    # A wrapper with no real binary next to it is used as it is.
    real.unlink()
    plain = runner()
    assert plain.browser() == str(wrapper)
    assert plain.EXTRA_ENV == {}


def test_a_commit_that_changes_the_dashboard_gets_its_real_pages_captured(
    tmp_path: Path,
) -> None:
    # UX reviews the dashboard as built, not only a mock: a change under deploy/team-kit to a
    # dashboard file makes the scenario start the commit's dashboard on its fixtures.
    import subprocess as sp

    shots = runner()
    root = tmp_path / "repo"
    (root / "deploy/team-kit").mkdir(parents=True)

    def git(*args: str) -> None:
        sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
               cwd=root, check=True, capture_output=True)  # fmt: skip

    git("init", "-q", "--initial-branch=main")
    (root / "deploy/team-kit/teamkit.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert not shots.dashboard_changed(root)
    (root / "deploy/team-kit/teamkit.py").write_text("x = 2\n")
    git("commit", "-qam", "not the dashboard")
    assert not shots.dashboard_changed(root)
    (root / "deploy/team-kit/dashboard_teams.js").write_text("// new\n")
    git("add", ".")
    git("commit", "-qm", "dashboard")
    assert shots.dashboard_changed(root)
    assert shots.dashboard_routes(root) == ["/"]
    (root / "deploy/team-kit/screenshots.routes").write_text(
        "/\n/member?team=dev&member=dev-team.dev.1\nnot-a-route\n"
    )
    assert shots.dashboard_routes(root) == ["/", "/member?team=dev&member=dev-team.dev.1"]


def test_the_report_gives_a_reader_without_images_the_text_and_the_overflow() -> None:
    module = runner()
    dom = (
        '<html data-scroll-width="500" data-client-width="390" data-wide="DIV.card,TABLE">'
        "<head><title>t</title><style>x{}</style></head><body><h1>Team  overview</h1>"
        "<script>var a</script><p>two <b>columns</b></p></body></html>"
    )
    text = "\n".join(module.summary(dom, 390))
    assert "viewport 390px, content 500px: HORIZONTAL OVERFLOW" in text
    assert "elements past the right edge: DIV.card,TABLE" in text
    assert "| Team overview" in text and "| columns" in text
    assert "var a" not in text and "x{}" not in text
    fine = "\n".join(module.summary('<html data-scroll-width="390" data-client-width="390" '
                                  'data-wide=""><body>hi</body></html>', 390))
    assert "no horizontal overflow" in fine and "past the right edge" not in fine
    unmeasured = "\n".join(module.summary("<html><body>hi</body></html>", 1280))
    assert "layout not measured" in unmeasured


@pytest.mark.loopback
def test_the_served_pages_measure_their_own_overflow() -> None:
    module = runner()
    for name in (b"data-scroll-width", b"data-client-width", b"data-wide"):
        assert name in module.MEASURE
