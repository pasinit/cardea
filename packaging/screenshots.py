"""The `screenshots` scenario's runner for `env.try`: the mock pages a commit adds, in
headless Chromium, full page, at 390 px and at desktop width.

Usage (`make screenshots`):

    python3 packaging/screenshots.py [--root DIR] [--page PATH] [--out DIR]

Serves `--root` (default the current directory, the checkout) on an ephemeral port of the
loopback interface. The pages are the HTML files HEAD adds under `docs/mocks/`, or just
`--page` (a path under the root) when it is given. Each page gets two screenshots as tall as
the page, `p<N>-mobile-390.png` and `p<N>-desktop-1280.png` (N from 1, in path order, at most
`MAX_PAGES`), written to `--out` (default `screenshots`), the scenario's artifact paths; the
log names the page behind each N. `env.try` keeps at most 256 KiB per artifact and 1 MiB in
all, so a screenshot over its share is retaken at a lower device pixel ratio, and a page that
still does not fit fails. It needs only a loopback, so the catalog declares `network:
loopback` and the scenario runs in the second phase, whose unit has no proxy and no route
out. Chromium is also told to resolve nothing but `127.0.0.1`, so a page that names another
host fails to load rather than reaching for it. The last line is `screenshots: PASS` or
`screenshots: FAIL`; the exit status agrees.
"""

import argparse
import functools
import http.server
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path

WIDTHS: tuple[tuple[str, int, int], ...] = (("mobile-390", 390, 844), ("desktop-1280", 1280, 800))
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
MOCKS = "docs/mocks"
# env.try keeps artifacts of at most 256 KiB each and 1 MiB in all, and a scenario's artifact
# paths are fixed in the catalog: two per page, so at most 16 files, 8 pages.
MAX_BYTES = 256 * 1024
MAX_TOTAL = 1024 * 1024
MAX_PAGES = 8
# Tried in turn until the screenshot fits its share of the limit.
SCALES = (1.0, 0.75, 0.5, 0.35, 0.25)
MAX_HEIGHT = 10000
UNMEASURED = 2400
# Lets the page's fetch of its fixture data finish before the capture.
VIRTUAL_TIME_MS = 5000
TIMEOUT = 120
# Served with every HTML page: records the page's height for the capture of its full length.
MEASURE = (
    b"<script>setTimeout(() => document.documentElement.setAttribute("
    b"'data-page-height', document.documentElement.scrollHeight), 4000)</script>"
)
HEIGHT = re.compile(r'data-page-height="(\d+)"')


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_head(self):  # type: ignore[no-untyped-def]
        path = Path(self.translate_path(self.path))
        if path.suffix not in {".html", ".htm"} or not path.is_file():
            return super().send_head()
        body = path.read_bytes() + MEASURE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)


# Set when the browser is a toolchain's Chromium started directly (see `direct`).
EXTRA_ENV: dict[str, str] = {}


def browser() -> str | None:
    found = next((path for name in BROWSERS if (path := shutil.which(name))), None)
    return direct(found) if found else None


def direct(found: str) -> str:
    """A pinned toolchain's Chromium, started without Debian's wrapper script.

    `env.try` mounts the toolchain under its own root (not at /) and the worker sees no
    /etc. The wrapper (`<root>/usr/bin/chromium`) sources /etc/chromium.d/* and dies there,
    and the real binary needs the toolchain's own libraries and fontconfig. So run
    `<root>/usr/lib/chromium/chromium` with LD_LIBRARY_PATH and FONTCONFIG_FILE pointing
    into the toolchain. A Chromium installed at / keeps its wrapper.
    """
    path = Path(found).resolve()
    root = path.parent.parent.parent
    real = root / "usr" / "lib" / "chromium" / "chromium"
    if path.parent.name != "bin" or root == Path("/") or not real.is_file():
        return found
    libraries = [
        folder
        for arch in sorted(p.name for p in (root / "usr" / "lib").glob("*-linux-gnu*"))
        for folder in (
            root / "usr" / "lib" / arch,
            root / "lib" / arch,
            root / "usr" / "lib" / arch / "pulseaudio",
        )
        if folder.is_dir()
    ] + [real.parent]
    EXTRA_ENV["LD_LIBRARY_PATH"] = ":".join(str(folder) for folder in libraries)
    fonts = root / "etc" / "fonts" / "fonts.conf"
    if fonts.is_file():
        EXTRA_ENV["FONTCONFIG_FILE"] = str(fonts)
    return str(real)


