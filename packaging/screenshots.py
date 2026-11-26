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
import html.parser
import http.server
import io
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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
MAX_PAGES = 7
# A text report of what each page shows, for a reader who cannot open images: one artifact.
REPORT = "report.txt"
REPORT_BYTES = 64 * 1024
LOG_REPORT = 6000
REPORT_TEXT = 3000
# The rendered DOM of each page, scripts and external references removed, as one HTML file.
DOM = "dom.html"
DOM_BYTES = 128 * 1024
# Tried in turn until the screenshot fits its share of the limit.
SCALES = (1.0, 0.75, 0.5, 0.35, 0.25)
MAX_HEIGHT = 10000
UNMEASURED = 2400
# Lets the page's fetch of its fixture data finish before the capture.
VIRTUAL_TIME_MS = 5000
TIMEOUT = 120
# One Chromium run, and the whole scenario, must end well inside env.try's 300 s: a hung
# browser fails fast with its own output instead of a silent timeout.
RUN_TIMEOUT = 40
DEADLINE = 240
STARTED = time.monotonic()
# Served with every HTML page: records the page's height for the capture of its full length.
MEASURE = (
    b"<script>setTimeout(() => document.documentElement.setAttribute("
    b"'data-page-height', document.documentElement.scrollHeight), 4000);"
    b"setTimeout(() => {const d = document.documentElement, w = d.clientWidth, id = (e) =>"
    b" (e.tagName + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className"
    b" ? '.' + e.className.trim().split(/\\s+/)[0] : '')).replace(/[^\\w#.-]/g, '');"
    b" d.setAttribute('data-scroll-width', d.scrollWidth); d.setAttribute('data-client-width', w);"
    b" d.setAttribute('data-wide', [...document.body.querySelectorAll('*')].filter((e) =>"
    b" e.getBoundingClientRect().right > w + 1).slice(0, 8).map(id).join(','));"
    b" d.setAttribute('data-outline', encodeURIComponent([...document.body.querySelectorAll('*')]"
    b".filter((e) => !['SCRIPT', 'STYLE'].includes(e.tagName)).slice(0, 250).map((e) => {const r ="
    b" e.getBoundingClientRect(), t = [...e.childNodes].filter((n) => n.nodeType === 3)"
    b".map((n) => n.textContent.trim()).join(' ').slice(0, 40);"
    b" return [e.tagName.toLowerCase(), t, Math.round(r.x + scrollX), Math.round(r.y + scrollY),"
    b" Math.round(r.width), Math.round(r.height), r.right > w + 1 ? 'OVERFLOW' : ''].join(' | ')})"
    b".join('\\n')))}, 4000)</script>"
    # Headless Chromium will not make its window narrower than about 500 px, so a "390 px"
    # capture is wider. Measure the page again inside a 390 px iframe of itself.
    b"<script>if (window.top === window) {let f; setTimeout(() => {f = document.createElement("
    b"'iframe'); f.style.cssText = 'position:absolute;left:0;top:0;width:390px;height:600px;"
    b"visibility:hidden;border:0'; f.src = location.href; document.body.appendChild(f)}, 500);"
    b"setTimeout(() => {try {const d = f.contentDocument.documentElement, w = d.clientWidth;"
    b" document.documentElement.setAttribute('data-narrow', [w, d.scrollWidth,"
    b" [...f.contentDocument.body.querySelectorAll('*')]"
    b".filter((e) => e.getBoundingClientRect().right > w + 1).slice(0, 5)"
    b".map((e) => e.tagName.toLowerCase() + (typeof e.className === 'string' && e.className ? '.' +"
    b" e.className.trim().split(/\\s+/)[0] : '')).join(',').replace(/[^\\w#.,-]/g, '')].join('|'))}"
    b" catch (e) {}}, 4500)}</script>"
)
# Chromium's headless window is never narrower than this: a phone width is measured in a
# frame of that width inside the page, so the page sees the width that was asked for.
MIN_WINDOW = 500
RUNNER = ""
FRAME = (
    "<!doctype html><body style=\"margin:0\"><iframe id=f src=\"{src}\" "
    "style=\"width:{width}px;height:{tall}px;border:0;display:block\"></iframe>"
    "<script>setTimeout(() => {{const i = document.getElementById('f'), "
    "d = i.contentDocument.documentElement, r = document.documentElement;"
    "for (const a of ['data-page-height', 'data-scroll-width', 'data-client-width', 'data-wide',"
    " 'data-outline']) if (d.hasAttribute(a)) r.setAttribute(a, d.getAttribute(a));"
    "const c = document.createElement('div'); c.hidden = true;"
    "c.innerHTML = i.contentDocument.body.innerHTML; document.body.appendChild(c)}}, 4500)"
    "</script>"
)
# Pages whose content was wider than the width asked for.
OVERFLOWS: list[str] = []
HEIGHT = re.compile(r'data-page-height="(\d+)"')
ATTRIBUTE = re.compile(r'data-(scroll-width|client-width|wide|outline|narrow)="([^"]*)"')
# The report lines of the run, written to REPORT at the end.
LINES: list[str] = []
# The rendered DOM of each page and width, for DOM.
DOMS: list[str] = []
# One short line per page and width, printed first so a truncated log still has it.
DIGEST: list[str] = []


