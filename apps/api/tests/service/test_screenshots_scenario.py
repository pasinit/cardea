"""The `screenshots` scenario of `env.try`: the fixture page in headless Chromium at 390px
and desktop width, returned as artifacts.

Portable: the runner is exercised with a stand-in browser that fetches the page it is given
and writes a PNG of the requested width. The run against real Chromium is skipped on a host
that has none. Serving the page uses TCP loopback, so those tests are marked `loopback`.
"""

import importlib.util
import json
import os
import shutil
import stat
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

from cardea.service.repositories import Registry

ROOT = Path(__file__).resolve().parents[4]
# env.try drops an artifact over 256 KiB and keeps 1 MiB in all.
ARTIFACT_LIMIT = 256 * 1024
TOTAL_LIMIT = 1024 * 1024
EXPECTED = {"phone-390.png": 390, "desktop-1280.png": 1280}

# A browser that records its argv, loads the page over HTTP like a browser would, and writes
# a PNG as wide as `--window-size` asks, times $FAKE_SCALE.
FAKE_BROWSER = """#!{python}
import json, os, struct, sys, urllib.request, zlib

args = sys.argv[1:]
target = next(a.split("=", 1)[1] for a in args if a.startswith("--screenshot="))
size = next(a for a in args if a.startswith("--window-size=")).split("=")[1]
width, height = (int(v) for v in size.split(","))
width = int(width * float(os.environ.get("FAKE_SCALE", "1")))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
assert b"data.json" in opener.open(args[-1]).read()
data = json.load(opener.open(args[-1].rsplit("/", 1)[0] + "/data.json"))
assert data["orders"]

def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

row = b"\\x00" + b"\\xff" * (width * 3)
header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
png = (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header)
       + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))
open(target, "wb").write(png)
with open(target + ".argv", "w") as log:
    json.dump(args, log)
"""


