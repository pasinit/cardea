"""AgentMail adapter: a thin httpx client for four endpoints (ADR-039).

Endpoints (https://docs.agentmail.to/api-reference, read 2026-11-17), base
`<api_url>/v0`, bearer authentication:
- `POST /inboxes/{inbox_id}/messages/send` with an `Idempotency-Key` header: a retry
  with the same key within 24 hours returns the first message and sends nothing;
- `GET /inboxes/{inbox_id}/messages?labels=...` to find a send by the label it carries;
- `GET /inboxes/{inbox_id}/threads/{thread_id}` for the messages on one thread;
- `GET /inboxes/{inbox_id}/messages?labels=received` and
  `GET /inboxes/{inbox_id}/messages/{message_id}` for inbound mail on no Cardea thread
  (amendment 2026-11-21). Only messages from an allowed address are read in full.

Inbound mail is polled, never pushed: the host needs no public endpoint or webhook. The
official Python SDK is not used: see ADR-039 for why.
"""

import logging
from datetime import datetime
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

import httpx

from cardea_email_ops.authentication import from_agentmail
from cardea_email_ops.config import AgentMailTransport, CredentialUnavailable, secret
from cardea_email_ops.transport import Inbound, Outgoing, ProviderError, Refused, Sent, Thread

logger = logging.getLogger("cardea.email_ops.agentmail")

SEND = "POST /inboxes/{inbox_id}/messages/send"
LIST = "GET /inboxes/{inbox_id}/messages"
MESSAGE = "GET /inboxes/{inbox_id}/messages/{message_id}"
THREAD = "GET /inboxes/{inbox_id}/threads/{thread_id}"
MAX_PAGES = 5
# Responses that mean the request was refused before anything was sent. 409 is not one:
# it means this idempotency key already sent a different request, so an earlier attempt
# may have sent; the call stays uncertain and the reconciler decides.
DEFINITE = {400, 401, 403, 404, 413, 422, 429}


def _failure(response: httpx.Response, route: str) -> str:
    """Log a failed call and return its message. Only the status and the route template
    are reported: the URL names the inbox and the provider's message may echo the request."""
    message = f"AgentMail answered HTTP {response.status_code} for {route}"
    logger.warning(message)
    return message


