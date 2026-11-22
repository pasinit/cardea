"""`email.send`, its reconciler and its reply poller (ADR-039).

Rules checked before anything is sent:
- the caller is a group member whose role (`<group>.<pool>`) the host configuration lists,
  and the alias it names is one of that role's recipients (`recipient_not_allowed`);
- the role's calls in the configured window, this one included, stay within its rate
  limit (`rate_limited`); a call without the kernel's counter is refused;
- the subject and body pass the kernel's content gate, which fails closed.

The model names an alias only. The address, the transport and its credential come from
private host configuration and never appear in arguments, outputs, receipts or logs.

Replies: for each successful send in the reply window, the kernel calls `poll_replies`
with the send's receipt. It accepts only messages on that thread from the address of the
alias the send went to, only when the sender is authenticated (`authentication.py`: an
aligned DMARC, SPF or DKIM pass; unknown counts as not authenticated unless the host sets
`accept_unauthenticated`), and never spam. Accepted replies go to the sending agent's
mailbox, where they are untrusted data. Everything else is dropped and counted by
reason, and its content is not kept.

Inbound mail (amendment 2026-11-21, off unless the host configures `inbound`): once per
poll interval the kernel calls `poll_inbound` with the recent send threads. It reads new
mail on none of them from the addresses of the configured aliases and applies the same
checks: an exact sender address, an authenticated sender (or `accept_unauthenticated`),
no spam, and at most `max_bytes` of text. Accepted mail goes to the configured pool's
mailbox as untrusted data from that alias; the kernel enforces the rate limit and records
every rejection with the sender's domain and the reason only.
"""

from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

import httpx
from pydantic import JsonValue

from cardea.service.operations import OperationContext, OperationRejected, Reconciliation
from cardea.service.replies import Dropped, Inbox, Polled, Reply
from cardea_email_ops.agentmail import AgentMail
from cardea_email_ops.authentication import domain_of
from cardea_email_ops.config import EmailConfig
from cardea_email_ops.smtp_imap import SmtpImap
from cardea_email_ops.transport import (
    Inbound,
    Outgoing,
    Refused,
    Sent,
    Thread,
    Transport,
)

TRUNCATED = "\n[reply truncated]"


def build_transport(config: EmailConfig, http: httpx.AsyncBaseTransport | None = None) -> Transport:
    if config.transport.kind == "agentmail":
        return AgentMail(config.transport, http)
    return SmtpImap(config.transport)


def _shorten(text: str, limit: int) -> str:
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[: limit - len(TRUNCATED)].decode(errors="ignore") + TRUNCATED


