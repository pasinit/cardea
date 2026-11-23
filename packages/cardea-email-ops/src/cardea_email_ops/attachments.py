"""Checks on `email.send` attachments (ADR-039 amendment 2026-11-23).

An attachment is `{filename, content_type, data}` with `data` the base64 of the bytes, or,
for `text/html` only, `{filename, content_type, text}` with `text` the page itself (a model
cannot be relied on to base64-encode a page by hand; the checker encodes it). Exactly one of
`data` and `text` is given. Either way the whole attachment is in the call's arguments: the receipt, the idempotency scope and
the content gate all see exactly what is sent. Only two types are accepted:
- `image/png`: the bytes must start with the PNG signature and an IHDR chunk;
- `text/html`: the bytes must be UTF-8 text without control characters, and self-contained:
  no script, frame, form or link element and no `src`, `href`, `url()` or `@import`
  that points anywhere but a `data:` URI or an in-page `#` anchor. The reference check is a
  conservative screen, not an HTML sanitizer: anything it cannot read as safe is refused.

The limits come from the host configuration (`AttachmentLimits`). Every refusal happens
before anything is sent.
"""

import base64
import binascii
import re
from collections.abc import Sequence

from pydantic import JsonValue

from cardea.service.operations import OperationRejected
from cardea_email_ops.config import AttachmentLimits
from cardea_email_ops.transport import Attachment

PNG = b"\x89PNG\r\n\x1a\n"
TYPES = {"image/png": ".png", "text/html": ".html"}
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ACTIVE = re.compile(r"<\s*(script|iframe|frame|object|embed|link|base|form)\b")
REFERENCE = re.compile(
    r"""\b(?:src|href|action|formaction|poster|srcset|data|xlink:href)
        \s*=\s*["']?\s*(?!data:|\#|["'])
    |\burl\(\s*["']?\s*(?!data:|\#)
    |@import\b
    |http-equiv\s*=\s*["']?\s*refresh""",
    re.VERBOSE,
)


def check(raw: JsonValue, limits: AttachmentLimits) -> tuple[Attachment, ...]:
    """The attachments of one call, decoded and checked; `OperationRejected` on any fault."""
    items: Sequence[JsonValue] = raw if isinstance(raw, list) else []
    if len(items) > limits.max_count:
        raise OperationRejected(
            f"At most {limits.max_count} attachments are allowed", "attachments_too_many"
        )
    checked: list[Attachment] = []
    for item in items:
        assert isinstance(item, dict)
        filename, content_type = str(item["filename"]), str(item["content_type"])
        if (
            not FILENAME.fullmatch(filename)
            or content_type not in TYPES
            or not filename.lower().endswith(TYPES[content_type])
            or filename in {a.filename for a in checked}
        ):
            raise OperationRejected(
                "An attachment needs a unique plain filename that matches its content type",
                "attachment_invalid",
            )
        content = _bytes(item, content_type)
        if not content or len(content) > limits.max_file_bytes:
            raise OperationRejected(
                f"An attachment may be 1 to {limits.max_file_bytes} bytes", "attachment_too_large"
            )
        _content(content_type, content)
        checked.append(Attachment(filename, content_type, content))
    if sum(len(a.content) for a in checked) > limits.max_total_bytes:
        raise OperationRejected(
            f"Attachments may total {limits.max_total_bytes} bytes", "attachments_too_large"
        )
    return tuple(checked)


def _bytes(item: dict[str, JsonValue], content_type: str) -> bytes:
    """The attachment's bytes: its `text` as UTF-8 (HTML only) or its `data` from base64."""
    if ("text" in item) == ("data" in item) or ("text" in item and content_type != "text/html"):
        raise OperationRejected(
            "An attachment has base64 `data`, or, for HTML, its page as `text`, not both",
            "attachment_invalid",
        )
    if "text" in item:
        return str(item["text"]).encode()
    try:
        return base64.b64decode(str(item["data"]), validate=True)
    except (binascii.Error, ValueError):
        raise OperationRejected(
            "An attachment's data is not valid base64; for an HTML page send it as `text` instead",
            "attachment_invalid",
        ) from None


def _content(content_type: str, content: bytes) -> None:
    if content_type == "image/png":
        if not content.startswith(PNG) or content[12:16] != b"IHDR":
            raise OperationRejected("The attachment is not a PNG image", "attachment_type_mismatch")
        return
    try:
        page = content.decode("utf-8")
    except UnicodeDecodeError:
        page = ""
    if not page or CONTROL.search(page):
        raise OperationRejected("The attachment is not HTML text", "attachment_type_mismatch")
    lowered = page.lower()
    if ACTIVE.search(lowered) or REFERENCE.search(lowered):
        raise OperationRejected(
            "The HTML attachment must be self-contained", "attachment_external_reference"
        )


def texts(attachments: Sequence[Attachment]) -> dict[str, str]:
    """The readable attachments, for the content gate. A PNG has no text to read."""
    return {
        f"attachment {a.filename}": a.content.decode()
        for a in attachments
        if a.content_type == "text/html"
    }
