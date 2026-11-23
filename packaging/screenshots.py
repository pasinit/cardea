"""The `screenshots` scenario's runner for `env.try`: a page served from recorded fixture
data, rendered by headless Chromium at phone and desktop width.

Usage (`make screenshots`):

    python3 packaging/screenshots.py [--out DIR] [--fixture DIR]

Serves the fixture directory (`packaging/fixtures/page`, a page and its recorded
`data.json`) on an ephemeral port of the loopback and renders it once per width with the
browser's own `--screenshot`, writing `phone-390.png` and `desktop-1280.png` to `--out`
(default `screenshots`, which the catalog lists as the scenario's artifacts). The scenario
declares `network: loopback`: the unit has its own loopback and no route out, so the page
can only load what this process serves, and the browser is told to use no proxy. Nothing
here widens what a scenario can reach. The browser is `$CARDEA_BROWSER` or the first of
`chromium`, `chromium-browser`, `chrome` on `PATH` (the toolchain image's `chromium`).

Each file's PNG header must show the requested width, and each must fit `env.try`'s
256 KiB artifact limit, else the step fails: a dropped or blank artifact is not a pass.
The last line is `screenshots: PASS (exit 0)` or `screenshots: FAIL (exit 1)`.
"""

import argparse
import functools
import http.server
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "page"
# Name, width, height in CSS pixels. 390 is a phone; 1280 a desktop.
VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("phone-390", 390, 844),
    ("desktop-1280", 1280, 800),
)
BROWSERS = ("chromium", "chromium-browser", "chrome")
# env.try drops an artifact over this.
MAX_BYTES = 256 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Lets the page's fetch of data.json finish before the capture.
RENDER_BUDGET_MS = 5000
RENDER_TIMEOUT = 120


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def browser() -> str | None:
    named = os.environ.get("CARDEA_BROWSER")
    if named:
        return named
    return next((found for name in BROWSERS if (found := shutil.which(name))), None)


def png_width(path: Path) -> int | None:
    """The width in the PNG's IHDR chunk, or None when the file is not a PNG."""
    head = path.read_bytes()[:24]
    if len(head) < 24 or not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return None
    return int(struct.unpack(">I", head[16:20])[0])


def command(
    binary: str, url: str, target: Path, profile: Path, width: int, height: int
) -> list[str]:
    return [
        binary,
        "--headless",
        f"--screenshot={target}",
        f"--window-size={width},{height}",
        f"--virtual-time-budget={RENDER_BUDGET_MS}",
        f"--user-data-dir={profile}",
        "--hide-scrollbars",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        # The unit is the sandbox: it has no user namespaces for Chromium's own, and the
        # unit allows no writable-executable memory, so V8 must not JIT.
        "--no-sandbox",
        "--js-flags=--jitless",
        "--no-proxy-server",
        url,
    ]


def render(binary: str, base: str, out: Path, profile: Path) -> list[str]:
    """Every viewport's screenshot; the problems found, empty when all are good."""
    problems: list[str] = []
    for name, width, height in VIEWPORTS:
        target = out / f"{name}.png"
        argv = command(binary, f"{base}/index.html", target, profile, width, height)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            problems.append(f"{name}: browser did not run: {error}")
            continue
        if done.returncode != 0 or not target.is_file():
            problems.append(f"{name}: browser exited {done.returncode}: {done.stderr[-500:]}")
        elif (found := png_width(target)) != width:
            problems.append(f"{name}: expected a PNG {width}px wide, got {found}")
        elif target.stat().st_size > MAX_BYTES:
            problems.append(f"{name}: {target.stat().st_size} bytes is over the {MAX_BYTES} limit")
        else:
            print(f"{name}: {width}x{height}, {target.stat().st_size} bytes")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)
    binary = browser()
    problems: list[str] = []
    if binary is None:
        problems.append(f"no browser: set CARDEA_BROWSER or install one of {', '.join(BROWSERS)}")
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(Quiet, directory=str(args.fixture))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            with tempfile.TemporaryDirectory() as profile:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                problems = render(binary, base, args.out.resolve(), Path(profile))
            server.shutdown()
    for problem in problems:
        print(problem)
    print(f"screenshots: {'FAIL' if problems else 'PASS'} (exit {1 if problems else 0})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
