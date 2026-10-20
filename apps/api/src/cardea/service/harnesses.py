"""Installed agent harnesses: host-owner registry, selection and admission checks (ADR-033).

The registry is a host file (`harnesses_file` in the service configuration, conventionally
`/etc/cardea-host-harnesses.json`). It must not be writable by anyone but its owner, and
its owner must be root or the service user. Each entry names an adapter kind, the pinned
package or binary it runs, where its credential lives (or the confined profile that holds
it), provider settings and the models a request may select. Nothing provider-specific is
known to the kernel: it only resolves `(harness, model)` against this file.
"""

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, ValidationError, model_validator

from cardea.kernel.models import Identifier
from cardea.runtime.harness import Harness
from cardea.service.database import Fault
from cardea.service.models import (
    AgentRunRequest,
    HarnessID,
    ModelID,
    Record,
    ServiceConfig,
    TaskRequest,
)

ProfileName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
GRANT_PREFIX = "harness:"
MAX_FILE = 65536


def grant(harness_id: str) -> str:
    """The grant naming right `harness.use` on one harness: `harness:<id>`."""
    return GRANT_PREFIX + harness_id


class ModelPrice(Record):
    """USD per million tokens, used to meter harnesses that cannot report cost."""

    input_usd_per_mtok: Annotated[float, Field(ge=0, le=1000)]
    output_usd_per_mtok: Annotated[float, Field(ge=0, le=1000)]


class ModelEntry(Record):
    # Required for harnesses that cannot report cost; a free local model declares 0.
    price: ModelPrice | None = None


class Credential(Record):
    """A private file holding the harness's only credential."""

    file: Path
    kind: Literal["api_key", "oauth_token"] = "api_key"


class Confinement(Record):
    """Run the harness in a confined worker (ADR-028); the profile holds the credential."""

    profile: ProfileName
    worker: ProfileName


class Package(Record):
    """What the harness runs, pinned. A Python distribution is checked by its installed
    version; a binary by its SHA-256 (and its version is recorded for evidence)."""

    name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._@/-]+$")]
    version: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9.+_-]+$")]
    binary: Path | None = None
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def binary_is_hashed(self) -> Self:
        if (self.binary is None) != (self.sha256 is None):
            raise ValueError("A binary is pinned by its SHA-256")
        if self.binary is not None and not self.binary.is_absolute():
            raise ValueError("A harness binary path must be absolute")
        return self


class Provider(Record):
    """Where the harness sends model calls. `base_url` is any OpenAI-compatible endpoint."""

    base_url: AnyHttpUrl
    # Extra provider request headers, for example OpenRouter's attribution headers.
    headers: Annotated[dict[str, Annotated[str, Field(max_length=256)]], Field(max_length=8)] = (
        Field(default_factory=dict)
    )


