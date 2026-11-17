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
- `PersonalAccessTokens`: several such tokens, one per repository-host identity, each a
  named credential of the profile (`$CARDEA_CREDENTIALS/<name>`), chosen by the caller's
  kernel-resolved pool (ADR-037 amendment 2026-11-17): for example the developers' bot
  account, and the owner's account for the reviewer pool, because the host refuses an
  author's approval of its own pull request.
- `ProfileCredential`: the single credential with no expiry check.
- `FixedTestToken`: tests and fakes only.

The GitHub App minter (installation tokens restricted to one repository and the call's
permissions, at most an hour) is the documented upgrade, not built.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cardea.service.operations import OperationContext
from cardea_vcs_ops.config import (
    FixedTokenConfig,
    NamedToken,
    PersonalAccessTokenConfig,
    PersonalAccessTokensConfig,
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
        self,
        repository: Repository,
        permissions: tuple[Permission, ...],
        caller: OperationContext | None = None,
    ) -> Credential: ...


@dataclass(frozen=True)
class FixedTestToken:
    """Test-only provider: the same configured token for every call, valid for an hour."""

    config: FixedTokenConfig

    async def credential(
        self,
        repository: Repository,
        permissions: tuple[Permission, ...],
        caller: OperationContext | None = None,
    ) -> Credential:
        return Credential(
            token=self.config.token.get_secret_value(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


class ProfileCredential:
    """The confined worker profile's credential, read for each call (ADR-028)."""

    async def credential(
        self,
        repository: Repository,
        permissions: tuple[Permission, ...],
        caller: OperationContext | None = None,
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
        self,
        repository: Repository,
        permissions: tuple[Permission, ...],
        caller: OperationContext | None = None,
    ) -> Credential:
        check_expiry(self.config, self.today(), "the personal access token")
        token = profile_token()
        ends = datetime.combine(self.config.expires_on, time.min, tzinfo=UTC)
        return Credential(token=token, expires_at=ends)


def check_expiry(token: PersonalAccessTokenConfig | NamedToken, today: date, what: str) -> None:
    if today >= token.expires_on:
        raise CredentialUnavailable(f"{what} expired on {token.expires_on.isoformat()}")


@dataclass(frozen=True)
class PersonalAccessTokens:
    """One of several named tokens, chosen by the caller's pool (ADR-037 amendment).

    The selection uses only the kernel-resolved caller (`agent_id` and `group` from the
    member table); a caller outside every configured pool gets the default token, and
    nothing in the call's arguments takes part. `directory` defaults to
    `$CARDEA_CREDENTIALS`, the profile's credential directory."""

    config: PersonalAccessTokensConfig
    directory: Path | None = None
    today: Callable[[], date] = field(default=lambda: datetime.now(UTC).date())

    def name_for(self, caller: OperationContext | None) -> str:
        if caller is None:
            return self.config.default
        return self.config.select(caller.agent_id, caller.group)

    async def credential(
        self,
        repository: Repository,
        permissions: tuple[Permission, ...],
        caller: OperationContext | None = None,
    ) -> Credential:
        name = self.name_for(caller)
        token = self.config.tokens[name]
        check_expiry(token, self.today(), f"the {name} token")
        directory = self.directory
        if directory is None:
            named = os.environ.get("CARDEA_CREDENTIALS")
            if not named:
                raise CredentialUnavailable("this worker profile has no named credentials")
            directory = Path(named)
        secret = read_token(directory / name, f"the {name} credential")
        ends = datetime.combine(token.expires_on, time.min, tzinfo=UTC)
        return Credential(token=secret, expires_at=ends)


def profile_token() -> str:
    """The profile credential's contents, read for this call only."""
    path = os.environ.get("CARDEA_CREDENTIAL")
    if not path:
        raise CredentialUnavailable("this worker profile has no credential")
    return read_token(Path(path), "the profile credential")


def read_token(path: Path, what: str) -> str:
    try:
        token = path.read_text().strip()
    except OSError:
        raise CredentialUnavailable(f"{what} is unreadable") from None
    if not token:
        raise CredentialUnavailable(f"{what} is empty")
    return token


class CredentialUnavailable(Exception):
    """No credential for this call: nothing is sent."""


def provider(
    config: FixedTokenConfig
    | ProfileCredentialConfig
    | PersonalAccessTokenConfig
    | PersonalAccessTokensConfig,
) -> RepositoryCredentials:
    """The provider the host configuration names."""
    if isinstance(config, PersonalAccessTokensConfig):
        return PersonalAccessTokens(config)
    if isinstance(config, PersonalAccessTokenConfig):
        return PersonalAccessToken(config)
    if isinstance(config, ProfileCredentialConfig):
        return ProfileCredential()
    if config.kind == "test":
        return FixedTestToken(config)
    raise ValueError(f"Unknown credential provider {config.kind!r}")
