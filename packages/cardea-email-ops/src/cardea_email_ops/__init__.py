"""External email operations for Cardea agent teams (ADR-039).

`email.send` is an ADR-023 installed operation: the MCP tool descriptor in `descriptors/`
plus in-process handlers under the `cardea.operations` entry-point group: the handler, its
ADR-026 reconciler `email.find_sent`, its reply poller `email.poll_replies`, which the
kernel calls for each recent send (`replies` in the descriptor), and its inbound poller
`email.poll_inbound` for new mail on no thread, which the host enables per inbox.

The transport is pluggable behind one interface (`transport.Transport`): AgentMail's HTTP
API or standard SMTP submission plus IMAP, chosen by the host configuration
(`CARDEA_EMAIL_CONFIG`). Tests build their own `Email` and pass `handlers()` to the
operation registry.
"""

from importlib.resources import files

from pydantic import JsonValue

from cardea.service.database import Fault
from cardea.service.operations import (
    Handler,
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Poller,
    Reconciler,
    Reconciliation,
)
from cardea.service.replies import Inbox, Polled
from cardea_email_ops.config import load
from cardea_email_ops.ops import Email

__all__ = ["Email", "descriptors", "handlers"]


def descriptors() -> tuple[OperationDescriptor, ...]:
    """The package's descriptors, as installed under the host's `operations_dir`."""
    folder = files(__package__) / "descriptors"
    return tuple(
        OperationDescriptor.model_validate_json(entry.read_text())
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    )


def handlers(email: Email) -> dict[str, Handler | Reconciler | Poller]:
    """The entry points bound to one `Email`, for a test or rehearsal registry."""
    return {
        "email.send": email.send,
        "email.find_sent": email.find_sent,
        "email.poll_replies": email.poll_replies,
        "email.poll_inbound": email.poll_inbound,
    }


_DEFAULT: Email | None = None


def _email() -> Email:
    global _DEFAULT
    if _DEFAULT is None:
        try:
            _DEFAULT = Email(load())
        except Fault as error:
            # An unreadable configuration sends nothing: a definite refusal.
            raise OperationRejected(error.message, error.code) from None
    return _DEFAULT


async def send(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _email().send(arguments, ctx)


async def find_sent(arguments: dict[str, JsonValue], ctx: OperationContext) -> Reconciliation:
    return await _email().find_sent(arguments, ctx)


async def poll_replies(thread: dict[str, JsonValue]) -> Polled:
    return await _email().poll_replies(thread)


async def poll_inbound(job: dict[str, JsonValue]) -> Inbox | None:
    return await _email().poll_inbound(job)