class HarnessEntry(Record):
    id: HarnessID
    kind: Literal["claude-agent-sdk", "opencode"]
    package: Package
    credential: Credential | None = None
    confined: Confinement | None = None
    provider: Provider | None = None
    # None: the harness's own default, and no model may be selected.
    default_model: ModelID | None = None
    models: Annotated[dict[ModelID, ModelEntry], Field(max_length=64)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.default_model is not None and self.default_model not in self.models:
            raise ValueError(f"Harness {self.id}: default_model must be one of its models")
        if self.models and self.default_model is None:
            raise ValueError(f"Harness {self.id}: a harness with models needs a default_model")
        if self.credential is not None and self.confined is not None:
            raise ValueError(f"Harness {self.id}: a confined harness's credential is its profile's")
        if self.kind == "claude-agent-sdk":
            if self.provider is not None:
                raise ValueError(f"Harness {self.id}: the Claude Agent SDK takes no provider")
            if self.credential is None and self.confined is None:
                raise ValueError(f"Harness {self.id}: needs a credential or a confined profile")
        else:
            if self.provider is None:
                raise ValueError(f"Harness {self.id}: {self.kind} needs a provider base_url")
            if self.package.binary is None:
                raise ValueError(f"Harness {self.id}: {self.kind} runs a binary pinned by hash")
            if not self.models:
                raise ValueError(f"Harness {self.id}: {self.kind} needs at least one model")
            unpriced = [m for m, e in self.models.items() if e.price is None]
            if unpriced:
                raise ValueError(
                    f"Harness {self.id}: {self.kind} cannot report cost, so every model needs "
                    f"a price (0 for a free model): {', '.join(sorted(unpriced))}"
                )
        return self


class HarnessFile(Record):
    harnesses: Annotated[tuple[HarnessEntry, ...], Field(min_length=1, max_length=16)]
    # Concurrent agent sessions across all harnesses (one DBOS queue, ADR-027).
    concurrent_sessions: Annotated[int, Field(strict=True, ge=1, le=8)] = 1
    # The harness a request gets when it names none; a namespace may have its own.
    default: HarnessID
    namespace_defaults: Annotated[dict[Identifier, HarnessID], Field(max_length=256)] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        ids = [h.id for h in self.harnesses]
        if len(set(ids)) != len(ids):
            raise ValueError("Harness ids must be distinct")
        for name in (self.default, *self.namespace_defaults.values()):
            if name not in ids:
                raise ValueError(f"Default harness {name} is not configured")
        return self


def host_file(path: Path) -> bytes:
    """Read a host-owner file: regular, not a symlink, owned by root or the service user,
    and not group- or world-writable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise Fault(
            "harness_config_invalid", f"Cannot open {path}: {error.strerror}", 500
        ) from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
        ):
            raise Fault(
                "unsafe_file",
                f"{path} must be a regular file owned by root and writable only by its owner",
                500,
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(MAX_FILE + 1)
        if len(content) > MAX_FILE:
            raise Fault("file_too_large", f"{path} exceeds {MAX_FILE} bytes", 500)
        return content
    finally:
        os.close(fd)


def load(path: Path) -> HarnessFile:
    try:
        return HarnessFile.model_validate_json(host_file(path))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in error.errors()
        )
        raise Fault("harness_config_invalid", f"{path}: {problems}"[:1000], 500) from None


def check_pin(entry: HarnessEntry) -> None:
    """Refuse a harness whose installed package or binary is not the pinned one."""
    package = entry.package
    if package.binary is not None:
        try:
            digest = hashlib.sha256(package.binary.read_bytes()).hexdigest()
        except OSError:
            raise Fault(
                "harness_not_installed", f"Harness {entry.id}: {package.binary} is missing", 500
            ) from None
        if digest != package.sha256:
            raise Fault(
                "harness_pin_mismatch", f"Harness {entry.id}: binary does not match its pin", 500
            )
        return
    try:
        installed = version(package.name)
    except PackageNotFoundError:
        raise Fault(
            "harness_not_installed", f"Harness {entry.id}: {package.name} is not installed", 500
        ) from None
    if installed != package.version:
        raise Fault(
            "harness_pin_mismatch",
            f"Harness {entry.id}: {package.name} {installed} is installed, "
            f"{package.version} is pinned",
            500,
        )


class Catalog:
    """Installed harness entries and how a request selects one (no running adapters)."""

    def __init__(
        self,
        entries: Mapping[str, HarnessEntry],
        default: str,
        *,
        namespace_defaults: Mapping[str, str] | None = None,
        implicit: str | None = None,
    ) -> None:
        if default not in entries:
            raise ValueError("The default harness must be configured")
        self.entries = dict(entries)
        self.default = default
        self.namespace_defaults = dict(namespace_defaults or {})
        # The entry derived from `agent_session`; the only one needing no grant.
        self.implicit = implicit

    @classmethod
    def from_file(cls, file: HarnessFile) -> "Catalog":
        return cls(
            {e.id: e for e in file.harnesses},
            file.default,
            namespace_defaults=file.namespace_defaults,
        )

    def resolve(self, namespace: str, harness: str | None, model: str | None) -> tuple[str, str]:
        """The `(harness id, model id)` a request runs with, or a 422 Fault.

        An empty model id means the harness's own default.
        """
        chosen = harness or self.namespace_defaults.get(namespace, self.default)
        entry = self.entries.get(chosen)
        if entry is None:
            raise Fault("harness_not_installed", f"Harness {chosen} is not installed", 422)
        selected = model or entry.default_model
        if model is not None and model not in entry.models:
            raise Fault("model_not_allowed", f"Model {model} is not allowed on {chosen}", 422)
        return chosen, selected or ""

    def required_grant(self, harness_id: str) -> str | None:
        """The grant a run on this harness needs, or None for the implicit entry."""
        return None if harness_id == self.implicit else grant(harness_id)

    def check_grant(self, harness_id: str, grants: Iterable[str]) -> None:
        required = self.required_grant(harness_id)
        if required is not None and required not in grants:
            raise Fault(
                "harness_not_granted", f"Using harness {harness_id} needs grant {required}", 403
            )

    def check_grants(self, grants: Iterable[str]) -> None:
        """Every `harness:<id>` grant must name an installed harness."""
        for name in grants:
            if name.startswith(GRANT_PREFIX) and name[len(GRANT_PREFIX) :] not in self.entries:
                raise Fault(
                    "harness_not_installed", f"Grant {name} names no installed harness", 422
                )

    def price(self, harness_id: str, model: str) -> ModelPrice | None:
        entry = self.entries[harness_id].models.get(model)
        return entry.price if entry else None


class Harnesses(Catalog):
    """The catalog plus one running adapter per entry."""

    def __init__(
        self,
        harnesses: Mapping[str, Harness],
        catalog: Catalog,
        *,
        concurrent_sessions: int = 1,
    ) -> None:
        super().__init__(
            catalog.entries,
            catalog.default,
            namespace_defaults=catalog.namespace_defaults,
            implicit=catalog.implicit,
        )
        if set(harnesses) != set(self.entries):
            raise ValueError("Every harness entry needs an adapter")
        self.harnesses = dict(harnesses)
        self.concurrent_sessions = concurrent_sessions

    @classmethod
    def single(cls, harness: Harness, model: str | None = None) -> "Harnesses":
        """The ADR-027 `agent_session` configuration as an implicit `claude-sdk` entry."""
        entry = implicit_entry(model)
        catalog = Catalog({entry.id: entry}, entry.id, implicit=entry.id)
        return cls({entry.id: harness}, catalog)

    def all(self) -> list[Harness]:
        return list(self.harnesses.values())

    def get(self, harness_id: str) -> Harness:
        return self.harnesses[harness_id]


Builder = Callable[[HarnessEntry], Harness]


def build(file: HarnessFile, builders: Mapping[str, Builder]) -> Harnesses:
    harnesses = {}
    for entry in file.harnesses:
        if entry.kind not in builders:
            raise Fault(
                "harness_not_installed", f"Harness {entry.id}: kind {entry.kind} unavailable", 500
            )
        check_pin(entry)
        harnesses[entry.id] = builders[entry.kind](entry)
    return Harnesses(
        harnesses, Catalog.from_file(file), concurrent_sessions=file.concurrent_sessions
    )


IMPLICIT_ID = "claude-sdk"


def implicit_entry(model: str | None) -> HarnessEntry:
    """Registry entry for the ADR-027 `agent_session` block (its credential stays there)."""
    return HarnessEntry.model_construct(
        id=IMPLICIT_ID,
        kind="claude-agent-sdk",
        package=Package(name="claude-agent-sdk", version=version("claude-agent-sdk")),
        credential=None,
        confined=None,
        provider=None,
        default_model=model,
        models={model: ModelEntry()} if model else {},
    )


def check_harness_grants(
    catalog: Catalog | None, grants: Iterable[str], *, required: bool = True
) -> None:
    """Harness grants must name installed harnesses. Without a loaded catalog they are
    refused at admission (`required`) and accepted at provisioning, which may run before
    the registry is installed."""
    names = [name for name in grants if name.startswith(GRANT_PREFIX)]
    if not names:
        return
    if catalog is not None:
        catalog.check_grants(names)
    elif required:
        raise Fault("harness_not_installed", "No agent harnesses are installed", 422)


def check_selection(catalog: Catalog | None, request: TaskRequest) -> None:
    """Install-time check of an agent-run template's harness, model and grant."""
    if catalog is None or not isinstance(request, AgentRunRequest):
        return
    harness, _ = catalog.resolve(request.namespace, request.harness, request.model)
    catalog.check_grant(harness, request.grants)


def catalog(config: ServiceConfig) -> Catalog | None:
    """The installed harness catalog of a service configuration, without starting any."""
    if config.harnesses_file is not None:
        return Catalog.from_file(load(config.harnesses_file))
    if config.agent_session is not None:
        entry = implicit_entry(config.agent_session.model)
        return Catalog({entry.id: entry}, entry.id, implicit=entry.id)
    return None
