"""One command from a fresh Debian host to a running agent dev team (ADR-040).

    python3 -I deploy/team-kit/teamsetup.py plan  --host HOST.json
    python3 -I deploy/team-kit/teamsetup.py apply --host HOST.json

`plan` is a dry run: it runs every step's check and verify, which only read, and prints what
`apply` would change. It exits 0 when nothing is left to do and 2 when something is.
`apply` walks the same steps in order, as root.

Every step has the same three parts (`Step`):
- `check`: is the step already done? A step that is done is not touched again.
- `action`: do it. It must be safe to run again after a crash at any point in it.
- `verify`: observe the result and say so. It is printed for every step, done or not.

Order: the host file, base packages and accounts, SPIRE identity (x509pop) and its entries,
the confined workers, the kernel, the operations, the repository registry, the tokens, email,
`render`, `provision`, `vcs.check_repository`, `apply`.

State is recorded in `setup.state` (default `/var/lib/cardea-setup/state.json`) before each
action and after each verify, so a run that stops, fails or is killed says where it stopped
and resumes there. The state only reports: whether a step is done is decided by its check,
so a drifted step is repaired and a repeated run on a converged host changes nothing.

Secrets (the repository tokens and the email credential) are behind `SecretStore`. This
module only asks whether a secret is present and usable and, when it is not, asks the store
to `provide` it. The shipped store provides nothing: it names the file to create.

The runbook's "What the host needs first" is the list of checks below:
- the kernel installed and running with host execution: steps `workers`, `kernel`;
- the `vcs` and `try` profiles and `code.session`: `workers`, `operations`;
- the repository alias in the registry with its `environment` catalog: `repository`;
- `vcs.check_repository` reporting `ready: true`: `repository-ready`;
- the operation packages and descriptors: `operations`;
- the private host file, `0600`, no placeholders, timeouts within the session wall: `team-host`;
- the base provisioning document, without the team: `kernel`;
- the supervisor's identity for the kit's client: `spire-entries`;
- tokens that do not expire within a week: `tokens`.

Host-specific paths and sources are optional `setup` settings in the host file; the defaults
are the small-host layout. `setup.spire.url` and `setup.spire.sha256` name the SPIRE release
to install when the binaries are missing, `setup.repository` (`clone_url`, `entry`) and
`setup.email` (`transport`, `recipients`) give what the registry and the email file need.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

STATE_VERSION = 1
TOKEN_WARNING_DAYS = 7
GRANT_RENEWAL_DAYS = 2
CERT_RENEWAL_SECONDS = 30 * 24 * 3600
WORKER_PROFILES = ("session", "coding", "vcs", "try")
WORKER_NAMES = ("agent", "code-session", "env-try", "vcs-check-repository")
PACKAGES = {
    "cardea-vcs-ops": "cardea_vcs_ops",
    "cardea-coding-ops": "cardea_coding_ops",
    "cardea-env-ops": "cardea_env_ops",
    "cardea-email-ops": "cardea_email_ops",
}


class StepError(Exception):
    """A step cannot be done by this tool; the message says what the host owner must do."""


class SecretUnavailable(StepError):
    """A secret is not present and the store cannot provide it."""


def load_kit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("teamkit", Path(__file__).with_name("teamkit.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Verdict:
    ok: bool
    detail: str


class SecretStore(Protocol):
    """Where tokens and email credentials live. Names are logical (`vcs:<token>`,
    `email:key`); a store decides how a name maps to storage and how a missing secret is
    obtained. Setup never reads a secret's value."""

    def status(self, name: str) -> Verdict:
        """Whether the secret is present and usable."""
        ...

    def provide(self, name: str) -> None:
        """Make the secret present, or raise `SecretUnavailable` saying what to do."""
        ...


class FileSecretStore:
    """Root-only files. It checks presence and mode and never prompts: `provide` only names
    the file the host owner must create."""

    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files

    def status(self, name: str) -> Verdict:
        path = self.files.get(name)
        if path is None:
            return Verdict(False, f"{name}: no file is configured for it")
        try:
            info = path.stat()
        except OSError:
            return Verdict(False, f"{name}: {path} is missing")
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return Verdict(False, f"{name}: {path} is not a non-empty file")
        if info.st_mode & 0o077:
            return Verdict(False, f"{name}: {path} is readable by others (needs 0600)")
        return Verdict(True, f"{name}: {path}")

    def provide(self, name: str) -> None:
        raise SecretUnavailable(
            f"{name}: create {self.files.get(name, '<unconfigured>')} (0600) with the secret, "
            "or plug in a SecretStore that obtains it"
        )


@dataclass(frozen=True)
class Result:
    code: int
    out: str


