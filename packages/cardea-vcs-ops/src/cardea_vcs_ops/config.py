"""Host-owner configuration of the vcs operations (ADR-037).

One private JSON file, named by `CARDEA_VCS_CONFIG`, holds whether `APPROVE` reviews are
allowed and the credential provider. Repository aliases, their hosts and branch prefixes
come from the host's repository registry (`CARDEA_REPOSITORIES`); for one release this
file may still list `repositories` and a global `branch_prefix`, with a deprecation
warning. Callers name an alias; they never send a URL, a token or a host.

In the `vcs` confined worker (ADR-037) the file holds no secret: the provider is
`personal_access_token` (a fine-grained token, the owner's choice for now) or
`profile_credential`, both read from the worker profile's own credential, which systemd
loads into the unit. Such a file may be a shared, read-only host-owner file (root-owned, not
writable by others), bound read-only into the sandbox. A file that holds a token must
be private to the reading account.
"""

import os
from datetime import date
import re
import stat
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from cardea.service.database import Fault, private_file

Alias = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Name = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
# A group pool, `<group>.<pool>`: the names ADR-034 allows, which hold no dots.
PoolAddress = Annotated[
    str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,39}\.[a-z][a-z0-9-]{0,31}$")
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Repository(Record):
    """One repository on a GitHub-compatible host."""

    owner: Name
    name: Name
    # REST API base, for example `https://api.github.com`.
    api_url: Annotated[str, Field(strict=True, pattern=r"^(https://|http://127\.0\.0\.1)")]
    # Where branches are pushed, for example `https://github.com/<owner>/<name>.git`.
    git_url: Annotated[str, Field(strict=True, min_length=1, max_length=512)]


class FixedTokenConfig(Record):
    """A fixed token for tests and fakes only; the GitHub App minter comes later."""

    kind: Literal["test"]
    # Never printed: pydantic masks a SecretStr in repr, str and JSON dumps.
    token: Annotated[SecretStr, Field(min_length=8, max_length=512)]


class ProfileCredentialConfig(Record):
    """The confined worker profile's credential, read per call from `$CARDEA_CREDENTIAL`
    (ADR-028): a token for now; a GitHub App key once the minter exists."""

    kind: Literal["profile_credential"]


class PersonalAccessTokenConfig(Record):
    """A fine-grained personal access token held as the `vcs` profile's credential
    (ADR-037 question 7, decided by the owner 2026-11-16).

    The token itself is never in this file: systemd loads it into the worker unit from a
    root-only host secret, and the worker reads it at `$CARDEA_CREDENTIAL` for each call,
    as `profile_credential` does. `expires_on` is the expiry the owner set when creating
    it; from that day on every call is refused with nothing sent, so a lapsed token fails
    loudly instead of as a host 401."""

    kind: Literal["personal_access_token"]
    expires_on: date


# Providers whose secret lives outside the configuration file, so the file may be shared.
NoSecret = ProfileCredentialConfig | PersonalAccessTokenConfig


class VcsConfig(Record):
    # Deprecated for one release: repositories come from the host's repository registry
    # (`CARDEA_REPOSITORIES`, `cardea.service.repositories`). When both are set they must
    # agree, or the kernel refuses to start and these operations refuse every call.
    repositories: dict[Alias, Repository] = Field(default_factory=dict)
    # Pushed branches must start with this, after `{group}` is replaced by the caller's
    # group (ADR-037 open question 3; the lead recommends one prefix per group).
    branch_prefix: Annotated[str, Field(strict=True, min_length=1, max_length=64)] = (
        "agent/{group}/"
    )
    # `APPROVE` reviews (ADR-037 question 2, decided by the owner 2026-11-16): refused by
    # default. `true` allows them for every caller; a list of pools, each `<group>.<pool>`
    # as in mail addresses, allows them only for members of those pools.
    allow_approve: bool | Annotated[tuple[PoolAddress, ...], Field(max_length=32)] = False
    credentials: Annotated[
        FixedTokenConfig | ProfileCredentialConfig | PersonalAccessTokenConfig,
        Field(discriminator="kind"),
    ]

    @model_validator(mode="after")
    def prefix_names_the_group(self) -> Self:
        if "{group}" not in self.branch_prefix or not self.branch_prefix.endswith("/"):
            raise ValueError("branch_prefix must contain {group} and end with /")
        if not re.fullmatch(r"[A-Za-z0-9._/-]*\{group\}[A-Za-z0-9._/-]*", self.branch_prefix):
            raise ValueError("branch_prefix may hold only branch-name characters")
        return self


def load(path: Path | None = None) -> VcsConfig:
    """Read the host-owner file: private to this account, or, when it holds no secret,
    a read-only shared file that only root or this account can change."""
    location = path or Path(os.environ.get("CARDEA_VCS_CONFIG", "/etc/cardea/vcs.json"))
    try:
        return VcsConfig.model_validate_json(private_file(location))
    except Fault as refused:
        if refused.code != "unsafe_file":
            raise
    config = VcsConfig.model_validate_json(shared_file(location))
    if not isinstance(config.credentials, NoSecret):
        raise Fault("unsafe_file", "A vcs configuration with a token must be private", 500)
    return config


def shared_file(path: Path) -> bytes:
    """A regular file owned by root or this account that nobody else may change."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022
            or info.st_size > 262144
        ):
            raise Fault("unsafe_file", "The vcs configuration may not be shared writable", 500)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(262145)
    finally:
        os.close(fd)
