"""Live smoke test of the email transport against the host's real provider (ADR-039).

    python3 email_live.py --alias client [--config /etc/cardea/email.json] [--reply-wait 900]

Run it as the account that owns the private email configuration and its key file
(normally `cardea-kernel`). It uses the configured adapter (AgentMail or SMTP/IMAP) and
the operations package's own checks, not the daemon. **It sends one real email** to the
address of `--alias` and then waits for a human to answer it from that address.

Checks, written to the evidence JSON:

- one send to the alias returns a thread id;
- AgentMail only: repeating the send with the same idempotency key returns the same
  message and sends nothing more;
- the reconciler's lookup (`find_sent`) finds the send by its effect key;
- within `--reply-wait` seconds, `email.poll_replies` accepts at least one reply on that
  thread from the alias's address (it accepts nothing from another sender by construction;
  dropped replies are counted by reason).

The evidence holds booleans, counts and SHA-256 digests only: no address, subject, body,
inbox id or key. Nothing here has been run yet (see the README).
"""

import argparse
import asyncio
import hashlib
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    REPOSITORY,
    Evidence,
    add_common_arguments,
    claim,
    run_script,
    use_kernel,
)

NAME = "email-live"
EMAIL_OPS = REPOSITORY / "packages" / "cardea-email-ops" / "src"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    add_common_arguments(parser, NAME)
    parser.add_argument("--alias", required=True, help="the recipient alias to email")
    parser.add_argument("--config", type=Path, default=Path("/etc/cardea/email.json"))
    parser.add_argument(
        "--reply-wait", type=int, default=900, help="seconds to wait for a human reply"
    )
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    work = claim(args.work, NAME)
    use_kernel(args.cardea_lib)
    try:
        import cardea_email_ops  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(EMAIL_OPS))
    from cardea_email_ops import Email
    from cardea_email_ops.config import load
    from cardea_email_ops.transport import Outgoing

    evidence = Evidence(NAME, work, args.out)

    def body() -> None:
        config = load(args.config)
        email = Email(config)
        address = config.address(args.alias)
        if address is None:
            raise SystemExit(f"alias {args.alias!r} is not in the configuration")
        evidence.observe("transport", config.transport.kind)
        key = secrets.token_hex(32)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        message = Outgoing(
            to=address,
            subject=f"Cardea email smoke test {stamp}",
            body="This is a Cardea acceptance test. Please reply with one short line.",
            effect_key=key,
        )
        sent = asyncio.run(email.transport.send(message))
        evidence.check("send_returns_a_thread", bool(sent.thread_id), thread=digest(sent.thread_id))
        if config.transport.kind == "agentmail":
            again = asyncio.run(email.transport.send(message))
            evidence.check(
                "idempotent_retry_returns_the_same_message",
                again.message_id == sent.message_id and again.thread_id == sent.thread_id,
            )
        found = None
        deadline = time.monotonic() + 120
        while found is None and time.monotonic() < deadline:
            found = asyncio.run(email.transport.find_sent(key))
            if found is None:
                time.sleep(5)
        evidence.check(
            "reconciler_finds_the_send",
            found is not None and found.thread_id == sent.thread_id,
        )
        print(f"Reply to the smoke-test email from the {args.alias} address now.", flush=True)
        thread = {
            "arguments": {"to": args.alias},
            "output": {
                "thread_id": sent.thread_id,
                "message_id": sent.message_id,
                "sent_at": datetime.now(UTC).isoformat(),
            },
        }
        accepted = 0
        dropped: dict[str, int] = {}
        deadline = time.monotonic() + args.reply_wait
        while time.monotonic() < deadline:
            polled = asyncio.run(email.poll_replies(thread))  # type: ignore[arg-type]
            accepted = len(polled.replies)
            dropped = {}
            for item in polled.dropped:
                dropped[item.reason] = dropped.get(item.reason, 0) + 1
            if accepted:
                break
            time.sleep(args.poll_seconds)
        evidence.observe("replies", {"accepted": accepted, "dropped": dropped})
        evidence.check("an_allow_listed_reply_is_accepted", accepted >= 1)

    return run_script(evidence, body)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
