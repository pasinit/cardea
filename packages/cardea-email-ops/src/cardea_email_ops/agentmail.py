"""AgentMail adapter: a thin httpx client for four endpoints (ADR-039).

Endpoints (https://docs.agentmail.to/api-reference, read 2026-11-17), base
`<api_url>/v0`, bearer authentication:
- `POST /inboxes/{inbox_id}/messages/send` with an `Idempotency-Key` header: a retry
  with the same key within 24 hours returns the first message and sends nothing;
- `GET /inboxes/{inbox_id}/messages?labels=...` to find a send by the label it carries;
- `GET /inboxes/{inbox_id}/threads/{thread_id}` for the messages on one thread.

Inbound mail is polled, never pushed: the host needs no public endpoint or webhook. The
official Python SDK is not used: see ADR-039 for why.
"""

from typing import Any
from urllib.parse import quote

import httpx

from cardea_email_ops.authentication import from_agentmail
from cardea_email_ops.config import AgentMailTransport, CredentialUnavailable, secret
from cardea_email_ops.transport import Inbound, Outgoing, ProviderError, Refused, Sent, Thread

MAX_PAGES = 5
# Responses that mean the request was refused before anything was sent. 409 is not one:
# it means this idempotency key already sent a different request, so an earlier attempt
# may have sent; the call stays uncertain and the reconciler decides.
DEFINITE = {400, 401, 403, 404, 413, 422, 429}


def _checked(response: httpx.Response) -> dict[str, Any]:
    """The JSON body of a successful response; otherwise only the status is reported (the
    URL names the inbox, and the provider's message may echo the request)."""
    if response.status_code >= 400:
        raise ProviderError(f"AgentMail answered HTTP {response.status_code}")
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
            # The provider's own code only; its message may echo the request.
            raise Refused(f"AgentMail refused the send (HTTP {response.status_code})")
        data = _checked(response)
        return Sent(thread_id=str(data["thread_id"]), message_id=str(data["message_id"]))

    async def find_sent(self, effect_key: str) -> Sent | None:
        async with self._client() as client:
            response = await client.get(
                f"/inboxes/{self.inbox}/messages",
                params={"labels": label(effect_key), "limit": 1},
            )
        found = _checked(response).get("messages") or []
        if not found:
            return None
        return Sent(thread_id=str(found[0]["thread_id"]), message_id=str(found[0]["message_id"]))

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
                page = _checked(response)
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