class System(Protocol):
    """Every effect on the host, so steps can be exercised without one."""

    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> Result:
        """A command that only reads."""
        ...

    def change(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> Result:
        """A command that changes the host; a nonzero exit raises `StepError`."""
        ...

    def install(
        self, path: Path, content: str, mode: int = 0o600, owner: str | None = None
    ) -> bool:
        """Write the file atomically unless it already holds the content; True if written."""
        ...


class HostSystem:
    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> Result:
        try:
            done = subprocess.run(
                argv, input=input, cwd=cwd, capture_output=True, text=True, check=False
            )
        except OSError as error:
            return Result(127, str(error))
        return Result(done.returncode, done.stdout + done.stderr)

    def change(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> Result:
        result = self.run(argv, input=input, cwd=cwd)
        if result.code:
            raise StepError(f"{' '.join(argv)} exited {result.code}: {result.out.strip()[-400:]}")
        return result

    def install(
        self, path: Path, content: str, mode: int = 0o600, owner: str | None = None
    ) -> bool:
        try:
            if path.read_text() == content and stat.S_IMODE(path.stat().st_mode) == mode:
                return False
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        if owner:
            import shutil

            shutil.chown(staged, owner, owner)
        os.replace(staged, path)
        return True


class RepositoryProbe(Protocol):
    def check(self, alias: str) -> Verdict:
        """A supervisor's `vcs.check_repository` on the alias."""
        ...


class KernelProbe:
    """Runs `vcs.check_repository` as an operation task through the kit's client. Not yet
    exercised against a real kernel: the receipt's shape is read defensively."""

    def __init__(self, host: dict[str, Any], kit: ModuleType) -> None:
        self.host, self.kit = host, kit

    def check(self, alias: str) -> Verdict:
        from cardea.service.client import ClientError

        namespace, _, supervisor, _ = self.kit._names(self.host)
        task = f"setup-check-{alias}-{datetime.now(UTC):%Y%m%d%H%M%S}"
        kernel = self.kit.client(self.host)
        try:
            body = {
                "request_id": task,
                "agent_id": supervisor,
                "namespace": namespace,
                "objective": "check the repository's rules",
                "payload": {},
                "operation": "vcs.check_repository",
                "arguments": {"repository": alias},
                "grants": ["vcs.check_repository", f"repo:{alias}"],
            }
            view = kernel.request("POST", "/tasks", json=body)
            task_id = view["task"]["task_id"]
            receipt = kernel.request("GET", f"/tasks/{task_id}/receipt")
        except ClientError as error:
            return Verdict(False, f"vcs.check_repository: {error.code}")
        finally:
            kernel.close()
        result = (receipt.get("receipt") or {}).get("result") or {}
        if result.get("ready") is True:
            return Verdict(
                True, f"{alias}: ready, host rules {result.get('host_rules', 'required')}"
            )
        missing = [
            c["name"] for c in result.get("checks", []) if c.get("required") and not c.get("ok")
        ]
        return Verdict(False, f"{alias}: not ready; missing {', '.join(missing) or 'unknown'}")


class State:
    """The record of the last run: per step `started`, `done` or `failed`. It reports and
    carries fingerprints; it never decides that a step is done."""

    def __init__(self, path: Path, system: System) -> None:
        self.path, self.system = path, system
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            loaded = {}
        steps = loaded.get("steps") if isinstance(loaded, dict) else None
        self.steps: dict[str, dict[str, Any]] = steps if isinstance(steps, dict) else {}

    def mark(self, name: str, status: str, detail: str = "", **extra: Any) -> None:
        entry = {"status": status, "detail": detail, **extra}
        # A step whose status and fingerprint stay as they were is not rewritten (its detail
        # is only a note), so a repeated run changes nothing.
        before = self.get(name)
        if (
            status == "done"
            and before.get("status") == status
            and before.get("fingerprint") == extra.get("fingerprint")
        ):
            return
        self.steps[name] = {**entry, "at": datetime.now(UTC).isoformat()}
        document = {"version": STATE_VERSION, "steps": self.steps}
        self.system.install(self.path, json.dumps(document, indent=2) + "\n")

    def get(self, name: str) -> dict[str, Any]:
        return self.steps.get(name, {})


@dataclass
class Layout:
    """Every path and name the steps use, from the host file's `setup` section."""

    host: dict[str, Any]
    kit: ModuleType
    source: Path = field(init=False)
    trust_domain: str = field(init=False)
    node: str = field(init=False)
    setup: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.setup = dict(self.host.get("setup") or {})
        self.source = Path(self.setup.get("source", "/opt/cardea"))
        self.trust_domain = str(self.host["supervisor"].get("issuer", "cardea.internal"))
        self.node = str(self.setup.get("node", "cardea"))

    def path(self, key: str, default: str) -> Path:
        return Path(str(self.setup.get(key, default)))

    @property
    def small_host(self) -> Path:
        return self.source / "deploy" / "small-host"

    @property
    def spire_dir(self) -> Path:
        return self.path("spire_dir", "/etc/spire")

    @property
    def spire_bin(self) -> Path:
        return self.path("spire_bin", "/opt/spire/bin")

    @property
    def etc(self) -> Path:
        return self.path("etc", "/etc/cardea")

    @property
    def data_dir(self) -> Path:
        return self.path("data_dir", "/var/lib/cardea")

    @property
    def systemd(self) -> Path:
        return self.path("systemd_dir", "/etc/systemd/system")

    @property
    def venv_python(self) -> Path:
        return self.source / "apps" / "api" / ".venv" / "bin" / "python"

    @property
    def registry(self) -> Path:
        return Path(str(self.host.get("repositories_config", "/opt/cardea-code/repositories.json")))

    @property
    def workers_config(self) -> Path:
        return Path(str(self.host.get("workers_config", "/etc/cardea-host-workers.json")))

    @property
    def operations_dir(self) -> Path:
        return self.path("operations_dir", "/etc/cardea/operations")

    @property
    def credentials_dir(self) -> Path:
        return self.path("credentials_dir", "/etc/cardea/credentials")

    @property
    def vcs_config(self) -> Path:
        return self.path("vcs_config", "/opt/cardea-vcs/vcs.json")

    @property
    def email_config(self) -> Path:
        return self.path("email_config", "/etc/cardea/email.json")

    @property
    def render_dir(self) -> Path:
        return self.path("render_dir", "/etc/cardea/team")

    @property
    def state(self) -> Path:
        return self.path("state", "/var/lib/cardea-setup/state.json")

    @property
    def helper(self) -> Path:
        return self.path("helper", "/usr/libexec/cardea-host")

    def template(self, *parts: str) -> str:
        return (self.small_host.joinpath(*parts)).read_text()

    def secret_files(self) -> dict[str, Path]:
        names = self.host.get("credentials") or {}
        files = {
            f"vcs:{names[role]}": self.credentials_dir / str(names[role])
            for role in ("author", "reviewer")
            if names.get(role)
        }
        transport = (self.setup.get("email") or {}).get("transport") or {}
        for key in ("api_key_file", "password_file"):
            if transport.get(key):
                files["email:key"] = Path(str(transport[key]))
        return files


@dataclass
class Context:
    host: dict[str, Any]
    host_path: Path
    layout: Layout
    kit: ModuleType
    system: System
    secrets: SecretStore
    probe: RepositoryProbe
    state: State
    connect: Callable[[dict[str, Any]], Any]
    today: Callable[[], date] = lambda: datetime.now(UTC).date()

    def read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def write_json(
        self, path: Path, document: Any, mode: int = 0o600, owner: str | None = None
    ) -> bool:
        return self.system.install(path, json.dumps(document, indent=2) + "\n", mode, owner)


class Step(ABC):
    name: str
    title: str

    @abstractmethod
    def check(self, ctx: Context) -> bool:
        """Already done? Read-only, and false (never an error) when a prerequisite is absent."""

    @abstractmethod
    def action(self, ctx: Context) -> None:
        """Do it; safe to repeat after a crash at any point."""

    @abstractmethod
    def verify(self, ctx: Context) -> Verdict:
        """Observe the result. Read-only."""

    def fingerprint(self, ctx: Context) -> str | None:
        """Inputs whose change makes a done step stale; recorded when the step completes."""
        return None


def digest(*values: Any) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def strip_expiry(value: Any) -> Any:
    """A document without its `expires_at` values, which move with the clock."""
    if isinstance(value, dict):
        return {k: strip_expiry(v) for k, v in value.items() if k != "expires_at"}
    if isinstance(value, list):
        return [strip_expiry(v) for v in value]
    return value


def expiries(value: Any) -> Iterator[datetime]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "expires_at" and isinstance(item, str):
                yield datetime.fromisoformat(item)
            else:
                yield from expiries(item)
    elif isinstance(value, list):
        for item in value:
            yield from expiries(item)


def merged(base: Any, fragment: Any) -> Any:
    """`fragment` laid over `base`: objects merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(fragment, dict):
        return {**base, **{k: merged(base.get(k), v) for k, v in fragment.items()}}
    return fragment


def has_user(ctx: Context, name: str) -> bool:
    return ctx.system.run(["id", "-u", name]).code == 0


# Steps


class TeamHost(Step):
    name = "team-host"
    title = "the private team host file is complete"

    def problems(self, ctx: Context) -> list[str]:
        found = []
        try:
            mode = stat.S_IMODE(ctx.host_path.stat().st_mode)
        except OSError:
            return [f"{ctx.host_path} is missing"]
        if mode & 0o077:
            found.append(f"{ctx.host_path} is {mode:04o}; it must be 0600")
        for path in ctx.kit.placeholders(ctx.host):
            found.append(f"placeholder value at {path}")
        found.extend(ctx.kit.wall_problems(ctx.host))
        return found

    def check(self, ctx: Context) -> bool:
        return not self.problems(ctx)

    def action(self, ctx: Context) -> None:
        if all("must be 0600" in p or "is missing" in p for p in self.problems(ctx)):
            ctx.host_path.chmod(0o600)
        remaining = self.problems(ctx)
        if remaining:
            raise StepError("edit the host file: " + "; ".join(remaining))

    def verify(self, ctx: Context) -> Verdict:
        found = self.problems(ctx)
        if found:
            return Verdict(False, "; ".join(found))
        wall = ctx.kit.session_wall(ctx.host)
        note = (
            f"session wall {wall} s"
            if wall
            else "session wall unknown until the workers file exists"
        )
        return Verdict(True, f"{ctx.host_path} is 0600, no placeholders; {note}")


class Base(Step):
    name = "base"
    title = "packages, accounts and the kernel checkout"
    packages = ("bubblewrap", "squid", "openssl", "curl")

    def missing(self, ctx: Context) -> list[str]:
        layout, found = ctx.layout, []
        for package in self.packages:
            if ctx.system.run(["dpkg", "-s", package]).code:
                found.append(f"package {package}")
        for user in ("cardea-kernel", "cardea-worker"):
            if not has_user(ctx, user):
                found.append(f"user {user}")
        probe = ctx.system.run(
            [str(layout.venv_python), "-I", "-c", "import cardea.hosts.operation_worker"]
        )
        if probe.code:
            found.append("the kernel's virtualenv with the source on its path")
        if not layout.data_dir.is_dir() or stat.S_IMODE(layout.data_dir.stat().st_mode) != 0o700:
            found.append(f"{layout.data_dir} as a 0700 directory")
        return found

    def check(self, ctx: Context) -> bool:
        return not self.missing(ctx)

    def action(self, ctx: Context) -> None:
        layout, system = ctx.layout, ctx.system
        if not (layout.source / "apps" / "api").is_dir():
            raise StepError(f"clone the repository to {layout.source} at a reviewed commit first")
        system.change(["apt-get", "install", "-y", *self.packages])
        # Debian's default squid relays anything for loopback until the workers step replaces it.
        system.change(["systemctl", "disable", "--now", "squid"])
        for user in ("cardea-kernel", "cardea-worker"):
            if not has_user(ctx, user):
                shell = "/usr/sbin/nologin"
                system.change(["useradd", "--system", "--user-group", "--shell", shell, user])
        api = layout.source / "apps" / "api"
        system.change(["uv", "sync", "--locked", "--python", "/usr/bin/python3"], cwd=api)
        site = system.change(
            [
                str(layout.venv_python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ]
        ).out.strip()
        system.install(Path(site) / "cardea-api-src.pth", f"{api / 'src'}\n", 0o644)
        system.change(
            [
                "install",
                "-d",
                "-o",
                "cardea-kernel",
                "-g",
                "cardea-kernel",
                "-m",
                "0700",
                str(layout.data_dir),
            ]
        )

    def verify(self, ctx: Context) -> Verdict:
        found = self.missing(ctx)
        if found:
            return Verdict(False, "missing: " + ", ".join(found))
        return Verdict(
            True, "packages, cardea-kernel and cardea-worker, virtualenv and store directory"
        )


class SpireIdentity(Step):
    name = "spire-identity"
    title = "SPIRE server and agent, node attestation by x509pop"

    def files(self, ctx: Context) -> dict[Path, str]:
        layout = ctx.layout
        pop = layout.spire_dir / "x509pop"
        block = re.compile(r'NodeAttestor "join_token" \{\s*plugin_data \{\}\s*\}')
        server = block.sub(
            'NodeAttestor "x509pop" {\n    plugin_data {\n'
            f'      ca_bundle_path = "{pop / "ca.crt"}"\n'
            '      agent_path_template = "/{{ .PluginName }}/{{ .Subject.CommonName }}"\n'
            "    }\n  }",
            layout.template("spire", "server.conf"),
        )
        agent = block.sub(
            'NodeAttestor "x509pop" {\n    plugin_data {\n'
            f'      private_key_path = "{pop / "agent.key"}"\n'
            f'      certificate_path = "{pop / "agent.crt"}"\n'
            "    }\n  }",
            layout.template("spire", "agent-cardea.conf"),
        ).replace("/etc/spire/bootstrap-bundle.crt", str(layout.spire_dir / "bootstrap-bundle.crt"))
        files = {layout.spire_dir / "server.conf": server, layout.spire_dir / "agent.conf": agent}
        for unit in ("spire-server.service", "spire-agent.service"):
            files[layout.systemd / unit] = layout.template("spire", unit)
        return files

    def pending(self, ctx: Context) -> list[str]:
        layout, system, found = ctx.layout, ctx.system, []
        for tool in ("spire-server", "spire-agent"):
            if not (layout.spire_bin / tool).exists():
                found.append(f"{tool} binary")
        pop = layout.spire_dir / "x509pop"
        for name in ("ca.crt", "ca.key", "agent.crt", "agent.key"):
            if not (pop / name).exists():
                found.append(f"x509pop {name}")
        if (pop / "agent.crt").exists():
            expiring = system.run(
                [
                    "openssl",
                    "x509",
                    "-checkend",
                    str(CERT_RENEWAL_SECONDS),
                    "-noout",
                    "-in",
                    str(pop / "agent.crt"),
                ]
            )
            if expiring.code:
                found.append("agent certificate expires within 30 days")
        for path, content in self.files(ctx).items():
            try:
                same = path.read_text() == content
            except OSError:
                same = False
            if not same:
                found.append(f"{path}")
        if not (layout.spire_dir / "bootstrap-bundle.crt").exists():
            found.append("bootstrap bundle")
        return found

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        layout, system = ctx.layout, ctx.system
        if (
            not (layout.spire_bin / "spire-server").exists()
            or not (layout.spire_bin / "spire-agent").exists()
        ):
            self.install_spire(ctx)
        pop = layout.spire_dir / "x509pop"
        system.change(["install", "-d", "-m", "0700", str(pop)])
        if not (pop / "ca.crt").exists() or not (pop / "ca.key").exists():
            system.change(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "ec",
                    "-pkeyopt",
                    "ec_paramgen_curve:prime256v1",
                    "-nodes",
                    "-keyout",
                    str(pop / "ca.key"),
                    "-out",
                    str(pop / "ca.crt"),
                    "-subj",
                    "/CN=cardea-node-ca",
                    "-days",
                    "3650",
                ]
            )
        expiring = (pop / "agent.crt").exists() and system.run(
            [
                "openssl",
                "x509",
                "-checkend",
                str(CERT_RENEWAL_SECONDS),
                "-noout",
                "-in",
                str(pop / "agent.crt"),
            ]
        ).code
        if expiring or not (pop / "agent.crt").exists() or not (pop / "agent.key").exists():
            self.issue_agent(ctx, pop)
        for path, content in self.files(ctx).items():
            system.install(path, content, 0o644 if path.suffix == ".service" else 0o600)
        system.change(["systemctl", "daemon-reload"])
        system.change(["systemctl", "enable", "--now", "spire-server"])
        socket = ["-socketPath", "/run/spire-server/api.sock"]
        bundle = system.change([str(layout.spire_bin / "spire-server"), "bundle", "show", *socket])
        system.install(layout.spire_dir / "bootstrap-bundle.crt", bundle.out, 0o644)
        system.change(["systemctl", "enable", "--now", "spire-agent"])
        system.change(["systemctl", "restart", "spire-agent"])

    def issue_agent(self, ctx: Context, pop: Path) -> None:
        system, node = ctx.system, ctx.layout.node
        system.install(pop / "agent.ext", "keyUsage = critical, digitalSignature\n", 0o600)
        system.change(
            [
                "openssl",
                "req",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(pop / "agent.key"),
                "-out",
                str(pop / "agent.csr"),
                "-subj",
                f"/CN={node}",
            ]
        )
        system.change(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(pop / "agent.csr"),
                "-CA",
                str(pop / "ca.crt"),
                "-CAkey",
                str(pop / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(pop / "agent.crt"),
                "-days",
                "365",
                "-extfile",
                str(pop / "agent.ext"),
            ]
        )

    def install_spire(self, ctx: Context) -> None:
        spire = ctx.layout.setup.get("spire") or {}
        if not spire.get("url") or not spire.get("sha256"):
            raise StepError(
                "SPIRE is not installed; set setup.spire.url and setup.spire.sha256 to the "
                "release tarball (small-host section 2.1) or install it under "
                + str(ctx.layout.spire_bin)
            )
        archive = Path(tempfile.gettempdir()) / "spire-release.tar.gz"
        system = ctx.system
        system.change(["curl", "-fsSL", "-o", str(archive), str(spire["url"])])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != str(spire["sha256"]).lower():
            archive.unlink()
            raise StepError(
                "the SPIRE tarball does not match setup.spire.sha256; nothing was installed"
            )
        target = ctx.layout.spire_bin.parent
        system.change(["install", "-d", "-m", "0755", str(target)])
        system.change(["tar", "-xzf", str(archive), "-C", str(target), "--strip-components=1"])

    def verify(self, ctx: Context) -> Verdict:
        agent = ctx.layout.spire_bin / "spire-agent"
        result = ctx.system.run([str(agent), "healthcheck", "-socketPath", "/run/spire/agent.sock"])
        return Verdict(
            result.code == 0, f"spire-agent healthcheck: {result.out.strip() or result.code}"
        )


class SpireEntries(Step):
    name = "spire-entries"
    title = "the kernel's and the supervisor's workload entries"

    def wanted(self, ctx: Context) -> dict[str, str]:
        layout = ctx.layout
        parent = f"spiffe://{layout.trust_domain}/spire/agent/x509pop/{layout.node}"
        runtime = f"spiffe://{layout.trust_domain}/runtime/kernel"
        return {runtime: parent, str(ctx.host["supervisor"]["subject"]): parent}

    def shown(self, ctx: Context) -> str:
        server = ctx.layout.spire_bin / "spire-server"
        return ctx.system.run(
            [str(server), "entry", "show", "-socketPath", "/run/spire-server/api.sock"]
        ).out

    def absent(self, ctx: Context) -> list[str]:
        out = self.shown(ctx)
        return [
            i
            for i in self.wanted(ctx)
            if not re.search(rf"SPIFFE ID\s*:\s*{re.escape(i)}\s", out + "\n")
        ]

    def check(self, ctx: Context) -> bool:
        return not self.absent(ctx)

    def action(self, ctx: Context) -> None:
        server = str(ctx.layout.spire_bin / "spire-server")
        for spiffe_id in self.absent(ctx):
            ctx.system.change(
                [
                    server,
                    "entry",
                    "create",
                    "-socketPath",
                    "/run/spire-server/api.sock",
                    "-parentID",
                    self.wanted(ctx)[spiffe_id],
                    "-spiffeID",
                    spiffe_id,
                    "-selector",
                    "unix:user:cardea-kernel",
                ]
            )

    def verify(self, ctx: Context) -> Verdict:
        absent = self.absent(ctx)
        if absent:
            return Verdict(False, "no entry for " + ", ".join(absent))
        return Verdict(True, f"{len(self.wanted(ctx))} entries under the x509pop node")


class Workers(Step):
    name = "workers"
    title = "the confined-worker helper, profiles and egress proxy"

    def wanted(self, ctx: Context) -> dict[Path, tuple[str, int]]:
        layout = ctx.layout
        uid = ctx.system.run(["id", "-u", "cardea-kernel"]).out.strip()
        sudoers = 'cardea-kernel ALL=(root) NOPASSWD: /usr/libexec/cardea-host ""\n'
        return {
            layout.helper: (
                (layout.source / "apps/api/src/cardea/hosts/launcher.py").read_text(),
                0o755,
            ),
            layout.path("host_config", "/etc/cardea-host.json"): (
                json.dumps({"kernel_uid": int(uid or 0)}) + "\n",
                0o644,
            ),
            layout.path("sudoers", "/etc/sudoers.d/cardea-host"): (sudoers, 0o440),
            layout.path("squid_config", "/etc/squid/squid.conf"): (
                layout.template("cardea", "squid-workers.conf"),
                0o644,
            ),
        }

    def profile_problems(self, ctx: Context) -> list[str]:
        document = ctx.read_json(ctx.layout.workers_config)
        if not isinstance(document, dict):
            return [f"{ctx.layout.workers_config} is missing"]
        profiles, workers = document.get("profiles") or {}, document.get("workers") or {}
        return [f"profile {p}" for p in WORKER_PROFILES if p not in profiles] + [
            f"worker {w}" for w in WORKER_NAMES if w not in workers
        ]

    def pending(self, ctx: Context) -> list[str]:
        found = self.profile_problems(ctx)
        for path, (content, _) in self.wanted(ctx).items():
            try:
                same = path.read_text() == content
            except OSError:
                same = False
            if not same:
                found.append(str(path))
        if ctx.system.run(["systemctl", "is-active", "--quiet", "squid"]).code:
            found.append("squid running")
        return found

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        layout, system = ctx.layout, ctx.system
        if not layout.workers_config.exists():
            system.install(
                layout.workers_config, layout.template("cardea", "cardea-host-workers.json"), 0o644
            )
        problems = self.profile_problems(ctx)
        if problems:
            # The host owner's file: never overwritten.
            raise StepError(f"{layout.workers_config} lacks " + ", ".join(problems))
        for path, (content, mode) in self.wanted(ctx).items():
            system.install(path, content, mode)
        system.change(["visudo", "-cf", str(layout.path("sudoers", "/etc/sudoers.d/cardea-host"))])
        system.change(
            [
                "squid",
                "-k",
                "parse",
                "-f",
                str(layout.path("squid_config", "/etc/squid/squid.conf")),
            ]
        )
        system.change(["systemctl", "enable", "--now", "squid"])
        system.change(["systemctl", "restart", "squid"])

    def verify(self, ctx: Context) -> Verdict:
        probe = ctx.system.run(
            ["sudo", "-u", "cardea-kernel", "sudo", "-n", str(ctx.layout.helper)],
            input='{"action":"probe"}',
        )
        problems = self.profile_problems(ctx)
        ok = probe.code == 0 and not problems
        return Verdict(
            ok,
            "helper probe ok; profiles " + ", ".join(WORKER_PROFILES)
            if ok
            else f"helper probe exit {probe.code}; {'; '.join(problems)}",
        )


class Kernel(Step):
    name = "kernel"
    title = "the kernel's configuration, base provisioning, store and service"

    def documents(self, ctx: Context) -> dict[Path, tuple[str, int]]:
        layout = ctx.layout
        config = json.loads(layout.template("cardea", "config.confined.json"))
        # The kit talks to the Unix socket; the network listener stays out unless asked for.
        config.pop("network", None)
        config.update(
            {
                "data_dir": str(layout.data_dir),
                "trust_domain": layout.trust_domain,
                "operations_dir": str(layout.operations_dir),
                "runtime_spiffe_id": f"spiffe://{layout.trust_domain}/runtime/kernel",
            }
        )
        base = {
            "principals": [
                {
                    "principal_id": "runtime",
                    "kind": "service",
                    "auth_kind": "spiffe",
                    "issuer": layout.trust_domain,
                    "subject": config["runtime_spiffe_id"],
                }
            ]
        }
        settings = ctx.host.get("provisioning") or {}
        out = {
            Path(settings.get("config", "/etc/cardea/config.json")): (
                json.dumps(config, indent=2) + "\n",
                0o600,
            ),
            layout.systemd / "cardea-kernel.service": (
                layout.template("cardea", "cardea-kernel.service"),
                0o644,
            ),
        }
        for name in ("host-execution.conf", "repositories.conf"):
            out[layout.systemd / "cardea-kernel.service.d" / name] = (
                layout.template("cardea", "cardea-kernel.service.d", name),
                0o644,
            )
        base_path = Path(settings.get("base", "/etc/cardea/provision.base.json"))
        # The base document is the host owner's once it exists; setup only creates it.
        if not base_path.exists():
            out[base_path] = (json.dumps(base, indent=2) + "\n", 0o600)
        return out

    def pending(self, ctx: Context) -> list[str]:
        found = []
        for path, (content, _) in self.documents(ctx).items():
            try:
                same = path.read_text() == content
            except OSError:
                same = False
            if not same:
                found.append(str(path))
        if not (ctx.layout.data_dir / "kernel.sqlite3").exists():
            found.append("initialised store")
        if ctx.system.run(["systemctl", "is-active", "--quiet", "cardea-kernel"]).code:
            found.append("cardea-kernel running")
        return found

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        layout, system = ctx.layout, ctx.system
        settings = ctx.host.get("provisioning") or {}
        config = settings.get("config", "/etc/cardea/config.json")
        for path, (content, mode) in self.documents(ctx).items():
            owned = "cardea-kernel" if path.suffix == ".json" else None
            system.install(path, content, mode, owner=owned)
        system.change(["systemctl", "daemon-reload"])
        if not (layout.data_dir / "kernel.sqlite3").exists():
            cardea = list(
                settings.get("cardea", [str(layout.venv_python), "-I", "-m", "cardea.service"])
            )
            system.change(["sudo", "-u", "cardea-kernel", *cardea, "init", "--config", config])
        system.change(["systemctl", "enable", "--now", "cardea-kernel"])

    def verify(self, ctx: Context) -> Verdict:
        socket = Path((ctx.host.get("kernel") or {}).get("socket", "/var/lib/cardea/kernel.sock"))
        active = ctx.system.run(["systemctl", "is-active", "--quiet", "cardea-kernel"]).code == 0
        present = socket.exists()
        return Verdict(
            active and present,
            f"cardea-kernel {'active' if active else 'not active'}; "
            f"socket {socket} {'present' if present else 'absent'}",
        )


class Operations(Step):
    name = "operations"
    title = "the operation packages and their descriptors"

    def sources(self, ctx: Context) -> dict[Path, str]:
        packages = ["cardea-vcs-ops", "cardea-coding-ops", "cardea-env-ops"]
        if (ctx.host.get("email") or {}).get("alias"):
            packages.append("cardea-email-ops")
        files = {}
        for package in packages:
            descriptors = (
                ctx.layout.source / "packages" / package / "src" / PACKAGES[package] / "descriptors"
            )
            for file in sorted(descriptors.glob("*.json")):
                files[ctx.layout.operations_dir / file.name] = file.read_text()
        return files

    def pending(self, ctx: Context) -> list[str]:
        found = []
        for path, content in self.sources(ctx).items():
            try:
                same = path.read_text() == content
            except OSError:
                same = False
            if not same:
                found.append(path.name)
        return found + self.not_installed(ctx)

    def not_installed(self, ctx: Context) -> list[str]:
        wanted = [
            p
            for p in PACKAGES
            if p != "cardea-email-ops" or (ctx.host.get("email") or {}).get("alias")
        ]
        return [
            f"package {package}"
            for package in wanted
            if ctx.system.run(
                [str(ctx.layout.venv_python), "-I", "-c", f"import {PACKAGES[package]}"]
            ).code
        ]

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        layout, system = ctx.layout, ctx.system
        for package in self.not_installed(ctx):
            name = package.removeprefix("package ")
            system.change(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(layout.venv_python),
                    str(layout.source / "packages" / name),
                ]
            )
        changed = False
        for path, content in self.sources(ctx).items():
            changed |= system.install(path, content, 0o600, owner="cardea-kernel")
        if changed:
            system.change(["systemctl", "restart", "cardea-kernel"])

    def verify(self, ctx: Context) -> Verdict:
        found = self.pending(ctx)
        if found:
            return Verdict(False, "missing " + ", ".join(found))
        return Verdict(True, f"{len(self.sources(ctx))} descriptors in {ctx.layout.operations_dir}")


class Repository(Step):
    name = "repository"
    title = "the repository registry entry, mirror and environment catalog"

    def entry(self, ctx: Context) -> Any:
        document = ctx.read_json(ctx.layout.registry)
        repositories = document.get("repositories") if isinstance(document, dict) else None
        return (repositories or {}).get(str(ctx.host["repository"]))

    def problems(self, ctx: Context) -> list[str]:
        entry = self.entry(ctx)
        if not isinstance(entry, dict):
            return [f"no entry {ctx.host['repository']!r} in {ctx.layout.registry}"]
        found = []
        if not Path(str(entry.get("source", ""))).is_dir():
            found.append(f"mirror {entry.get('source')} is missing")
        if not entry.get("gates"):
            found.append("no gates")
        if (ctx.host.get("env_try") or {}).get("scenarios"):
            environment = entry.get("environment") or {}
            for scenario in ctx.host["env_try"]["scenarios"]:
                if scenario not in (environment.get("scenarios") or {}):
                    found.append(f"environment scenario {scenario}")
            pin = str((environment.get("toolchain") or {}).get("sha256", ""))
            if not pin or set(pin) == {"0"}:
                found.append("environment toolchain is not pinned (make try-toolchain)")
        return found

    def check(self, ctx: Context) -> bool:
        return not self.problems(ctx)

    def action(self, ctx: Context) -> None:
        wanted = ctx.layout.setup.get("repository") or {}
        alias = str(ctx.host["repository"])
        if self.entry(ctx) is None:
            if not wanted.get("entry"):
                raise StepError(
                    f"add {alias!r} to {ctx.layout.registry}, or give setup.repository.entry"
                )
            document = ctx.read_json(ctx.layout.registry) or {"repositories": {}}
            document.setdefault("repositories", {})[alias] = wanted["entry"]
            ctx.write_json(ctx.layout.registry, document, 0o644)
        entry = self.entry(ctx)
        source = Path(str(entry.get("source", "")))
        if not source.is_dir():
            if not wanted.get("clone_url"):
                raise StepError(f"create the mirror {source} or give setup.repository.clone_url")
            ctx.system.change(["git", "clone", "--mirror", str(wanted["clone_url"]), str(source)])
        remaining = self.problems(ctx)
        if remaining:
            raise StepError("the registry entry needs: " + "; ".join(remaining))

    def verify(self, ctx: Context) -> Verdict:
        found = self.problems(ctx)
        if found:
            return Verdict(False, "; ".join(found))
        return Verdict(
            True,
            f"{ctx.host['repository']}: mirror, gates and environment catalog "
            f"in {ctx.layout.registry}",
        )


class Tokens(Step):
    name = "tokens"
    title = "the repository tokens, their expiry and the vcs host file"

    def names(self, ctx: Context) -> list[str]:
        credentials = ctx.host.get("credentials") or {}
        return [str(credentials[role]) for role in ("author", "reviewer") if credentials.get(role)]

    def problems(self, ctx: Context) -> list[str]:
        found = []
        for name in self.names(ctx):
            verdict = ctx.secrets.status(f"vcs:{name}")
            if not verdict.ok:
                found.append(verdict.detail)
        configured = ctx.read_json(ctx.layout.vcs_config)
        fragment = ctx.kit.vcs_fragment(ctx.host)
        if merged(configured, fragment) != configured:
            found.append(f"{ctx.layout.vcs_config} lacks the team's credential section")
        expires = ctx.host["credentials"].get("expires_on")
        try:
            last = date.fromisoformat(str(expires))
        except ValueError:
            return [*found, "credentials.expires_on is not a date"]
        days = (last - ctx.today()).days
        if days < TOKEN_WARNING_DAYS:
            found.append(
                f"tokens expire on {last} ({days} days); rotate them and update expires_on"
            )
        return found

    def check(self, ctx: Context) -> bool:
        return not self.problems(ctx)

    def action(self, ctx: Context) -> None:
        current = ctx.read_json(ctx.layout.vcs_config)
        if current is None:
            current = json.loads(ctx.layout.template("cardea", "cardea-vcs.json"))
        ctx.write_json(
            ctx.layout.vcs_config, merged(current, ctx.kit.vcs_fragment(ctx.host)), 0o644
        )
        for name in self.names(ctx):
            if not ctx.secrets.status(f"vcs:{name}").ok:
                ctx.secrets.provide(f"vcs:{name}")
        remaining = self.problems(ctx)
        if remaining:
            raise StepError("; ".join(remaining))

    def verify(self, ctx: Context) -> Verdict:
        found = self.problems(ctx)
        if found:
            return Verdict(False, "; ".join(found))
        return Verdict(
            True,
            f"{', '.join(self.names(ctx))} present, expire {ctx.host['credentials']['expires_on']}",
        )


class Email(Step):
    name = "email"
    title = "the email transport, allow-list and credential"

    def wanted(self, ctx: Context) -> Any:
        fragment = ctx.kit.email_fragment(ctx.host)
        extra = ctx.layout.setup.get("email") or {}
        return merged(fragment, {k: extra[k] for k in ("transport", "recipients") if k in extra})

    def problems(self, ctx: Context) -> list[str]:
        if not (ctx.host.get("email") or {}).get("alias"):
            return []
        configured = ctx.read_json(ctx.layout.email_config)
        found = []
        if merged(configured, self.wanted(ctx)) != configured or not (configured or {}).get(
            "transport"
        ):
            found.append(
                f"{ctx.layout.email_config} lacks the transport, recipients or the PM's role"
            )
        verdict = ctx.secrets.status("email:key")
        if not verdict.ok:
            found.append(verdict.detail)
        return found

    def check(self, ctx: Context) -> bool:
        return not self.problems(ctx)

    def action(self, ctx: Context) -> None:
        current = ctx.read_json(ctx.layout.email_config) or {}
        document = merged(current, self.wanted(ctx))
        if not document.get("transport") or not document.get("recipients"):
            raise StepError(
                "give setup.email.transport and setup.email.recipients, or fill the email file"
            )
        ctx.write_json(ctx.layout.email_config, document, 0o600, owner="cardea-kernel")
        if not ctx.secrets.status("email:key").ok:
            ctx.secrets.provide("email:key")
        remaining = self.problems(ctx)
        if remaining:
            raise StepError("; ".join(remaining))

    def verify(self, ctx: Context) -> Verdict:
        if not (ctx.host.get("email") or {}).get("alias"):
            return Verdict(True, "no email configured for the team")
        found = self.problems(ctx)
        return Verdict(
            not found, "; ".join(found) or f"alias {ctx.host['email']['alias']} and its credential"
        )


class Render(Step):
    name = "render"
    title = "the team's documents, rendered for review"

    def pending(self, ctx: Context) -> list[str]:
        found = []
        with tempfile.TemporaryDirectory() as scratch:
            for path in ctx.kit.render(ctx.host, Path(scratch)):
                existing = ctx.read_json(ctx.layout.render_dir / path.name)
                if strip_expiry(existing) != strip_expiry(json.loads(path.read_text())):
                    found.append(path.name)
        return found

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        ctx.kit.check_placeholders(ctx.host)
        ctx.kit.check_walls(ctx.host)
        ctx.kit.render(ctx.host, ctx.layout.render_dir)

    def verify(self, ctx: Context) -> Verdict:
        found = self.pending(ctx)
        return Verdict(
            not found,
            "stale: " + ", ".join(found) if found else f"documents in {ctx.layout.render_dir}",
        )


class Provision(Step):
    name = "provision"
    title = "the supervisor's principal and grants"

    def merged(self, ctx: Context) -> Any:
        settings = ctx.host.get("provisioning") or {}
        base = ctx.read_json(Path(settings.get("base", "/etc/cardea/provision.base.json")))
        if base is None:
            return None
        return ctx.kit.merge_provisioning(
            base, ctx.kit.provisioning_fragment(ctx.host), str(ctx.host["namespace"])
        )

    def pending(self, ctx: Context) -> list[str]:
        settings = ctx.host.get("provisioning") or {}
        existing = ctx.read_json(Path(settings.get("out", "/etc/cardea/provision.json")))
        wanted = self.merged(ctx)
        if wanted is None or existing is None:
            return ["provisioning document"]
        if strip_expiry(existing) != strip_expiry(wanted):
            return ["provisioning document differs from the host file"]
        soonest = min(expiries(existing), default=None)
        if soonest and soonest < datetime.now(UTC) + timedelta(days=GRANT_RENEWAL_DAYS):
            return [f"grants expire {soonest:%Y-%m-%d}"]
        if existing is not None and any(
            p.get("disabled")
            for p in existing.get("principals", [])
            if p.get("principal_id") == ctx.host["supervisor"]["principal_id"]
        ):
            return ["supervisor is disabled (team paused); resume with teamkit.py resume"]
        return []

    def check(self, ctx: Context) -> bool:
        return not self.pending(ctx)

    def action(self, ctx: Context) -> None:
        def runner(argv: list[str], check: bool = True) -> Result:
            return ctx.system.change(argv)

        ctx.kit.provision(ctx.host, paused=False, runner=runner)

    def verify(self, ctx: Context) -> Verdict:
        found = self.pending(ctx)
        return Verdict(
            not found, "; ".join(found) or f"grants valid for {ctx.host.get('grant_days', 14)} days"
        )


class RepositoryReady(Step):
    name = "repository-ready"
    title = "vcs.check_repository reports the repository ready"

    def check(self, ctx: Context) -> bool:
        return self.verify(ctx).ok

    def action(self, ctx: Context) -> None:
        verdict = self.verify(ctx)
        raise StepError(
            f"{verdict.detail}; add the missing rules on the repository host, "
            "or waive them in the registry"
        )

    def verify(self, ctx: Context) -> Verdict:
        return ctx.probe.check(str(ctx.host["repository"]))


class Apply(Step):
    name = "apply"
    title = "the collections and the agent group"

    def fingerprint(self, ctx: Context) -> str | None:
        kit = ctx.kit
        return digest(
            kit.work_collection(ctx.host),
            kit.decisions_collection(ctx.host),
            kit.group_document(ctx.host),
        )

    def exists(self, ctx: Context) -> bool:
        from cardea.service.client import ClientError

        _, group, _, _ = ctx.kit._names(ctx.host)
        try:
            kernel = ctx.connect(ctx.host)
        except Exception:
            return False
        try:
            kernel.request("GET", f"/groups/{group}", params={"namespace": ctx.host["namespace"]})
        except ClientError:
            return False
        finally:
            kernel.close()
        return True

    def check(self, ctx: Context) -> bool:
        recorded = ctx.state.get(self.name)
        return recorded.get("fingerprint") == self.fingerprint(ctx) and self.exists(ctx)

    def action(self, ctx: Context) -> None:
        ctx.kit.apply(ctx.host, ctx.connect)

    def verify(self, ctx: Context) -> Verdict:
        _, group, _, _ = ctx.kit._names(ctx.host)
        return Verdict(self.exists(ctx), f"group {group} in {ctx.host['namespace']}")


def steps() -> list[Step]:
    return [
        TeamHost(), Base(), SpireIdentity(), SpireEntries(), Workers(), Kernel(), Operations(),
        Repository(), Tokens(), Email(), Render(), Provision(), RepositoryReady(), Apply(),
    ]  # fmt: skip


def plan(ctx: Context, out: Callable[[str], None] = print) -> bool:
    """Prints each step's check and, for done steps, verify. Changes nothing. True when
    every step is done."""
    converged = True
    for number, step in enumerate(steps(), 1):
        if step.check(ctx):
            verdict = step.verify(ctx)
            out(
                f"{number:02d} {step.name}: done; "
                f"verify {'ok' if verdict.ok else 'FAILED'}: {verdict.detail}"
            )
            converged &= verdict.ok
        else:
            out(f"{number:02d} {step.name}: would run: {step.title}")
            converged = False
    return converged


def apply_all(ctx: Context, out: Callable[[str], None] = print) -> bool:
    """Walks the steps, stopping at the first that fails. True when all are done."""
    previous = {n: s.get("status") for n, s in ctx.state.steps.items()}
    for number, step in enumerate(steps(), 1):
        label = f"{number:02d} {step.name}"
        if previous.get(step.name) in ("started", "failed"):
            out(f"{label}: resuming after a {previous[step.name]} run")
        try:
            done = step.check(ctx)
            verdict = step.verify(ctx) if done else None
            if verdict is not None and verdict.ok:
                out(f"{label}: already done; verify ok: {verdict.detail}")
            else:
                if done:
                    out(
                        f"{label}: done but verify failed "
                        f"({verdict.detail if verdict else ''}); repeating"
                    )
                else:
                    out(f"{label}: {step.title}")
                ctx.state.mark(step.name, "started")
                step.action(ctx)
                verdict = step.verify(ctx)
                out(f"{label}: verify {'ok' if verdict.ok else 'FAILED'}: {verdict.detail}")
                if not verdict.ok:
                    ctx.state.mark(step.name, "failed", verdict.detail)
                    return False
            ctx.state.mark(step.name, "done", verdict.detail, fingerprint=step.fingerprint(ctx))
        except Exception as error:
            message = str(error) or type(error).__name__
            out(f"{label}: FAILED: {message}")
            ctx.state.mark(step.name, "failed", message)
            return False
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        commands.add_parser(name).add_argument("--host", type=Path, required=True)
    args = parser.parse_args(argv)
    kit = load_kit()
    host = kit.load_host(args.host)
    layout = Layout(host, kit)
    system = HostSystem()
    ctx = Context(
        host=host, host_path=args.host, layout=layout, kit=kit, system=system,
        secrets=FileSecretStore(layout.secret_files()), probe=KernelProbe(host, kit),
        state=State(layout.state, system), connect=kit.client,
    )  # fmt: skip
    if args.command == "plan":
        return 0 if plan(ctx) else 2
    return 0 if apply_all(ctx) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
