"""Host-owner configuration of the vcs operations (ADR-037).

One private JSON file, named by `CARDEA_VCS_CONFIG`, holds whether `APPROVE` reviews are
allowed and the credential provider. Repository aliases, their hosts and branch prefixes
come from the host's repository registry (`CARDEA_REPOSITORIES`); for one release this
file may still list `repositories` and a global `branch_prefix`, with a deprecation
warning. Callers name an alias; they never send a URL, a token or a host.
"""

import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from cardea.service.database import private_file

Alias = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Name = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,100}$")]


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
    # `APPROVE` reviews are refused unless the host owner enables them (open question 2).
    allow_approve: bool = False
    credentials: FixedTokenConfig

    @model_validator(mode="after")
    def prefix_names_the_group(self) -> Self:
        if "{group}" not in self.branch_prefix or not self.branch_prefix.endswith("/"):
            raise ValueError("branch_prefix must contain {group} and end with /")
        if not re.fullmatch(r"[A-Za-z0-9._/-]*\{group\}[A-Za-z0-9._/-]*", self.branch_prefix):
            raise ValueError("branch_prefix may hold only branch-name characters")
        return self


def load(path: Path | None = None) -> VcsConfig:
    """Read the host-owner file: private, root- or service-owned, not world-readable."""
    location = path or Path(os.environ.get("CARDEA_VCS_CONFIG", "/etc/cardea/vcs.json"))
    return VcsConfig.model_validate_json(private_file(location))