def _checked(response: httpx.Response, route: str) -> dict[str, Any]:
    """The JSON body of a successful response; a failed one is logged and raised."""
    if response.status_code >= 400:
        raise ProviderError(_failure(response, route))
    try:
        data = response.json()
    except ValueError:
        raise ProviderError("AgentMail answered with a body that is not JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("AgentMail answered with an unexpected body")
    return data


def label(effect_key: str) -> str:
    """The label a send carries so the reconciler can find it again."""
    return f"cardea-{effect_key[:40]}"


class AgentMail:
    def __init__(
        self, config: AgentMailTransport, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport
        self.inbox = quote(config.inbox_id, safe="@.")

    def _client(self) -> httpx.AsyncClient:
        try:
            key = secret(self.config.api_key_file)
        except CredentialUnavailable as missing:
            raise Refused(str(missing), "credential_unavailable") from None
        return httpx.AsyncClient(
            base_url=self.config.api_url.rstrip("/") + "/v0",
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.config.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
        )

    async def send(self, message: Outgoing) -> Sent:
        async with self._client() as client:
            try:
                response = await client.post(
                    f"/inboxes/{self.inbox}/messages/send",
                    headers={"Idempotency-Key": message.effect_key},
                    json={
                        "to": [message.to],
                        "subject": message.subject,
                        "text": message.body,
                        "labels": [label(message.effect_key)],
                    },
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # No connection, so no request: nothing was sent.
                raise Refused("AgentMail is unreachable", "provider_unreachable") from None
        if response.status_code in DEFINITE:
            raise Refused(_failure(response, SEND))
        data = _checked(response, SEND)
        return Sent(thread_id=str(data["thread_id"]), message_id=str(data["message_id"]))

    async def find_sent(self, effect_key: str) -> Sent | None:
        async with self._client() as client:
            response = await client.get(
                f"/inboxes/{self.inbox}/messages",
                params={"labels": label(effect_key), "limit": 1},
            )
        found = _checked(response, LIST).get("messages") or []
        if not found:
            return None
        return Sent(thread_id=str(found[0]["thread_id"]), message_id=str(found[0]["message_id"]))

    async def poll_inbound(
        self, senders: tuple[str, ...], since: datetime, known: frozenset[str]
    ) -> list[Inbound]:
        """Received messages since `since` on no known thread. A message from any other
        address is reported by its sender only (no body is read), so it can be rejected."""
        wanted = {s.lower() for s in senders}
        items: list[dict[str, Any]] = []
        found: list[Inbound] = []
        async with self._client() as client:
            token: str | None = None
            for _ in range(MAX_PAGES):
                # No `after`: the window is applied below (`_after`), so the query stays
                # to the plain parameters `find_sent` already sends successfully.
                params: dict[str, Any] = {"labels": "received", "limit": 100}
                if token:
                    params["page_token"] = token
                response = await client.get(f"/inboxes/{self.inbox}/messages", params=params)
                page = _checked(response, LIST)
                items.extend(page.get("messages") or [])
                token = page.get("next_page_token")
                if not token:
                    break
            for item in items:
                labels = set(item.get("labels") or [])
                if "sent" in labels or str(item.get("thread_id")) in known:
                    continue
                if not _after(item.get("timestamp"), since):
                    continue
                sender = str(item.get("from") or "")
                if parseaddr(sender)[1].lower() in wanted:
                    response = await client.get(
                        f"/inboxes/{self.inbox}/messages/{quote(str(item['message_id']), safe='')}"
                    )
                    item = {**item, **_checked(response, MESSAGE)}
                    text = str(item.get("extracted_text") or item.get("text") or "")
                    authenticated = from_agentmail(item)
                else:
                    text, authenticated = "", None
                size = item.get("size")
                found.append(
                    Inbound(
                        reply_id=str(item["message_id"]),
                        sender=sender,
                        subject=str(item.get("subject") or ""),
                        text=text,
                        received_at=str(item.get("timestamp") or ""),
                        authenticated=authenticated,
                        spam="spam" in labels,
                        thread_id=str(item.get("thread_id") or ""),
                        size=size if isinstance(size, int) else None,
                    )
                )
        return found

    async def poll_replies(self, thread: Thread) -> list[Inbound]:
        messages: list[dict[str, Any]] = []
        token: str | None = None
        async with self._client() as client:
            for _ in range(MAX_PAGES):
                params: dict[str, Any] = {"limit": 100}
                if token:
                    params["page_token"] = token
                response = await client.get(
                    f"/inboxes/{self.inbox}/threads/{quote(thread.thread_id, safe='')}",
                    params=params,
                )
                page = _checked(response, THREAD)
                messages.extend(page.get("messages") or [])
                token = page.get("next_page_token")
                if not token:
                    break
        found = []
        for item in messages:
            labels = set(item.get("labels") or [])
            text = item.get("extracted_text") or item.get("text") or ""
            found.append(
                Inbound(
                    reply_id=str(item["message_id"]),
                    sender=str(item.get("from") or ""),
                    subject=str(item.get("subject") or ""),
                    text=str(text),
                    received_at=str(item.get("timestamp") or ""),
                    own="sent" in labels or item.get("message_id") == thread.message_id,
                    # True only for an aligned pass in `authentication_results`; the
                    # absence of the `unauthenticated` label alone is not enough.
                    authenticated=from_agentmail(item),
                    spam="spam" in labels,
                )
            )
        return found


def _after(stamp: object, since: datetime) -> bool:
    """Whether a provider timestamp is at or after `since`; an unreadable one counts."""
    try:
        return datetime.fromisoformat(str(stamp)) >= since
    except (ValueError, TypeError):
        return True
