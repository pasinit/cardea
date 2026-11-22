"""The transport interface every email adapter implements (ADR-039).

`send` sends one message, `find_sent` looks for one an interrupted call may have sent,
`poll_replies` reads the replies on one thread Cardea created, and `poll_inbound` reads
new messages from given addresses that are on no thread Cardea created (amendment
2026-11-21). Adapters read their
credential for each call and never put it in an exception, a return value or a log.

Failures:
- `Refused`: the provider definitely did not send (a refused recipient, a bad request,
  a missing credential). The call fails with nothing sent.
- any other exception from `send`: the outcome is unknown, and the kernel's reconciler
  (`find_sent`) settles it from the provider's state.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Outgoing:
    to: str
    subject: str
    body: str
    # The call's ADR-026 effect key: the provider idempotency key and the Message-ID.
    effect_key: str


@dataclass(frozen=True)
class Sent:
    # The id replies are matched by: AgentMail's thread id, or our RFC 5322 Message-ID.
    thread_id: str
    message_id: str


@dataclass(frozen=True)
class Thread:
    thread_id: str
    message_id: str
    sent_at: datetime


@dataclass(frozen=True)
class Inbound:
    """One message on a thread, as the provider reports it. Untrusted data."""

    reply_id: str
    sender: str
    subject: str
    text: str
    received_at: str
    # Our own message on the thread (the send itself), never a reply.
    own: bool = False
    # True only when the From domain is authenticated (DMARC pass, or an SPF or DKIM pass
    # aligned with it); False when the verdicts say it is not; None when unknown.
    authenticated: bool | None = None
    spam: bool = False
    # The provider's thread (AgentMail), or empty where threads are headers (IMAP).
    thread_id: str = ""
    # The text's size before any shortening, in bytes, as the provider reports it.
    size: int | None = None


class Refused(Exception):
    """The provider definitely did not send. `code` becomes the failure code."""

    def __init__(self, message: str, code: str = "provider_refused") -> None:
        super().__init__(message)
        self.code = code


class ProviderError(Exception):
    """The provider failed in a way that leaves the outcome open. Its message holds only a
    status or reply code, never a URL, an address or the provider's own text."""


class ReconcileUnavailable(Exception):
    """The adapter cannot tell whether an interrupted send happened."""


class Transport(Protocol):
    async def send(self, message: Outgoing) -> Sent: ...

    async def find_sent(self, effect_key: str) -> Sent | None: ...

    async def poll_replies(self, thread: Thread) -> list[Inbound]: ...

    async def poll_inbound(
        self, senders: tuple[str, ...], since: datetime, known: frozenset[str]
    ) -> list[Inbound]: ...