class Texts(html.parser.HTMLParser):
    """The visible text of a rendered DOM, one line per text node, scripts and styles left out."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.skip += tag in {"script", "style", "head"}

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.lines.append(" ".join(data.split()))


def summary(dom: str, width: int) -> list[str]:
    """What a reader without images needs: overflow, and the page's text in reading order."""
    seen = dict(ATTRIBUTE.findall(dom))
    parser = Texts()
    parser.feed(dom)
    text = [line for line in parser.lines if line]
    out = []
    if seen.get("narrow"):
        width390, scroll390, wide390 = (seen["narrow"].split("|") + ["", "", ""])[:3]
        out.append(f"  AT 390px (iframe): viewport {width390}px, content {scroll390}px: "
                   f"{'HORIZONTAL OVERFLOW' if int(scroll390) > int(width390) else 'no overflow'}"
                   f"{' past the edge: ' + wide390 if wide390 else ''}")
    if "scroll-width" in seen:
        scroll, client = seen["scroll-width"], seen["client-width"]
        over = int(scroll) > width
        if over:
            OVERFLOWS.append(f"{width}px: content {scroll}px wide")
        out.append(f"  viewport {client}px, content {scroll}px: "
                   f"{'HORIZONTAL OVERFLOW' if over else 'no horizontal overflow'}")
        if seen["wide"]:
            out.append(f"  elements past the right edge: {seen['wide']}")
    else:
        out.append(f"  viewport {width}px (layout not measured: page not served by the runner)")
    if seen.get("outline"):
        out.append("  layout outline (tag | own text | x | y | width | height | overflow):")
        out.extend(f"    {row}" for row in urllib.parse.unquote(seen["outline"]).split("\n"))
    out.append(f"  {len(text)} text blocks; first lines:")
    shown = 0
    for line in text:
        if shown + len(line) > REPORT_TEXT:
            out.append("  ...")
            break
        out.append(f"    | {line[:200]}")
        shown += len(line)
    return out


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_head(self):  # type: ignore[no-untyped-def]
        path = Path(self.translate_path(self.path))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if query.get("frame") and path.suffix in {".html", ".htm"} and path.is_file():
            src = urllib.parse.urlsplit(self.path).path
            body = FRAME.format(
                src=html.escape(src), width=int(query["frame"][0]),
                tall=int(query.get("tall", ["800"])[0]),
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)
        if path.suffix not in {".html", ".htm"} or not path.is_file():
            return super().send_head()
        body = path.read_bytes() + MEASURE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)


