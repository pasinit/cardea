"""Repository credentials, resolved per call (ADR-037 decision 3).

The kernel side of the package asks a `RepositoryCredentials` provider for a credential
for one repository and the permissions one call needs, uses it for that call only, and
drops it. It never goes into arguments, outputs, receipts, prompts, logs, an agent's
environment or a coding session: only to the HTTP client and the `git` child of this call.

In the `vcs` confined worker the provider reads the worker profile's single credential
(ADR-028), which systemd loads into the unit from a root-only host secret and the worker
reads at `$CARDEA_CREDENTIAL` for each call. The service process never sees it.

- `PersonalAccessToken`: a fine-grained personal access token, the owner's choice for now
  (ADR-037 question 7). Its repositories and permissions are fixed when it is created, so
  the call's `permissions` cannot narrow it; the host's branch rules are the second guard.
  Calls are refused from its configured expiry day on.
- `ProfileCredential`: the same credential with no expiry check.
- `FixedTestToken`: tests and fakes only.

The GitHub App minter (installation tokens restricted to one repository and the call's
permissions, at most an hour) is the documented upgrade, not built.
"""

import os
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cardea_vcs_ops.config import (
    FixedTokenConfig,
    PersonalAccessTokenConfig,
    ProfileCredentialConfig,
    Repository,
)

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
        return Credential(token=profile_token(), expires_at=datetime.now(UTC) + timedelta(hours=1))


@dataclass(frozen=True)
class PersonalAccessToken:
    """A fine-grained personal access token as the profile credential (ADR-037 question 7).

    The same token serves every call until the owner rotates it. Refused, with nothing
    sent, from its configured expiry day on."""

    config: PersonalAccessTokenConfig
    today: Callable[[], date] = field(default=lambda: datetime.now(UTC).date())

    async def credential(
        self, repository: Repository, permissions: tuple[Permission, ...]
    ) -> Credential:
        if self.today() >= self.config.expires_on:
            raise CredentialUnavailable(
                f"the personal access token expired on {self.config.expires_on.isoformat()}"
            )
        token = profile_token()
        ends = datetime.combine(self.config.expires_on, time.min, tzinfo=UTC)
        return Credential(token=token, expires_at=ends)


def profile_token() -> str:
    """The profile credential's contents, read for this call only."""
    path = os.environ.get("CARDEA_CREDENTIAL")
    if not path:
        raise CredentialUnavailable("this worker profile has no credential")
    try:
        token = Path(path).read_text().strip()
    except OSError:
        raise CredentialUnavailable("the profile credential is unreadable") from None
    if not token:
        raise CredentialUnavailable("the profile credential is empty")
    return token


class CredentialUnavailable(Exception):
    """No credential for this call: nothing is sent."""


def provider(
    config: FixedTokenConfig | ProfileCredentialConfig | PersonalAccessTokenConfig,
) -> RepositoryCredentials:
    """The provider the host configuration names."""
    if isinstance(config, PersonalAccessTokenConfig):
        return PersonalAccessToken(config)
    if isinstance(config, ProfileCredentialConfig):
        return ProfileCredential()
    if config.kind == "test":
        return FixedTestToken(config)
    raise ValueError(f"Unknown credential provider {config.kind!r}")
