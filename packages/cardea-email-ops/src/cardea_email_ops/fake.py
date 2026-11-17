"""In-process fakes of the email providers, for tests and rehearsals only (ADR-039).

- `FakeAgentMail`: the four AgentMail endpoints the adapter uses, as an
  `httpx.MockTransport`. It checks the bearer key and honours `Idempotency-Key`.
- `FakeSmtp`: an SMTP submission server on a loopback socket (EHLO, AUTH PLAIN, MAIL,
  RCPT, DATA). It records each accepted message and can refuse a recipient.
- `FakeImap`: an IMAP4rev1 server on a loopback socket with just LOGIN, SELECT/EXAMINE,
  UID SEARCH HEADER, UID FETCH and LOGOUT. It records every command, so tests can check
  that the adapter never stores, copies, expunges or fetches anything unmatched.

None of them talks to a real service.
"""

import base64
import json
import socket
import socketserver
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from typing import Any
from urllib.parse import unquote

import httpx

# AgentMail


@dataclass
class FakeAgentMail:
    inbox_id: str
    api_key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    keys: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    sends: int = 0
    # Status codes to answer the next sends with, instead of sending.
    fail_next: list[int] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reply(
        self,
        thread_id: str,
        sender: str,
        text: str,
        *,
        labels: tuple[str, ...] = ("received", "unread"),
        subject: str = "Re: question",
        authentication: dict[str, str] | None = None,
    ) -> str:
        """A received message. `authentication` is its `authentication_results`; by default
        a DMARC pass for the sender's own domain, as for an ordinary authenticated sender.
        Pass `{}` for a message with no verdicts."""
        message_id = f"<{uuid.uuid4().hex}@fake.agentmail.invalid>"
        domain = sender.rsplit("@", 1)[-1].strip(">").lower()
        results = (
            {"dmarc": "pass", "dmarc_domain": domain} if authentication is None else authentication
        )
        self.messages.append(
            {
                "authentication_results": results,
                "inbox_id": self.inbox_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "labels": list(labels),
                "timestamp": datetime.now(UTC).isoformat(),
                "from": sender,
                "to": [self.inbox_id],
                "subject": subject,
                "text": text + "\n\n> quoted earlier message",
                "extracted_text": text,
            }
        )
        return message_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"name": "Unauthorized", "message": "bad key"})
        path = unquote(request.url.path)
        prefix = f"/v0/inboxes/{self.inbox_id}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"name": "NotFound", "message": "no inbox"})
        rest = path[len(prefix) :]
        if request.method == "POST" and rest == "/messages/send":
            return self._send(request)
        if request.method == "GET" and rest == "/messages":
            wanted = request.url.params.get_list("labels")
            found = [m for m in self.messages if set(wanted) <= set(m["labels"])]
            limit = int(request.url.params.get("limit", "100"))
            return httpx.Response(
                200, json={"count": len(found[:limit]), "messages": found[:limit]}
            )
        if request.method == "GET" and rest.startswith("/threads/"):
            thread_id = rest[len("/threads/") :]
            found = [m for m in self.messages if m["thread_id"] == thread_id]
            if not found:
                return httpx.Response(404, json={"name": "NotFound", "message": "no thread"})
            return httpx.Response(
                200,
                json={"thread_id": thread_id, "count": len(found), "messages": found},
            )
        return httpx.Response(404, json={"name": "NotFound", "message": "no route"})

    def _send(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers.get("idempotency-key")
        if key is not None and key in self.keys:
            earlier, result = self.keys[key]
            if earlier != request.content.decode():
                return httpx.Response(409, json={"name": "Conflict", "message": "key reused"})
            return httpx.Response(200, json=result)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"name": "Error", "message": "x"})
        thread_id = f"thread-{uuid.uuid4().hex[:12]}"
        message_id = f"<{uuid.uuid4().hex}@fake.agentmail.invalid>"
        self.messages.append(
            {
                "inbox_id": self.inbox_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "labels": ["sent", *body.get("labels", [])],
                "timestamp": datetime.now(UTC).isoformat(),
                "from": self.inbox_id,
                "to": body["to"],
                "subject": body["subject"],
                "text": body["text"],
            }
        )
        self.sends += 1
        result = {"message_id": message_id, "thread_id": thread_id}
        if key is not None:
            self.keys[key] = (request.content.decode(), result)
        return httpx.Response(200, json=result)


# Socket servers


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Base:
    def __init__(self, handler: type[socketserver.StreamRequestHandler]) -> None:
        self.server = _Server(("127.0.0.1", 0), handler)
        self.server.owner = self  # type: ignore[attr-defined]
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_Base":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp(_Base):
    """Accepts submissions with AUTH PLAIN for one username and password."""

    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password
        self.accepted: list[bytes] = []
        self.refuse_recipients: set[str] = set()
        self.logins = 0
        super().__init__(_SmtpHandler)