class Measured(http.server.BaseHTTPRequestHandler):
    """Forwards GETs to the dashboard and appends MEASURE to its HTML, so its pages get the
    same layout report as a mock."""

    upstream = ""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        try:
            with urllib.request.urlopen(self.upstream + self.path, timeout=10) as reply:
                body, status = reply.read(), reply.status
                kind = reply.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            body, kind, status = error.read(), error.headers.get("Content-Type", ""), error.code
        except OSError:
            body, kind, status = b"", "text/plain", 502
        if kind.startswith("text/html"):
            body += MEASURE
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def measuring(upstream: str) -> http.server.ThreadingHTTPServer:
    handler = type("Handler", (Measured,), {"upstream": upstream})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


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
    # The toolchain's fonts.conf names /usr/share/fonts, which the worker does not have: write
    # one that names the toolchain's own fonts and a cache directory the worker can write.
    fonts = root / "etc" / "fonts" / "fonts.conf"
    shipped = root / "usr" / "share" / "fonts"
    if fonts.is_file() and shipped.is_dir():
        scratch = Path(tempfile.mkdtemp(prefix="fontconfig-"))
        config = scratch / "fonts.conf"
        config.write_text(
            '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
            f"<dir>{shipped}</dir><cachedir>{scratch}/cache</cachedir>"
            f"<include ignore_missing=\"yes\">{root}/etc/fonts/conf.d</include></fontconfig>"
        )
        EXTRA_ENV["FONTCONFIG_FILE"] = str(config)
        EXTRA_ENV["FONTCONFIG_PATH"] = str(root / "etc" / "fonts")
        EXTRA_ENV["XDG_CACHE_HOME"] = str(scratch)
    elif fonts.is_file():
        EXTRA_ENV["FONTCONFIG_FILE"] = str(fonts)
    return str(real)


SCRIPTS = re.compile(r"<script\b.*?</script\s*>|<link\b[^>]*>|<meta\b[^>]*>", re.S | re.I)
EXTERNAL = re.compile(r"""\s(src|href|srcset|action|poster)\s*=\s*(["'])(?!#)[^"']*\2""", re.I)
MEASURED = re.compile(
    r'\sdata-(?:page-height|scroll-width|client-width|wide|outline|narrow)="[^"]*"'
)


def sanitize(dom: str) -> str:
    """The DOM as text for a reader: no scripts, no external loads, no measuring attributes."""
    return MEASURED.sub("", EXTERNAL.sub("", SCRIPTS.sub("", dom)))


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
        # Under a memory limit a renderer cannot reserve V8's code range: run without the JIT
        # reservation, and do not wait on crashpad when it dies.
        "--js-flags=--jitless",
        "--disable-crash-reporter",
        "--disable-breakpad",
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


def lift_limits() -> None:
    """Raise the soft address-space limit to its hard one: V8 reserves a large code range
    at start and a renderer that cannot dies ("Failed to reserve virtual memory for
    CodeRange") while the browser waits for it until the timeout."""
    for name in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        _, hard = resource.getrlimit(name)
        resource.setrlimit(name, (hard, hard))


def run(argv: list[str]) -> tuple[str, str]:
    """Chromium's stdout and a problem, empty when it exited cleanly."""
    left = DEADLINE - (time.monotonic() - STARTED)
    if left < 5:
        return "", "scenario deadline reached"
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=min(RUN_TIMEOUT, left),
            check=False,
            env={**os.environ, **EXTRA_ENV} if EXTRA_ENV else None,
            preexec_fn=lift_limits,
        )
    except subprocess.TimeoutExpired as error:
        seen = error.stderr or b""
        text = seen if isinstance(seen, str) else seen.decode("utf-8", "replace")
        sys.stdout.write(f"chromium hung after {error.timeout:.0f}s: {text[-1000:]}\n")
        return "", "TimeoutExpired"
    except OSError as error:
        return "", type(error).__name__
    if done.returncode:
        sys.stdout.write(done.stderr[-1000:])
        return done.stdout, f"exit {done.returncode}"
    return done.stdout, ""


def framed(url: str, width: int, tall: int) -> str:
    """`url` inside a frame of `width` when the window cannot be that narrow."""
    if width >= MIN_WINDOW or not RUNNER or not url.startswith(RUNNER):
        return url
    return f"{url}?frame={width}&tall={tall}"


def height(binary: str, scratch: Path, url: str, width: int, tall: int) -> int:
    """The page's full height at `width`, from the DOM after it has settled."""
    shown, problem = run(
        command(binary, scratch / "measure", framed(url, width, tall), width, tall)
    )
    LINES.append(f"{width}px wide:" if not problem else f"{width}px wide: could not be read")
    if not problem:
        rows = summary(shown, width)
        LINES.extend(rows)
        DIGEST.extend(f"{url.split('/', 3)[-1] or '/'} @{width}: {r.strip()}" for r in rows[:3]
                      if "viewport" in r)
        DOMS.append(f"<!-- {url.split('/', 3)[-1] or '/'} at {width}px -->\n{sanitize(shown)}")
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
        problem = run(
            command(binary, scratch / name, framed(url, width, full), width, full, target, scale)
        )[1]
        if not problem and not target.is_file():
            problem = "no screenshot written"
        if problem:
            return problem
        if target.stat().st_size <= limit:
            return ""
    return f"{target.stat().st_size} bytes at scale {SCALES[-1]}, over its {limit} byte share"


