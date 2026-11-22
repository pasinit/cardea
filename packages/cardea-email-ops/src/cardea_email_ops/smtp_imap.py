"""SMTP submission plus IMAP adapter, on the standard library (ADR-039).

Sending (`smtplib`, RFC 6409 submission): the message carries a Message-ID minted from the
call's effect key, `<cardea.<effect key>@<message_id_domain>>`. That Message-ID is the
thread id: a reply names it in `In-Reply-To` or `References` (RFC 5322 section 3.6.4).

Reading (`imaplib`, RFC 3501) is narrow by construction:
- the folder is opened with EXAMINE (read-only), so no flag can change;
- the only searches are `UID SEARCH HEADER In-Reply-To <id>` and `HEADER References <id>`
  for a thread Cardea created, never a scan of the mailbox;
- only the messages those searches return are fetched, with `BODY.PEEK[]`;
- nothing is stored, copied, moved, flagged or expunged, and unmatched mail is never read;
- inbound mail (amendment 2026-11-21) is found only by `UID SEARCH HEADER From <address>`
  for each configured sender address, so mail from anyone else is never fetched.
A mailbox password usually grants the whole mailbox; this narrowing is the only guard.
"""

import asyncio
import contextlib
import email.utils
import imaplib
import re
import smtplib
import ssl
from dataclasses import replace
from datetime import datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default

from cardea_email_ops.authentication import from_header
from cardea_email_ops.config import (
    ADDRESS,
    CredentialUnavailable,
    Endpoint,
    ImapEndpoint,
    SmtpImapTransport,
    secret,
)
from cardea_email_ops.transport import (
    Inbound,
    Outgoing,
    ProviderError,
    ReconcileUnavailable,
    Refused,
    Sent,
    Thread,
)

MAX_MATCHES = 50
MESSAGE_ID = re.compile(r"^<[A-Za-z0-9._%+=-]{1,200}@[A-Za-z0-9.-]{1,253}>$")


def message_id(effect_key: str, domain: str) -> str:
    return f"<cardea.{effect_key}@{domain}>"


def _password(endpoint: Endpoint) -> str:
    try:
        return secret(endpoint.password_file)
    except CredentialUnavailable as missing:
        raise Refused(str(missing), "credential_unavailable") from None