class _SmtpHandler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        owner: FakeSmtp = self.server.owner  # type: ignore[attr-defined]
        self._say("220 fake.smtp.invalid ESMTP")
        authed = False
        recipients: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.wfile.write(b"250-fake.smtp.invalid\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif verb == "AUTH":
                parts = line.split()
                token = parts[2] if len(parts) > 2 else ""
                try:
                    _, user, password = base64.b64decode(token).decode().split("\0")
                except ValueError:
                    user = password = ""
                if user == owner.username and password == owner.password:
                    authed = True
                    owner.logins += 1
                    self._say("235 2.7.0 Authentication successful")
                else:
                    self._say("535 5.7.8 Authentication failed")
            elif verb == "MAIL":
                if not authed:
                    self._say("530 5.7.0 Authentication required")
                    continue
                recipients = []
                self._say("250 OK")
            elif verb == "RCPT":
                address = line.split(":", 1)[1].strip().strip("<>").lower()
                if address in owner.refuse_recipients:
                    self._say("550 5.1.1 No such user")
                else:
                    recipients.append(address)
                    self._say("250 OK")
            elif verb == "DATA":
                self._say("354 End data with <CR><LF>.<CR><LF>")
                data = bytearray()
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data.extend(chunk[1:] if chunk.startswith(b"..") else chunk)
                owner.accepted.append(bytes(data))
                self._say("250 OK queued")
            elif verb == "RSET" or verb == "NOOP":
                self._say("250 OK")
            elif verb == "QUIT":
                self._say("221 Bye")
                return
            else:
                self._say("502 5.5.2 Command not implemented")


@dataclass
class StoredMessage:
    uid: int
    raw: bytes
    folder: str = "INBOX"


class FakeImap(_Base):
    """A read-mostly IMAP server: LOGIN, SELECT/EXAMINE, UID SEARCH HEADER, UID FETCH."""

    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password
        self.messages: list[StoredMessage] = []
        self.commands: list[str] = []
        self.fetched: list[int] = []
        super().__init__(_ImapHandler)

    def deliver(self, raw: bytes, folder: str = "INBOX") -> int:
        uid = len(self.messages) + 1
        self.messages.append(StoredMessage(uid, raw, folder))
        return uid


class _ImapHandler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        owner: FakeImap = self.server.owner  # type: ignore[attr-defined]
        self._say("* OK [CAPABILITY IMAP4rev1] fake IMAP ready")
        folder: str | None = None
        authed = False
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            tag, _, rest = line.partition(" ")
            command, _, arguments = rest.partition(" ")
            command = command.upper()
            # Never record a LOGIN line: it holds the password.
            owner.commands.append(command if command == "LOGIN" else rest)
            if command == "CAPABILITY":
                self._say("* CAPABILITY IMAP4rev1")
                self._say(f"{tag} OK CAPABILITY completed")
            elif command == "LOGIN":
                user, _, password = arguments.partition(" ")
                if user.strip('"') == owner.username and password.strip('"') == owner.password:
                    authed = True
                    self._say(f"{tag} OK LOGIN completed")
                else:
                    self._say(f"{tag} NO LOGIN failed")
            elif command in ("SELECT", "EXAMINE") and authed:
                folder = arguments.strip('"')
                count = sum(m.folder == folder for m in owner.messages)
                self._say("* FLAGS (\\Seen)")
                self._say(f"* {count} EXISTS")
                mode = "READ-ONLY" if command == "EXAMINE" else "READ-WRITE"
                self._say(f"{tag} OK [{mode}] {command} completed")
            elif command == "UID" and authed and folder is not None:
                self._uid(owner, tag, arguments, folder)
            elif command == "LOGOUT":
                self._say("* BYE")
                self._say(f"{tag} OK LOGOUT completed")
                return
            else:
                self._say(f"{tag} BAD unsupported")

    def _uid(self, owner: FakeImap, tag: str, arguments: str, folder: str) -> None:
        sub, _, rest = arguments.partition(" ")
        sub = sub.upper()
        if sub == "SEARCH":
            words = rest.split(" ", 2)
            if len(words) != 3 or words[0].upper() != "HEADER":
                self._say(f"{tag} BAD only HEADER searches")
                return
            header, value = words[1], words[2].strip('"')
            found = []
            for message in owner.messages:
                if message.folder != folder:
                    continue
                parsed = BytesParser(policy=default).parsebytes(message.raw, headersonly=True)
                if value in str(parsed.get(header, "")):
                    found.append(str(message.uid))
            self._say("* SEARCH" + ("" if not found else " " + " ".join(found)))
            self._say(f"{tag} OK SEARCH completed")
        elif sub == "FETCH":
            uid_text, _, items = rest.partition(" ")
            if items.upper() != "(BODY.PEEK[])":
                self._say(f"{tag} BAD only BODY.PEEK[]")
                return
            for message in owner.messages:
                if message.folder == folder and str(message.uid) in uid_text.split(","):
                    owner.fetched.append(message.uid)
                    self.wfile.write(
                        f"* {message.uid} FETCH (UID {message.uid} BODY[] "
                        f"{{{len(message.raw)}}}\r\n".encode()
                        + message.raw
                        + b")\r\n"
                    )
            self._say(f"{tag} OK FETCH completed")
        else:
            self._say(f"{tag} BAD unsupported")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