def serve(root: Path) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def command(
    binary: str,
    profile: Path,
    url: str,
    width: int,
    height: int,
    target: Path | None = None,
    scale: float = 1.0,
) -> list[str]:
    """Chromium's argv: a screenshot into `target`, or the page's DOM on stdout without one."""
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
        "--dump-dom" if target is None else f"--screenshot={target}",
        url,
    ]


def pages(root: Path, page: str | None) -> list[str]:
    """The pages to capture: `page` alone, else the HTML files HEAD adds under docs/mocks."""
    if page:
        return [page]
    listing = subprocess.run(
        ["git", "-C", str(root), "show", "-m", "--first-parent", "--no-renames", "-z",
         "--diff-filter=A", "--name-only", "--format=", "HEAD", "--", MOCKS],
        capture_output=True, text=True, timeout=TIMEOUT, check=False,
    )  # fmt: skip
    names = listing.stdout.split("\0") if listing.returncode == 0 else []
    return sorted(n for n in names if n.lower().endswith((".html", ".htm")))


def run(argv: list[str]) -> tuple[str, str]:
    """Chromium's stdout and a problem, empty when it exited cleanly."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
            env={**os.environ, **EXTRA_ENV} if EXTRA_ENV else None,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", type(error).__name__
    if done.returncode:
        sys.stdout.write(done.stderr[-1000:])
        return done.stdout, f"exit {done.returncode}"
    return done.stdout, ""


def height(binary: str, scratch: Path, url: str, width: int, tall: int) -> int:
    """The page's full height at `width`, from the DOM after it has settled."""
    shown, problem = run(command(binary, scratch / "measure", url, width, tall))
    found = None if problem else HEIGHT.search(shown)
    # A page we did not serve (the dashboard) has no measuring script: capture a long window.
    return min(max(int(found.group(1)), tall), MAX_HEIGHT) if found else max(tall, UNMEASURED)


def shoot(
    binary: str, scratch: Path, url: str, spec: tuple[str, int, int], target: Path, limit: int
) -> str:
    """One full-page screenshot into `target` within `limit` bytes; the problem, if any."""
    name, width, tall = spec
    full = height(binary, scratch, url, width, tall)
    problem = ""
    for scale in SCALES:
        target.unlink(missing_ok=True)
        problem = run(command(binary, scratch / name, url, width, full, target, scale))[1]
        if not problem and not target.is_file():
            problem = "no screenshot written"
        if problem:
            return problem
        if target.stat().st_size <= limit:
            return ""
    return f"{target.stat().st_size} bytes at scale {SCALES[-1]}, over its {limit} byte share"