def runner() -> ModuleType:
    path = ROOT / "packaging" / "screenshots.py"
    spec = importlib.util.spec_from_file_location("screenshots_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_browser(tmp_path: Path, body: str = FAKE_BROWSER) -> Path:
    script = tmp_path / "fake-chromium"
    script.write_text(body.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_the_catalog_names_screenshots_as_a_loopback_scenario_with_both_artifacts() -> None:
    registry = Registry.model_validate_json(
        (ROOT / "deploy/small-host/cardea/repositories.json").read_text()
    )
    environment = next(e.environment for e in registry.repositories.values() if e.environment)
    assert environment is not None
    scenario = environment.scenarios["screenshots"]
    assert scenario.argv == ("/usr/bin/make", "screenshots") and scenario.tier == "process"
    # No route out and no fetch: the page and its data are in the checkout.
    assert scenario.network == "loopback" and scenario.fetch is None
    assert scenario.artifacts == ("screenshots/phone-390.png", "screenshots/desktop-1280.png")
    assert "screenshots:\n\tpython3 packaging/screenshots.py\n" in (ROOT / "Makefile").read_text()


def test_the_scenario_adds_no_egress_and_leaves_the_confinement_alone() -> None:
    kit = ROOT / "deploy/small-host/cardea"
    workers = json.loads((kit / "cardea-host-workers.json").read_text())
    try_profile = workers["profiles"]["try"]
    assert try_profile["network"]["hosts"] == ["pypi.org", "files.pythonhosted.org"]
    # The browser comes in the read-only toolchain image; the profiles gain no JIT and no path.
    assert "jit" not in try_profile and "jit" not in workers["profiles"]["try-loopback"]
    for name in ("env-try", "env-try-loopback"):
        assert not any("playwright" in p for p in workers["workers"][name]["read_only"])
    source = (ROOT / "packaging" / "try_toolchain.py").read_text()
    packages = source.split("PACKAGES = (", 1)[1].split(")", 1)[0]
    assert '"chromium"' in packages


def test_the_fixture_page_renders_from_recorded_data() -> None:
    fixture = ROOT / "packaging" / "fixtures" / "page"
    data = json.loads((fixture / "data.json").read_text())
    assert data["orders"] and data["recorded"]
    page = (fixture / "index.html").read_text()
    assert 'fetch("data.json")' in page and 'name="viewport"' in page
    # Nothing is loaded from another origin.
    assert "http://" not in page and "https://" not in page and "//cdn" not in page


def test_the_viewports_are_phone_and_desktop() -> None:
    widths = {name: width for name, width, _ in runner().VIEWPORTS}
    assert {f"{n}.png": w for n, w in widths.items()} == EXPECTED


def test_the_browser_is_told_to_use_no_proxy_and_no_jit(tmp_path: Path) -> None:
    argv = runner().command("chromium", "http://127.0.0.1:1/index.html", tmp_path / "a.png",
                            tmp_path, 390, 844)  # fmt: skip
    assert "--no-proxy-server" in argv and "--js-flags=--jitless" in argv
    assert "--window-size=390,844" in argv and argv[-1] == "http://127.0.0.1:1/index.html"


def test_png_width_reads_the_header_and_refuses_other_files(tmp_path: Path) -> None:
    module = runner()
    chunk = struct.pack(">IIBBBBB", 390, 5, 8, 2, 0, 0, 0)
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + chunk + b"\0" * 4)
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not a png at all, but long enough to have a header")
    assert module.png_width(good) == 390 and module.png_width(junk) is None


@pytest.mark.loopback
def test_a_run_serves_the_page_and_returns_both_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARDEA_BROWSER", str(fake_browser(tmp_path)))
    out = tmp_path / "screenshots"
    assert runner().main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.endswith("screenshots: PASS (exit 0)\n")
    module = runner()
    sizes = {}
    for name, width in EXPECTED.items():
        assert module.png_width(out / name) == width
        sizes[name] = (out / name).stat().st_size
    assert all(size <= ARTIFACT_LIMIT for size in sizes.values())
    assert sum(sizes.values()) <= TOTAL_LIMIT
    flags = json.loads((out / "phone-390.png.argv").read_text())
    assert "--window-size=390,844" in flags and flags[-1].endswith("/index.html")
    assert flags[-1].startswith("http://127.0.0.1:")


@pytest.mark.loopback
def test_a_browser_that_fails_or_writes_the_wrong_width_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    crashing = fake_browser(tmp_path, "#!{python}\nimport sys\nsys.exit(3)\n")
    monkeypatch.setenv("CARDEA_BROWSER", str(crashing))
    assert runner().main(["--out", str(tmp_path / "a")]) == 1
    out = capsys.readouterr().out
    assert "phone-390: browser exited 3" in out and out.endswith("screenshots: FAIL (exit 1)\n")
    monkeypatch.setenv("CARDEA_BROWSER", str(fake_browser(tmp_path)))
    monkeypatch.setenv("FAKE_SCALE", "0.5")
    assert runner().main(["--out", str(tmp_path / "b")]) == 1
    assert "phone-390: expected a PNG 390px wide, got 195" in capsys.readouterr().out


def test_no_browser_fails_the_run_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CARDEA_BROWSER", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert runner().main(["--out", str(tmp_path / "o")]) == 1
    out = capsys.readouterr().out
    assert "no browser" in out and out.endswith("screenshots: FAIL (exit 1)\n")


@pytest.mark.loopback
@pytest.mark.skipif(
    shutil.which("chromium") is None and not os.environ.get("CARDEA_BROWSER"),
    reason="no Chromium on this host",
)
def test_real_chromium_renders_the_fixture_page(tmp_path: Path) -> None:
    module = runner()
    assert module.main(["--out", str(tmp_path)]) == 0
    for name, width in EXPECTED.items():
        assert module.png_width(tmp_path / name) == width
        assert (tmp_path / name).stat().st_size <= ARTIFACT_LIMIT
