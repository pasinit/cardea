"""The content-gate hook (ADR-037 decision 4; implemented in slice 6).

Every operation that sends content off the host calls the gate before any network call:
`vcs.push_branch` on the change, and `vcs.open_change`, `vcs.comment` and `vcs.review` on
each text field. A refusal raises `ContentRefused`, which the operation turns into a
definite `content_refused` failure, so nothing is sent.

Until slice 6 lands the installed gate is `NoContentGate`, which allows everything.
Slice 6 installs its implementation with `install_content_gate`.
"""

from typing import Literal, Protocol

TextField = Literal["title", "body", "comment", "review", "commit_message"]


class ContentRefused(Exception):
    """Raised by a gate. `rule` and `where` name the match, never the matched text."""

    def __init__(self, rule: str, where: str) -> None:
        super().__init__(f"{rule} in {where}")
        self.rule, self.where = rule, where


class ContentGate(Protocol):
    def check_text(self, field: TextField, text: str) -> None: ...

    def check_change(self, bundle: bytes) -> None: ...


class NoContentGate:
    """The placeholder gate: allows everything. Replaced by ADR-037 slice 6."""

    def check_text(self, field: TextField, text: str) -> None:
        return None

    def check_change(self, bundle: bytes) -> None:
        return None


_GATE: ContentGate = NoContentGate()


def content_gate() -> ContentGate:
    """The gate every sending operation calls before it sends."""
    return _GATE


def install_content_gate(gate: ContentGate) -> None:
    global _GATE
    _GATE = gate