def capture(binary: str, urls: list[str], out: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    limit = min(MAX_BYTES, MAX_TOTAL // (2 * max(len(urls), 1)))
    with tempfile.TemporaryDirectory(prefix="shots-") as scratch:
        for number, url in enumerate(urls, 1):
            sys.stdout.write(f"p{number}: {url.split('/', 3)[-1] or '/'}\n")
            for kind, width, tall in WIDTHS:
                label = f"p{number}-{kind}"
                target = out / f"{label}.png"
                problem = shoot(binary, Path(scratch), url, (label, width, tall), target, limit)
                results.append((label, not problem, problem))
    return results


# The team dashboard, rendered from the commit's own code against its recorded fixtures, when
# the commit changes it (compared with its base): UX reviews the real page, not only a mock.
DASHBOARD = "deploy/team-kit"
DASHBOARD_FILES = ("dashboard",)
DASHBOARD_SERVER = f"{DASHBOARD}/dashboard_teams.py"
DASHBOARD_FIXTURES = (
    ("dev", "dashboard.fixture.json"),
    ("aerarium", "dashboard.fixture.aerarium.json"),
)
# Extra routes a commit wants captured, one per line (for example a member page), else "/".
DASHBOARD_ROUTES = f"{DASHBOARD}/screenshots.routes"


def dashboard_changed(root: Path) -> bool:
    base = ""
    for ref in ("origin/main", "origin/HEAD", "main"):
        found = subprocess.run(
            ["git", "-C", str(root), "merge-base", "HEAD", ref],
            capture_output=True, text=True, timeout=TIMEOUT, check=False,
        )  # fmt: skip
        if found.returncode == 0 and found.stdout.strip():
            base = found.stdout.strip()
            break
    if not base:
        return False
    listed = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{base}..HEAD", "--", DASHBOARD],
        capture_output=True, text=True, timeout=TIMEOUT, check=False,
    )  # fmt: skip
    return any(Path(name).name.startswith(DASHBOARD_FILES) for name in listed.stdout.splitlines())


def dashboard_routes(root: Path) -> list[str]:
    path = root / DASHBOARD_ROUTES
    routes = path.read_text().splitlines() if path.is_file() else []
    cleaned = [r.strip() for r in routes if r.strip().startswith("/")]
    return cleaned or ["/"]


def start_dashboard(root: Path) -> tuple[subprocess.Popen[bytes], str] | None:
    """The commit's dashboard on a free loopback port, reading its fixtures; None if it fails."""
    probe = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    port = probe.server_port
    probe.server_close()
    fixtures = [f"{name}={root / DASHBOARD / file}" for name, file in DASHBOARD_FIXTURES]
    process = subprocess.Popen(
        [sys.executable, "-I", str(root / DASHBOARD_SERVER), "--fixtures", *fixtures,
         "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(url + "/", timeout=1):
                return process, url
        except OSError:
            if process.poll() is not None:
                return None
            time.sleep(0.1)
    process.kill()
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--page", default=None)
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    args = parser.parse_args(argv)
    binary = browser()
    names = [] if binary is None else pages(args.root, args.page)
    dashboard = binary is not None and not args.page and dashboard_changed(args.root)
    problem = ""
    if binary is None:
        problem = "no Chromium on PATH"
    elif not names and not dashboard:
        problem = (
            f"no page: HEAD adds nothing under {MOCKS}/, the dashboard is unchanged, "
            "and no --page was given"
        )
    else:
        root = args.root.resolve()
        for name in names:
            target = (root / name).resolve()
            if not target.is_file() or not target.is_relative_to(root):
                problem = f"no page {name} in {args.root}"
    if problem or binary is None:
        sys.stdout.write(f"{problem}\nscreenshots: FAIL\n")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    server = serve(args.root)
    running = start_dashboard(args.root) if dashboard else None
    if dashboard and running is None:
        server.shutdown()
        sys.stdout.write("the dashboard did not start\nscreenshots: FAIL\n")
        return 1
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        urls = [f"{base}/{name}" for name in names]
        if running is not None:
            urls += [running[1] + route for route in dashboard_routes(args.root)]
        skipped = urls[MAX_PAGES:]
        results = capture(binary, urls[:MAX_PAGES], args.out)
    finally:
        server.shutdown()
        if running is not None:
            running[0].kill()
    for name, ok, problem in results:
        sys.stdout.write(f"{name}: {'pass' if ok else 'FAIL ' + problem}\n")
    for url in skipped:
        sys.stdout.write(f"not captured, over {MAX_PAGES} pages: {url.split('/', 3)[-1]}\n")
    passed = all(ok for _, ok, _ in results) and not skipped
    sys.stdout.write(f"screenshots: {'PASS' if passed else 'FAIL'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
