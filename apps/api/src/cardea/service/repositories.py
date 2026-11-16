"""The host's repository registry (ADR-037): one host-owner file both packages read.

`CARDEA_REPOSITORIES` names a JSON file `{"repositories": {<alias>: {...}}}`. Each alias
holds everything about one repository:

- `source`: the read-only mirror `code.session` clones from;
- `owner`, `name`, `api_url`, `git_url`: where `vcs.*` pushes and opens changes;
- `gates`: the targets `code.session` runs, each `{name, argv, timeout_seconds?}`;
- `branch_prefix`: where agent branches may go (default `agent/{group}/`).

A call names only an alias, authorized by the `repo:<alias>` grant. The file holds no
secret; it must still be a regular file that no one but its owner can change.

The packages' own files (`CARDEA_CODING_CONFIG`, `CARDEA_VCS_CONFIG`) used to list
repositories too. For one release they still may: their `repositories` are used when no
registry is set, with a deprecation warning, and a host where both are set and disagree
refuses to start (`check`) and refuses the call (`resolve`).
"""

import json
import logging
import os
import stat
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from cardea.service.database import Fault
from cardea.service.models import Record

log = logging.getLogger(__name__)

REGISTRY = "CARDEA_REPOSITORIES"
LEGACY = {"code.session": "CARDEA_CODING_CONFIG", "vcs": "CARDEA_VCS_CONFIG"}
MAX_BYTES = 262144
Alias = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Name = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
DEFAULT_PREFIX = "agent/{group}/"


class Gate(Record):
    name: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    argv: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=7200)] | None = None


class Repository(Record):
    source: Annotated[str, Field(strict=True, min_length=1, max_length=512)]
    owner: Name
    name: Name
    api_url: Annotated[str, Field(strict=True, pattern=r"^(https://|http://127\.0\.0\.1)")]
    git_url: Annotated[str, Field(strict=True, min_length=1, max_length=512)]
    gates: Annotated[tuple[Gate, ...], Field(max_length=16)] = ()
    branch_prefix: Annotated[str, Field(strict=True, min_length=1, max_length=64)] = (
        DEFAULT_PREFIX
    )

    @model_validator(mode="after")
    def prefix_names_the_group(self) -> Self:
        if "{group}" not in self.branch_prefix or not self.branch_prefix.endswith("/"):
            raise ValueError("branch_prefix must contain {group} and end with /")
        return self

    def gate_documents(self) -> list[dict[str, Any]]:
        """The gates as `code.session` reads them."""
        return [g.model_dump(mode="json", exclude_none=True) for g in self.gates]


class Registry(Record):
    repositories: dict[Alias, Repository]


def _read(path: Path) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_BYTES:
            raise Fault(
                "unsafe_repositories", "The repository registry must be a private regular file"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return json.loads(stream.read(MAX_BYTES + 1))
    finally:
        os.close(fd)


def load(environ: Mapping[str, str] = os.environ) -> Registry | None:
    """The registry the host owner configured, or None when `CARDEA_REPOSITORIES` is unset."""
    location = environ.get(REGISTRY)
    if not location:
        return None
    try:
        return Registry.model_validate(_read(Path(location)))
    except (OSError, ValueError, ValidationError) as error:
        reason = type(error).__name__
        raise Fault("invalid_repositories", f"The repository registry is invalid ({reason})") from None


def _deprecated(variable: str) -> None:
    message = (
        f"{variable} lists repositories; move them to the {REGISTRY} registry "
        "(the package files' repositories are removed in the next release)"
    )
    warnings.warn(message, DeprecationWarning, stacklevel=3)
    log.warning(message, extra={"code": "repositories_deprecated"})


def _coding_view(entry: Repository) -> dict[str, JsonValue]:
    return {"source": entry.source, "gates": entry.gate_documents()}  # type: ignore[dict-item]


def _vcs_view(entry: Repository) -> dict[str, JsonValue]:
    return {
        "owner": entry.owner,
        "name": entry.name,
        "api_url": entry.api_url,
        "git_url": entry.git_url,
    }


def _legacy_view(package: str, legacy: Mapping[str, Any]) -> dict[str, JsonValue]:
    if package == "code.session":
        return {
            "source": legacy.get("source"),
            "gates": [
                {k: v for k, v in gate.items() if v is not None} for gate in legacy.get("gates", ())
            ],
        }
    return {k: legacy.get(k) for k in ("owner", "name", "api_url", "git_url")}


def disagreements(
    package: str, registry: Registry, legacy: Mapping[str, Any], legacy_prefix: str | None = None
) -> list[str]:
    """Aliases whose package-file entry differs from the registry's, for `package`."""
    found = []
    for alias in sorted(set(registry.repositories) | set(legacy)):
        entry = registry.repositories.get(alias)
        if entry is None or alias not in legacy:
            found.append(alias)
            continue
        view = _coding_view(entry) if package == "code.session" else _vcs_view(entry)
        if view != _legacy_view(package, legacy[alias]):
            found.append(alias)
        elif legacy_prefix is not None and legacy_prefix != entry.branch_prefix:
            found.append(alias)
    return found


def resolve(
    package: str,
    legacy: Mapping[str, Any] | None,
    *,
    legacy_prefix: str | None = None,
    environ: Mapping[str, str] = os.environ,
) -> Registry | None:
    """The repositories one package should use.

    The registry when it is set; else None, and the package keeps its own (deprecated)
    `repositories`. A package file that still lists repositories warns, and refuses when it
    disagrees with the registry.
    """
    registry = load(environ)
    if legacy:
        _deprecated(LEGACY[package])
        if registry is not None:
            differ = disagreements(package, registry, legacy, legacy_prefix)
            if differ:
                raise Fault(
                    "repositories_disagree",
                    f"{LEGACY[package]} and {REGISTRY} disagree about: {', '.join(differ)}",
                )
    return registry


def check(environ: Mapping[str, str] = os.environ) -> None:
    """At kernel start: refuse a host whose package files and registry disagree."""
    for package, variable in LEGACY.items():
        location = environ.get(variable)
        if not location or not Path(location).exists():
            continue
        try:
            document = json.loads(Path(location).read_text())
        except (OSError, ValueError):
            continue  # the package itself reports an unreadable file at its first call
        legacy = document.get("repositories") if isinstance(document, dict) else None
        prefix = document.get("branch_prefix", DEFAULT_PREFIX) if package == "vcs" else None
        resolve(package, legacy, legacy_prefix=prefix if legacy else None, environ=environ)