class SmtpImap:
    def __init__(self, config: SmtpImapTransport, tls: ssl.SSLContext | None = None) -> None:
        self.config = config
        self.tls = tls or ssl.create_default_context()

    # Sending

    def _smtp(self) -> smtplib.SMTP:
        endpoint = self.config.smtp
        timeout = self.config.timeout_seconds
        if endpoint.security == "tls":
            return smtplib.SMTP_SSL(endpoint.host, endpoint.port, timeout=timeout, context=self.tls)
        client = smtplib.SMTP(endpoint.host, endpoint.port, timeout=timeout)
        if endpoint.security == "starttls":
            client.starttls(context=self.tls)
        return client

    def _send(self, message: Outgoing) -> Sent:
        identity = message_id(message.effect_key, self.config.message_id_domain)
        mail = EmailMessage()
        mail["From"] = self.config.from_address
        mail["To"] = message.to
        mail["Subject"] = message.subject
        mail["Date"] = email.utils.formatdate(localtime=False, usegmt=True)
        mail["Message-ID"] = identity
        mail.set_content(message.body)
        password = _password(self.config.smtp)
        try:
            client = self._smtp()
        except (OSError, smtplib.SMTPException):
            # No session, so nothing was submitted.
            raise Refused("The SMTP server is unreachable", "provider_unreachable") from None
        with client:
            try:
                client.login(self.config.smtp.username, password)
            except smtplib.SMTPException:
                raise Refused("The SMTP server refused the login", "provider_refused") from None
            try:
                refused = client.send_message(mail)
            except (
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError,
            ) as error:
                # The server answered with a rejection: it did not accept the message.
                code = getattr(error, "smtp_code", None)
                raise Refused(f"The SMTP server refused the message ({code})") from None
            except (OSError, smtplib.SMTPException) as error:
                # Submission may or may not have completed: the reconciler decides. The
                # server's text can name addresses, so only the class is kept.
                raise ProviderError(f"SMTP submission failed ({type(error).__name__})") from None
            if refused:
                raise Refused("The SMTP server refused the recipient")
        return Sent(thread_id=identity, message_id=identity)

    async def send(self, message: Outgoing) -> Sent:
        return await asyncio.to_thread(self._send, message)

    # Reading

    def _imap(self) -> imaplib.IMAP4:
        endpoint: ImapEndpoint = self.config.imap
        timeout = self.config.timeout_seconds
        client: imaplib.IMAP4
        if endpoint.security == "tls":
            client = imaplib.IMAP4_SSL(
                endpoint.host, endpoint.port, ssl_context=self.tls, timeout=timeout
            )
        else:
            client = imaplib.IMAP4(endpoint.host, endpoint.port, timeout=timeout)
            if endpoint.security == "starttls":
                client.starttls(ssl_context=self.tls)
        client.login(endpoint.username, _password(endpoint))
        return client

    @staticmethod
    def _search(client: imaplib.IMAP4, header: str, identity: str) -> list[bytes]:
        if not MESSAGE_ID.fullmatch(identity):
            raise ValueError("not a Cardea Message-ID")
        status, data = client.uid("SEARCH", "HEADER", header, f'"{identity}"')
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        return [uid for chunk in data if chunk for uid in chunk.split()]

    def _matching(self, client: imaplib.IMAP4, folder: str, identity: str) -> list[bytes]:
        status, _ = client.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP folder unavailable")
        uids: list[bytes] = []
        for header in ("In-Reply-To", "References"):
            for uid in self._search(client, header, identity):
                if uid not in uids:
                    uids.append(uid)
        return uids[:MAX_MATCHES]

    def _poll(self, thread: Thread) -> list[Inbound]:
        found: list[Inbound] = []
        client = self._imap()
        try:
            for uid in self._matching(client, self.config.imap.folder, thread.message_id):
                status, data = client.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                raw = next(
                    (item[1] for item in data if isinstance(item, tuple) and len(item) > 1),
                    None,
                )
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                found.append(self._inbound(uid, raw))
        finally:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                client.logout()
        return found

    def _inbound(self, uid: bytes, raw: bytes) -> Inbound:
        parsed = BytesParser(policy=default).parsebytes(raw)
        sender = email.utils.parseaddr(str(parsed.get("From", "")))[1]
        body = parsed.get_body(preferencelist=("plain",))
        text = ""
        if body is not None:
            try:
                text = str(body.get_content())
            except (LookupError, ValueError):
                text = ""
        reply_id = str(parsed.get("Message-ID", "")).strip() or f"imap-uid:{uid.decode()}"
        return Inbound(
            reply_id=reply_id,
            sender=sender,
            subject=str(parsed.get("Subject", "")),
            text=text,
            received_at=str(parsed.get("Date", "")),
            # Only the configured receiving server's own Authentication-Results counts.
            authenticated=from_header(parsed, self.config.imap.authserv_id),
        )

    async def poll_replies(self, thread: Thread) -> list[Inbound]:
        return await asyncio.to_thread(self._poll, thread)

    def _from(self, client: imaplib.IMAP4, address: str) -> list[bytes]:
        if not ADDRESS.fullmatch(address):
            raise ValueError("not a configured address")
        status, data = client.uid("SEARCH", "HEADER", "From", f'"{address}"')
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        return [uid for chunk in data if chunk for uid in chunk.split()]

    def _inbound_poll(
        self, senders: tuple[str, ...], since: datetime, known: frozenset[str]
    ) -> list[Inbound]:
        found: list[Inbound] = []
        own = f"@{self.config.message_id_domain}>".lower()
        client = self._imap()
        try:
            status, _ = client.select(f'"{self.config.imap.folder}"', readonly=True)
            if status != "OK":
                raise RuntimeError("IMAP folder unavailable")
            uids: list[bytes] = []
            for address in senders:
                for uid in self._from(client, address):
                    if uid not in uids:
                        uids.append(uid)
            # The newest matches only: UIDs grow with arrival.
            for uid in sorted(uids, key=int)[-MAX_MATCHES:]:
                status, data = client.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                raw = next(
                    (item[1] for item in data if isinstance(item, tuple) and len(item) > 1),
                    None,
                )
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                parsed = BytesParser(policy=default).parsebytes(raw, headersonly=True)
                threads = f"{parsed.get('In-Reply-To', '')} {parsed.get('References', '')}"
                if "<cardea." in threads.lower() and own in threads.lower():
                    continue  # a reply to a thread Cardea started: `poll_replies` reads it
                if str(parsed.get("Message-ID", "")).strip() in known:
                    continue
                if not _since(str(parsed.get("Date", "")), since):
                    continue
                message = self._inbound(uid, raw)
                found.append(replace(message, size=len(message.text.encode())))
        finally:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                client.logout()
        return found

    async def poll_inbound(
        self, senders: tuple[str, ...], since: datetime, known: frozenset[str]
    ) -> list[Inbound]:
        """New mail from `senders` (exact `From` addresses, rechecked after parsing) in the
        inbox since `since`, that replies to no Cardea thread (amendment 2026-11-21). Only
        `UID SEARCH HEADER From` for each configured address, then `BODY.PEEK[]` of the
        newest matches: mail from anyone else is never fetched."""
        return await asyncio.to_thread(self._inbound_poll, senders, since, known)

    def _find(self, effect_key: str) -> Sent | None:
        folder = self.config.imap.sent_folder
        if folder is None:
            raise ReconcileUnavailable("no sent folder is configured")
        identity = message_id(effect_key, self.config.message_id_domain)
        client = self._imap()
        try:
            status, _ = client.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise ReconcileUnavailable("the sent folder is unavailable")
            found = self._search(client, "Message-ID", identity)
        finally:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                client.logout()
        return Sent(thread_id=identity, message_id=identity) if found else None

    async def find_sent(self, effect_key: str) -> Sent | None:
        return await asyncio.to_thread(self._find, effect_key)


def _since(date: str, since: datetime) -> bool:
    """Whether an RFC 5322 Date is at or after `since`; an unreadable one counts."""
    try:
        sent = email.utils.parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return True
    try:
        return sent >= since
    except TypeError:
        return True
