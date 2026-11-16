"""Repository credentials, resolved per call (ADR-037 decision 3).

The kernel side of the package asks a `RepositoryCredentials` provider for a credential
for one repository and the permissions one call needs, uses it for that call only, and
drops it. It never goes into arguments, outputs, receipts, prompts, logs, an agent's
environment or a coding session: only to the HTTP client and the `git` child of this call.

The only provider here is `FixedTestToken`, for tests and fakes. The GitHub App minter
(installation tokens restricted to one repository and the call's permissions, at most an
hour) waits on the owner's choice between an App and a personal access token (ADR-037
open question 7).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from cardea_vcs_ops.config import FixedTokenConfig, Repository

Permission = Literal["contents:write", "pull_requests:write", "issues:write", "pull_requests:read"]


@dataclass(frozen=True)
class Credential:
    """A bearer token for one call. `repr` never shows the token."""

    token: str = field(repr=False)
    expires_at: datetime

    def __str__(self) -> str:
        return "Credential(<redacted>)"


class RepositoryCredentials(Protocol):
    async def credential(
        self, repository: Repository, permissions: tuple[Permission, ...]
    ) -> Credential: ...


@dataclass(frozen=True)
class FixedTestToken:
    """Test-only provider: the same configured token for every call, valid for an hour."""

    config: FixedTokenConfig

    async def credential(
        self, repository: Repository, permissions: tuple[Permission, ...]
    ) -> Credential:
        return Credential(
            token=self.config.token.get_secret_value(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


def provider(config: FixedTokenConfig) -> RepositoryCredentials:
    """The provider the host configuration names."""
    if config.kind == "test":
        return FixedTestToken(config)
    raise ValueError(f"Unknown credential provider {config.kind!r}")
