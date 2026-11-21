"""Host-owner configuration of the email operations (ADR-039).

One private JSON file, named by `CARDEA_EMAIL_CONFIG` (default `/etc/cardea/email.json`),
holds:

- `transport`: the adapter, `agentmail` (HTTP API) or `smtp_imap` (SMTP submission plus
  IMAP), and where its secret lives: a separate private file read for each call;
- `recipients`: the allow-list, alias -> address. The model names an alias, never an
  address; replies are accepted only from the address of the alias a thread went to;
- `roles`: per role (`<group>.<pool>`), the aliases it may write to and its rate limit;
- `inbound` (optional, off by default): new mail the listed aliases send to the team's
  inbox without replying to a thread, delivered to one pool's mailbox (amendment
  2026-11-21). The same sender authentication as replies applies.

Real addresses, hosts and inbox ids live only in this private host file. Nothing here is
a secret: API keys and passwords are in their own files, read inside the operation.
"""

import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardea.service.database import Fault, private_file

Alias = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Role = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
# A deliberately plain address: one local part, one domain, no display name or comments.
ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
Address = Annotated[str, Field(strict=True, max_length=320)]
Host = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9.-]{1,253}$")]
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def plain_address(value: str) -> str:
    if not ADDRESS.fullmatch(value):
        raise ValueError("an address is local@domain, without a display name")
    return value.lower()


class AgentMailTransport(Record):
    """AgentMail's HTTP API (https://docs.agentmail.to/api-reference)."""

    kind: Literal["agentmail"]
    # The inbox the agent team owns, as AgentMail names it (its address).
    inbox_id: Annotated[str, Field(strict=True, min_length=1, max_length=320)]
    api_url: Annotated[str, Field(strict=True, pattern=r"^(https://|http://127\.0\.0\.1)")] = (
        "https://api.agentmail.to"
    )
    # A private file holding the API key; read for each call, never logged or recorded.
    api_key_file: Path
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=60)] = 20
    # Weakens the reply check: accept replies whose sender is not authenticated (only the
    # From header then identifies them, and anyone can forge it). For servers that report
    # no verdicts; off by default (ADR-039).
    accept_unauthenticated: bool = False


class Endpoint(Record):
    host: Host
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    # `tls` connects with TLS, `starttls` upgrades; `none` is for a loopback fake only.
    security: Literal["tls", "starttls", "none"]
    username: Annotated[str, Field(strict=True, min_length=1, max_length=320)]
    # A private file holding the password (for example a provider app password).
    password_file: Path

    @model_validator(mode="after")
    def no_plaintext_off_host(self) -> Self:
        if self.security == "none" and self.host not in LOOPBACK:
            raise ValueError("security none is allowed only on loopback")
        return self


class ImapEndpoint(Endpoint):
    # The folder replies arrive in; it is opened read-only (EXAMINE).
    folder: Annotated[str, Field(strict=True, min_length=1, max_length=200)] = "INBOX"
    # The folder the server copies submitted mail into, if it does: the reconciler looks
    # there for an interrupted send. Without it, an interrupted send stays uncertain.
    sent_folder: Annotated[str, Field(strict=True, min_length=1, max_length=200)] | None = None
    # The authserv-id the receiving server writes in its RFC 8601 Authentication-Results
    # header (for example `mx.google.com`). Only that header's verdicts are trusted; without
    # it every reply's authentication is unknown and the reply is dropped.
    authserv_id: Host | None = None


class SmtpImapTransport(Record):
    """Standard SMTP submission (RFC 6409) and IMAP4rev1 (RFC 3501)."""

    kind: Literal["smtp_imap"]
    from_address: Address
    # Right-hand side of the Message-IDs this host mints (RFC 5322 section 3.6.4).
    message_id_domain: Host
    smtp: Endpoint
    imap: ImapEndpoint
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=60)] = 20
    # Weakens the reply check: accept replies whose sender is not authenticated (only the
    # From header then identifies them, and anyone can forge it). For servers that report
    # no verdicts; off by default (ADR-039).
    accept_unauthenticated: bool = False

    @model_validator(mode="after")
    def addresses(self) -> Self:
        plain_address(self.from_address)
        return self


class RateLimit(Record):
    count: Annotated[int, Field(strict=True, ge=1, le=1000)]
    window_days: Annotated[int, Field(strict=True, ge=1, le=90)]


class RoleConfig(Record):
    recipients: Annotated[tuple[Alias, ...], Field(min_length=1, max_length=32)]
    rate_limit: RateLimit


class InboundTarget(Record):
    """The mailbox inbound mail goes to: a group pool, `<group>.<pool>`, in a namespace."""

    namespace: Role
    pool: Annotated[
        str,
        Field(
            strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}\.[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"
        ),
    ]


class InboundConfig(Record):
    """New messages to the inbox that are on no thread Cardea started (amendment
    2026-11-21). Only mail from the addresses of `from` is read or accepted, only with an
    authenticated sender (or `accept_unauthenticated`), at most `max_bytes` of text, at
    most `rate_limit` deliveries per window, and none older than `window_days`."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    senders: Annotated[tuple[Alias, ...], Field(alias="from", min_length=1, max_length=16)]
    deliver_to: InboundTarget
    rate_limit: RateLimit
    max_bytes: Annotated[int, Field(strict=True, ge=256, le=12288)] = 8192
    window_days: Annotated[int, Field(strict=True, ge=1, le=30)] = 7


class EmailConfig(Record):
    transport: Annotated[AgentMailTransport | SmtpImapTransport, Field(discriminator="kind")]
    recipients: Annotated[dict[Alias, Address], Field(min_length=1, max_length=64)]
    roles: dict[Role, RoleConfig]
    # Reply text kept for the agent; longer replies are shortened and marked.
    max_reply_bytes: Annotated[int, Field(strict=True, ge=256, le=12288)] = 8192
    inbound: InboundConfig | None = None

    @model_validator(mode="after")
    def known_aliases(self) -> Self:
        for address in self.recipients.values():
            plain_address(address)
        for role, entry in self.roles.items():
            unknown = set(entry.recipients) - set(self.recipients)
            if unknown:
                raise ValueError(f"role {role} names unknown recipients {sorted(unknown)}")
        if self.inbound is not None and set(self.inbound.senders) - set(self.recipients):
            raise ValueError("inbound names unknown recipients")
        return self

    def address(self, alias: str) -> str | None:
        found = self.recipients.get(alias)
        return found.lower() if found is not None else None


def load(path: Path | None = None) -> EmailConfig:
    """Read the private host-owner file."""
    location = path or Path(os.environ.get("CARDEA_EMAIL_CONFIG", "/etc/cardea/email.json"))
    data = private_file(location)
    try:
        return EmailConfig.model_validate_json(data)
    except ValueError:
        # Never echo the file or the validation error: they hold addresses.
        raise Fault("email_config_invalid", "The email configuration is invalid", 500) from None


def secret(path: Path) -> str:
    """A credential from its private file, for one call. Never logged or returned."""
    try:
        value = private_file(path).decode().strip()
    except (OSError, UnicodeDecodeError, Fault):
        raise CredentialUnavailable("the email credential is unreadable") from None
    if not value:
        raise CredentialUnavailable("the email credential is empty")
    return value


class CredentialUnavailable(Exception):
    """No credential for this call: nothing is sent."""