class Email:
    def __init__(self, config: EmailConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.transport = transport or build_transport(config)
        self.accept_unauthenticated = config.transport.accept_unauthenticated

    def role(self, context: OperationContext) -> str | None:
        if context.group is None or context.pool is None:
            return None
        return f"{context.group}.{context.pool}"

    def _check(self, alias: str, context: OperationContext) -> str:
        role = self.role(context)
        entry = self.config.roles.get(role) if role is not None else None
        address = self.config.address(alias)
        if entry is None or alias not in entry.recipients or address is None:
            raise OperationRejected(
                "This role may not email that recipient", "recipient_not_allowed"
            )
        if context.count_calls is None:
            raise OperationRejected("The rate limit is not available", "rate_limit_unavailable")
        limit = entry.rate_limit
        if context.count_calls(timedelta(days=limit.window_days)) > limit.count:
            raise OperationRejected(
                f"This role may send {limit.count} emails per {limit.window_days} days",
                "rate_limited",
            )
        return address

    @staticmethod
    def _output(alias: str, sent: Sent, at: datetime) -> dict[str, JsonValue]:
        return {
            "to": alias,
            "thread_id": sent.thread_id,
            "message_id": sent.message_id,
            "sent_at": at.isoformat(),
        }

    async def send(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        alias = str(arguments["to"])
        subject = str(arguments["subject"])
        body = str(arguments["body"])
        address = self._check(alias, context)
        if context.check_content is None:
            raise OperationRejected(
                "The content gate is not available to this call", "content_gate_unavailable"
            )
        context.check_content(operation="email.send", texts={"subject": subject, "body": body})
        try:
            sent = await self.transport.send(
                Outgoing(to=address, subject=subject, body=body, effect_key=context.effect_key)
            )
        except Refused as refused:
            raise OperationRejected(str(refused), refused.code) from None
        return self._output(alias, sent, datetime.now(UTC))

    async def find_sent(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        """Read the provider for this call's send; never send."""
        # A provider that cannot be read raises (`ReconcileUnavailable` or any error): the
        # call stays uncertain and the kernel's loop tries again.
        sent = await self.transport.find_sent(context.effect_key)
        if sent is None:
            return Reconciliation(applied=False)
        return Reconciliation(
            applied=True, output=self._output(str(arguments["to"]), sent, datetime.now(UTC))
        )

    async def poll_replies(self, thread: dict[str, JsonValue]) -> Polled:
        arguments = thread.get("arguments")
        output = thread.get("output")
        assert isinstance(arguments, dict) and isinstance(output, dict)
        alias = str(arguments.get("to"))
        expected = self.config.address(alias)
        found = await self.transport.poll_replies(
            Thread(
                thread_id=str(output["thread_id"]),
                message_id=str(output["message_id"]),
                sent_at=datetime.fromisoformat(str(output["sent_at"])),
            )
        )
        replies: list[Reply] = []
        dropped: list[Dropped] = []
        for message in found:
            if message.own or message.reply_id == output["message_id"]:
                continue
            sender = parseaddr(message.sender)[1].lower()
            if expected is None or sender != expected:
                dropped.append(Dropped(message.reply_id, "sender_not_allowed"))
            elif message.authenticated is not True and not self.accept_unauthenticated:
                # Fail closed: without an authenticated sender, From proves nothing.
                reason = (
                    "unauthenticated"
                    if message.authenticated is False
                    else ("authentication_unknown")
                )
                dropped.append(Dropped(message.reply_id, reason))
            elif message.spam:
                dropped.append(Dropped(message.reply_id, "spam"))
            else:
                replies.append(
                    Reply(
                        message.reply_id,
                        [
                            {"text": _shorten(message.text, self.config.max_reply_bytes)},
                            {
                                "data": {
                                    "email_reply": {
                                        "from": alias,
                                        "subject": message.subject[:200],
                                        "received_at": message.received_at[:64],
                                        "thread_id": str(output["thread_id"]),
                                    }
                                }
                            },
                        ],
                    )
                )
        return Polled(tuple(replies), tuple(dropped))

    async def poll_inbound(self, job: dict[str, JsonValue]) -> Inbox | None:
        """New mail from the configured aliases on no thread a send started; None when the
        host has not enabled inbound mail."""
        inbound = self.config.inbound
        if inbound is None:
            return None
        senders = {
            address: alias
            for alias in inbound.senders
            if (address := self.config.address(alias)) is not None
        }
        known: set[str] = set()
        threads = job.get("threads")
        for thread in threads if isinstance(threads, list) else []:
            output = thread.get("output") if isinstance(thread, dict) else None
            if isinstance(output, dict):
                known.update(str(output.get(k)) for k in ("thread_id", "message_id"))
        since = datetime.now(UTC) - timedelta(days=inbound.window_days)
        found = await self.transport.poll_inbound(tuple(senders), since, frozenset(known))
        accepted: list[Reply] = []
        dropped: list[Dropped] = []
        for message in found:
            if message.own or message.thread_id in known or message.reply_id in known:
                continue
            sender = parseaddr(message.sender)[1].lower()
            domain = domain_of(sender) or None
            alias = senders.get(sender)
            size = message.size if message.size is not None else len(message.text.encode())
            if alias is None:
                dropped.append(Dropped(message.reply_id, "sender_not_allowed", domain))
            elif message.authenticated is not True and not self.accept_unauthenticated:
                # Fail closed: without an authenticated sender, From proves nothing.
                reason = (
                    "unauthenticated"
                    if message.authenticated is False
                    else "authentication_unknown"
                )
                dropped.append(Dropped(message.reply_id, reason, domain))
            elif message.spam:
                dropped.append(Dropped(message.reply_id, "spam", domain))
            elif size > inbound.max_bytes:
                dropped.append(Dropped(message.reply_id, "too_large", domain))
            else:
                accepted.append(Reply(message.reply_id, _inbound_parts(alias, message)))
        limit = inbound.rate_limit
        return Inbox(
            namespace=inbound.deliver_to.namespace,
            pool=inbound.deliver_to.pool,
            accepted=tuple(accepted),
            dropped=tuple(dropped),
            rate_limit=(limit.count, limit.window_days),
        )


def _inbound_parts(alias: str, message: Inbound) -> list[dict[str, JsonValue]]:
    """An inbound email as mailbox parts: its text, and who sent it, as data."""
    return [
        {"text": message.text},
        {
            "data": {
                "email_inbound": {
                    "from": alias,
                    "subject": message.subject[:200],
                    "received_at": message.received_at[:64],
                    # Reply with email.send to the alias; the reply starts a new thread.
                    "thread_id": (message.thread_id or message.reply_id)[:320],
                }
            }
        },
    ]
