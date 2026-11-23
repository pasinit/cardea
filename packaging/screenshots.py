"""The `screenshots` scenario's runner for `env.try`: a page against recorded fixture data,
in headless Chromium, at 390 px and at desktop width.

Usage (`make screenshots`):

    python3 packaging/screenshots.py [--root DIR] [--page PATH] [--out DIR]

Serves `--root` (default `packaging/screenshot_fixture`, a page and its recorded JSON) on an
ephemeral port of the loopback interface, takes one screenshot per width and writes
`mobile-390.png` and `desktop-1280.png` to `--out` (default `screenshots`), the scenario's
artifact paths. It needs only a loopback, so the catalog declares `network: loopback` and the
scenario runs in the second phase, whose unit has no proxy and no route out. Chromium is also
told to resolve nothing but `127.0.0.1`, so a page that names another host fails to load
rather than reaching for it. The last line is `screenshots: PASS` or `screenshots: FAIL`;
the exit status agrees.
"""

import argparse
import functools
import http.server
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDTHS: tuple[tuple[str, int, int], ...] = (
    ("mobile-390", 390, 844),
    ("desktop-1280", 1280, 800),
)
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
# env.try keeps artifacts of at most 256 KiB each; a bigger one would be dropped.
MAX_BYTES = 256 * 1024
# Lets the page's fetch of its fixture data finish before the capture.
VIRTUAL_TIME_MS = 5000
TIMEOUT = 120


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def browser() -> str | None:
    return next((path for name in BROWSERS if (path := shutil.which(name))), None)


def serve(root: Path) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def command(
    binary: str, profile: Path, target: Path, url: str, width: int, height: int
) -> list[str]:
    return [
        binary,
        "--headless",
        "--disable-gpu",
        # The worker's own confinement is the sandbox; Chromium's cannot nest inside it.
        "--no-sandbox",
        f"--user-data-dir={profile}",
        "--no-proxy-server",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        f"--window-size={width},{height}",
        f"--virtual-time-budget={VIRTUAL_TIME_MS}",
        f"--screenshot={target}",
        url,
    ]


def capture(binary: str, url: str, out: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="shots-") as scratch:
        for name, width, height in WIDTHS:
            target = out / f"{name}.png"
            argv = command(binary, Path(scratch) / name, target, url, width, height)
            try:
                done = subprocess.run(
                    argv, capture_output=True, text=True, timeout=TIMEOUT, check=False
                )
                problem = "" if done.returncode == 0 else f"exit {done.returncode}"
                if done.returncode:
                    sys.stdout.write(done.stderr[-1000:])
            except (OSError, subprocess.TimeoutExpired) as error:
                problem = f"{type(error).__name__}"
            if not problem and not target.is_file():
                problem = "no screenshot written"
            if not problem and target.stat().st_size > MAX_BYTES:
                problem = f"{target.stat().st_size} bytes, over the {MAX_BYTES} artifact limit"
            results.append((name, not problem, problem))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=HERE / "screenshot_fixture")
    parser.add_argument("--page", default="index.html")
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    args = parser.parse_args(argv)
    binary = browser()
    if binary is None or not (args.root / args.page).is_file():
        what = "no Chromium on PATH" if binary is None else f"no page {args.page} in {args.root}"
        sys.stdout.write(f"{what}\nscreenshots: FAIL\n")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    server = serve(args.root)
    try:
        url = f"http://127.0.0.1:{server.server_port}/{args.page}"
        results = capture(binary, url, args.out)
    finally:
        server.shutdown()
    for name, ok, problem in results:
        sys.stdout.write(f"{name}: {'pass' if ok else 'FAIL ' + problem}\n")
    passed = all(ok for _, ok, _ in results)
    sys.stdout.write(f"screenshots: {'PASS' if passed else 'FAIL'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