def log_report() -> str:
    """The layout report for the log, each page cut to its own share so every page shows."""
    pages: list[list[str]] = []
    for line in LINES:
        if re.match(r"p\d+: ", line) or not pages:
            pages.append([])
        pages[-1].append(line)
    share = LOG_REPORT // max(len(pages), 1)
    return "\n".join("\n".join(page)[:share] for page in pages)


def capture(binary: str, urls: list[str], out: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    limit = min(MAX_BYTES, (MAX_TOTAL - REPORT_BYTES - DOM_BYTES) // (2 * max(len(urls), 1)))
    with tempfile.TemporaryDirectory(prefix="shots-") as scratch:
        for number, url in enumerate(urls, 1):
            name = url.split("/", 3)[-1]
            shown = name if name.startswith("docs/") or name.endswith(".html") else f"/{name}"
            sys.stdout.write(f"p{number}: {shown}\n")
            LINES.append(f"p{number}: /{url.split('/', 3)[-1]}")
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


def diagnose() -> None:
    """What the browser has for fonts, printed when it failed: a headless Chromium without
    any font dies in Skia ("SkFontMgr_FontConfigInterface ... Not implemented")."""
    sys.stdout.write(f"browser env: {EXTRA_ENV}\n")
    for folder in ("/usr/share/fonts", "/etc/fonts"):
        found = sorted(str(p) for p in Path(folder).rglob("*"))[:5] if Path(folder).is_dir() else []
        sys.stdout.write(f"{folder}: {found}\n")
    fonts = EXTRA_ENV.get("FONTCONFIG_FILE")
    present = fonts is not None and Path(fonts).is_file()
    sys.stdout.write(f"FONTCONFIG_FILE: {fonts} exists={present}\n")
    root = Path(EXTRA_ENV["LD_LIBRARY_PATH"].split(":")[0]).parents[2] if EXTRA_ENV else None
    if root:
        for sub in ("usr/share/fonts", "etc/fonts"):
            there = root / sub
            names = sorted(str(p) for p in there.rglob("*"))[:12] if there.is_dir() else []
            sys.stdout.write(f"{there}: {names}\n")


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
        sys.stdout.write(
            f"nothing to render: HEAD adds nothing under {MOCKS}/, the dashboard is unchanged, "
            "and no --page was given\nscreenshots: PASS\n"
        )
        return 0
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
    proxy: http.server.ThreadingHTTPServer | None = None
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        global RUNNER
        RUNNER = base
        urls = [f"{base}/{name}" for name in names]
        if running is not None:
            proxy = measuring(running[1])
            origin = f"http://127.0.0.1:{proxy.server_port}"
            urls += [origin + route for route in dashboard_routes(args.root)]
        skipped = urls[MAX_PAGES:]
        results = capture(binary, urls[:MAX_PAGES], args.out)
    finally:
        server.shutdown()
        if proxy is not None:
            proxy.shutdown()
        if running is not None:
            running[0].kill()
    # The log reaches a reader even when an artifact does not: the layout report, bounded.
    sys.stdout.write("layout report:\n" + log_report() + "\n")
    (args.out / REPORT).write_text("\n".join(LINES)[:REPORT_BYTES] + "\n")
    (args.out / DOM).write_text("\n".join(DOMS)[:DOM_BYTES] + "\n")
    if not all(ok for _, ok, _ in results):
        diagnose()
    for name, ok, problem in results:
        sys.stdout.write(f"{name}: {'pass' if ok else 'FAIL ' + problem}\n")
    sys.stdout.write("widths:\n" + "\n".join(DIGEST)[:1500] + "\n")
    for url in skipped:
        sys.stdout.write(f"not captured, over {MAX_PAGES} pages: {url.split('/', 3)[-1]}\n")
    for line in OVERFLOWS:
        sys.stdout.write(f"HORIZONTAL OVERFLOW at {line}\n")
    passed = all(ok for _, ok, _ in results) and not skipped and not OVERFLOWS
    sys.stdout.write(f"screenshots: {'PASS' if passed else 'FAIL'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
