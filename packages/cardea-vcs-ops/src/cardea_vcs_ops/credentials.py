"""Repository credentials, resolved per call (ADR-037 decision 3).

The kernel side of the package asks a `RepositoryCredentials` provider for a credential
for one repository and the permissions one call needs, uses it for that call only, and
drops it. It never goes into arguments, outputs, receipts, prompts, logs, an agent's
environment or a coding session: only to the HTTP client and the `git` child of this call.

In the `vcs` confined worker the provider is `ProfileCredential`: the worker profile's
single credential (ADR-028), which systemd loads into the unit and the worker reads at
`$CARDEA_CREDENTIAL` for each call. The service process never sees it. `FixedTestToken`
is for tests and fakes. The GitHub App minter
(installation tokens restricted to one repository and the call's permissions, at most an
hour) waits on the owner's choice between an App and a personal access token (ADR-037
open question 7).
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cardea_vcs_ops.config import FixedTokenConfig, ProfileCredentialConfig, Repository

Permission = Literal[
    "contents:write",
    "pull_requests:write",
    "issues:write",
    "pull_requests:read",
    "metadata:read",
]


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


class ProfileCredential:
    """The confined worker profile's credential, read for each call (ADR-028)."""

    async def credential(
        self, repository: Repository, permissions: tuple[Permission, ...]
    ) -> Credential:
        path = os.environ.get("CARDEA_CREDENTIAL")
        if not path:
            raise CredentialUnavailable("this worker profile has no credential")
        try:
            token = Path(path).read_text().strip()
        except OSError:
            raise CredentialUnavailable("the profile credential is unreadable") from None
        if not token:
            raise CredentialUnavailable("the profile credential is empty")
        return Credential(token=token, expires_at=datetime.now(UTC) + timedelta(hours=1))


class CredentialUnavailable(Exception):
    """No credential for this call: nothing is sent."""


def provider(config: FixedTokenConfig | ProfileCredentialConfig) -> RepositoryCredentials:
    """The provider the host configuration names."""
    if isinstance(config, ProfileCredentialConfig):
        return ProfileCredential()
    if config.kind == "test":
        return FixedTestToken(config)
    raise ValueError(f"Unknown credential provider {config.kind!r}")
