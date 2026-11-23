"""The `screenshots` scenario's runner for `env.try`: pages against their own files (or recorded
fixture data), in headless Chromium, full page height, at 390 px and at desktop width.

Usage (`make screenshots [PAGES="docs/mocks/a.html ..."]`):

    python3 packaging/screenshots.py [--root DIR] [--out DIR] [PAGE ...]

Which pages: the `PAGE` arguments (paths below `--root`); else the HTML mock pages under
`docs/mocks/` that the checked-out commit adds or changes; else the fixture page
`packaging/screenshot_fixture/index.html`. At most `MAX_PAGES` are captured. `--root`
defaults to the repository (the fixture's own directory when falling back to the fixture) and
is served on an ephemeral port of the loopback interface.

Each page is measured (its scroll height at the width, capped at `MAX_HEIGHT`) and captured at
that height, to `page-<n>-mobile-390.png` and `page-<n>-desktop-1280.png` in `--out` (default
`screenshots`), the scenario's artifact paths; `page-<n>: <path>` lines say which page is which.
An artifact over its size limit is captured again at a lower device scale factor, down to
`SCALES[-1]`, before the page fails. It needs only a loopback, so the catalog declares
`network: loopback` and the scenario runs in the second phase, whose unit has no proxy and no
route out. Chromium is also told to resolve nothing but `127.0.0.1`, so a page that names
another host fails to load rather than reaching for it. The last line is `screenshots: PASS`
or `screenshots: FAIL`; the exit status agrees.
"""

import argparse
import functools
import http.server
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDTHS: tuple[tuple[str, int, int], ...] = (
    ("mobile-390", 390, 844),
    ("desktop-1280", 1280, 800),
)
# The scenario's artifacts are at most 8 files and 1 MiB together (`env.try`).
MAX_PAGES = 4
MAX_HEIGHT = 12000
SCALES = (1.0, 0.75, 0.5, 0.35)
MOCKS = "docs/mocks/"
MEASURE = "/__height__.html"
HEIGHT = re.compile(r"HEIGHT=(\d+)")
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
# env.try keeps artifacts of at most 256 KiB each; a bigger one would be dropped.
MAX_BYTES = 256 * 1024
TOTAL_BYTES = 1024 * 1024
# Lets the page's fetch of its fixture data finish before the capture.
VIRTUAL_TIME_MS = 5000
TIMEOUT = 120


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != MEASURE:
            return super().do_GET()
        # A harness that loads the page in a frame as wide as the capture and reports its height.
        page = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
        width = urllib.parse.parse_qs(parsed.query).get("w", ["390"])[0]
        body = (
            "<!doctype html><body style='margin:0'>"
            f"<iframe id=f src='{urllib.parse.quote(page)}' width='{int(width)}' height='800' "
            "style='border:0'></iframe><script>"
            "f.onload=()=>{const d=f.contentDocument;"
            "document.body.textContent='HEIGHT='+Math.max("
            "d.documentElement.scrollHeight,d.body?d.body.scrollHeight:0)}"
            "</script></body>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def browser() -> str | None:
    return next((path for name in BROWSERS if (path := shutil.which(name))), None)


def serve(root: Path) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def added_mocks(root: Path) -> list[str]:
    """The HTML pages under `docs/mocks/` that the commit checked out in `root` adds or changes."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff-tree", "-r", "--root", "--no-commit-id", "--name-only",
             "--diff-filter=AM", "-z", "HEAD", "--", MOCKS],
            capture_output=True, text=True, timeout=TIMEOUT, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = sorted(n for n in done.stdout.split("\0") if n.lower().endswith((".html", ".htm")))
    return names if done.returncode == 0 else []


def usable(root: Path, page: str) -> bool:
    """A page is a relative path to an existing file inside `root`."""
    target = (root / page).resolve()
    return not Path(page).is_absolute() and target.is_relative_to(root.resolve()) and target.is_file()


def command(
    binary: str,
    profile: Path,
    target: Path | None,
    url: str,
    width: int,
    height: int,
    scale: float = 1.0,
) -> list[str]:
    """Chromium's arguments for one capture (`target`) or, with none, a dump of the page's DOM."""
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
        f"--force-device-scale-factor={scale}",
        f"--virtual-time-budget={VIRTUAL_TIME_MS}",
        f"--screenshot={target}" if target else "--dump-dom",
        url,
    ]


def run(argv: list[str]) -> tuple[str, str]:
    """The browser's stdout and the problem, if any."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", type(error).__name__
    if done.returncode:
        sys.stdout.write(done.stderr[-1000:])
        return done.stdout, f"exit {done.returncode}"
    return done.stdout, ""


def measure(binary: str, profile: Path, base: str, page: str, width: int, fallback: int) -> int:
    """The page's full height at `width`, or `fallback` if it cannot be read."""
    query = urllib.parse.urlencode({"u": "/" + page, "w": width})
    out, problem = run(command(binary, profile, None, f"{base}{MEASURE}?{query}", width, fallback))
    found = HEIGHT.search(out)
    return min(max(int(found.group(1)), fallback), MAX_HEIGHT) if found and not problem else fallback


def capture(
    binary: str, base: str, pages: Sequence[str], out: Path
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    # Every artifact fits the limit for one file, and all together the limit for the total.
    limit = min(MAX_BYTES, TOTAL_BYTES // (len(WIDTHS) * len(pages)))
    with tempfile.TemporaryDirectory(prefix="shots-") as scratch:
        for number, page in enumerate(pages, 1):
            for name, width, height in WIDTHS:
                label = f"page-{number}-{name}"
                target = out / f"{label}.png"
                profile = Path(scratch) / label
                full = measure(binary, profile / "m", base, page, width, height)
                problem = ""
                for scale in SCALES:
                    target.unlink(missing_ok=True)
                    argv = command(binary, profile, target, f"{base}/{page}", width, full, scale)
                    _, problem = run(argv)
                    if not problem and not target.is_file():
                        problem = "no screenshot written"
                    if problem or target.stat().st_size <= limit:
                        break
                    problem = f"{target.stat().st_size} bytes, over the {limit} artifact limit"
                if problem:
                    target.unlink(missing_ok=True)
                results.append((label, not problem, problem))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    parser.add_argument("pages", nargs="*", metavar="PAGE")
    args = parser.parse_args(argv)
    root: Path = args.root or HERE.parent
    pages: list[str] = args.pages or added_mocks(root)
    if not pages:
        root, pages = args.root or HERE / "screenshot_fixture", ["index.html"]
    binary = browser()
    missing = [p for p in pages if not usable(root, p)]
    if binary is None or missing:
        what = "no Chromium on PATH" if binary is None else f"no page {missing[0]} in {root}"
        sys.stdout.write(f"{what}\nscreenshots: FAIL\n")
        return 1
    if len(pages) > MAX_PAGES:
        sys.stdout.write(f"skipped, over {MAX_PAGES} pages: {', '.join(pages[MAX_PAGES:])}\n")
        pages = pages[:MAX_PAGES]
    args.out.mkdir(parents=True, exist_ok=True)
    for number, page in enumerate(pages, 1):
        sys.stdout.write(f"page-{number}: {page}\n")
    server = serve(root)
    try:
        results = capture(binary, f"http://127.0.0.1:{server.server_port}", pages, args.out)
    finally:
        server.shutdown()
    for name, ok, problem in results:
        sys.stdout.write(f"{name}: {'pass' if ok else 'FAIL ' + problem}\n")
    passed = all(ok for _, ok, _ in results)
    sys.stdout.write(f"screenshots: {'PASS' if passed else 'FAIL'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
