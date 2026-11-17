"""ADR-035 on the host: a signed CloudEvent through the daemon's live TLS listener, sent by curl.

    python3 events_tls.py [--work DIR] [--listen-host 127.0.0.1] [--external-wait 0]

The daemon (`launcher.py`) runs with an ADR-025 TLS 1.3 listener on a free port, a test CA
and server certificate made with `openssl`, and one host-provisioned event trigger whose
template is an agent run. Every delivery is made by `curl`, a separate process speaking
HTTPS to the listener, and signed here with Standard Webhooks v1 (HMAC-SHA256 computed
directly, not with the kernel's library). Checks:

- a signed event is admitted and its run succeeds with the event as `trigger_event`;
- a redelivery with a new `webhook-id` answers `duplicate` with the same task, also after
  the daemon is killed and restarted;
- a wrong signature, a stale timestamp and a bearer credential instead of a signature are
  refused (401), and leave no delivery;
- plaintext HTTP and TLS 1.2 are refused by the listener.

With `--listen-host 0.0.0.0 --external-wait 600`, the script then prints a signed curl
command, with the CA file and body, for the operator to run from another machine within
the signature's five-minute tolerance, and checks that the delivery was admitted.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Evidence,
    Processes,
    add_common_arguments,
    claim,
    free_port,
    rows,
    run_script,
    use_kernel,
    wait_for,
)
from stack import Stack, agents, inspectable, people, platform, rights  # noqa: E402

NAME = "adr-035"
SUP = "sup"
NAMESPACE = "t35"
TRIGGER = "acc-hook"
SOURCE = "acceptance-sender"
TYPE = "acceptance.ping"


def openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True, timeout=120)


def certificates(directory: Path, names: list[str]) -> dict[str, Path]:
    """A throwaway CA and a server certificate for `names` (IP addresses or DNS names)."""
    directory.mkdir(mode=0o700)
    ca_key, ca, key, csr, cert = (
        directory / n for n in ("ca.key", "ca.pem", "server.key", "server.csr", "server.pem")
    )
    openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
        "-days",
        "2",
        "-subj",
        "/CN=cardea acceptance CA",
    )
    openssl(
        "req",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        "/CN=cardea acceptance listener",
    )
    entries = ",".join(
        f"IP:{n}" if n.replace(".", "").isdigit() or ":" in n else f"DNS:{n}" for n in names
    )
    extensions = directory / "server.ext"
    extensions.write_text(f"subjectAltName={entries}\nextendedKeyUsage=serverAuth\n")
    openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(cert),
        "-days",
        "2",
        "-extfile",
        str(extensions),
    )
    # The kernel reads its certificate and key only from private files (`unsafe_file`
    # otherwise); openssl writes them with the caller's umask (0664 under Debian's sudo).
    for path in (ca_key, key, cert):
        path.chmod(0o600)
    return {"ca": ca, "certificate": cert, "key": key}


def sign(secret: str, message_id: str, stamp: int, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{message_id}.{stamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def event(event_id: str) -> bytes:
    return json.dumps(
        {
            "specversion": "1.0",
            "id": event_id,
            "source": SOURCE,
            "type": TYPE,
            "subject": "acceptance",
            "datacontenttype": "application/json",
            "data": {"note": "signed by the acceptance script"},
        }
    ).encode()


def curl_argv(url: str, ca: Path, body: Path, headers: dict[str, str], *extra: str) -> list[str]:
    argv = ["curl", "--silent", "--show-error", "--cacert", str(ca), "--tlsv1.3", *extra]
    for name, value in headers.items():
        argv += ["-H", f"{name}: {value}"]
    return [*argv, "--data-binary", f"@{body}", "-w", "\n%{http_code}", url]


class Sender:
    def __init__(self, work: Path, url: str, ca: Path, secret: str) -> None:
        self.work, self.url, self.ca, self.secret = work, url, ca, secret
        self.sent = 0

    def headers(
        self, body: bytes, *, stamp: int | None = None, signature: str | None = None
    ) -> dict[str, str]:
        message_id = f"msg-{uuid.uuid4().hex}"
        stamp = stamp or int(time.time())
        return {
            "webhook-id": message_id,
            "webhook-timestamp": str(stamp),
            "webhook-signature": signature or sign(self.secret, message_id, stamp, body),
            "content-type": "application/cloudevents+json",
        }

    def post(
        self, body: bytes, headers: dict[str, str], *extra: str, url: str | None = None
    ) -> dict[str, Any]:
        self.sent += 1
        path = self.work / f"body-{self.sent}.json"
        path.write_bytes(body)
        done = subprocess.run(
            curl_argv(url or self.url, self.ca, path, headers, *extra),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        text, _, status = done.stdout.rpartition("\n")
        try:
            document = json.loads(text) if text.strip() else {}
        except ValueError:
            document = {"raw": text[:500]}
        return {
            "curl_exit": done.returncode,
            "status": int(status) if status.isdigit() else None,
            "body": document,
            "stderr": done.stderr[-500:],
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_arguments(parser, NAME)
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="the TLS listener's address (default: %(default)s)",
    )
    parser.add_argument(
        "--server-name",
        action="append",
        default=[],
        help="extra certificate names for an external sender (repeatable)",
    )
    parser.add_argument(
        "--external-wait",
        type=int,
        default=0,
        help="seconds to wait for a delivery from another machine (default: 0)",
    )
    args = parser.parse_args(argv)
    lib = use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    processes = Processes(work)

    def body() -> None:
        for tool in ("curl", "openssl"):
            done = subprocess.run(
                [tool, "--version" if tool == "curl" else "version"],
                capture_output=True,
                text=True,
                check=False,
            )
            evidence.observe(f"{tool}_version", done.stdout.splitlines()[:1])
        port = free_port("127.0.0.1" if args.listen_host == "127.0.0.1" else "0.0.0.0")
        names = sorted(
            {
                "127.0.0.1",
                "localhost",
                *args.server_name,
                *([args.listen_host] if args.listen_host != "0.0.0.0" else []),
            }
        )
        tls = certificates(work / "tls", names)
        secret = "whsec_" + base64.b64encode(os.urandom(32)).decode()
        secret_file = work / "event-secret"
        secret_file.write_text(secret + "\n")
        secret_file.chmod(0o600)
        network = {
            "host": args.listen_host,
            "port": port,
            "certificate_file": str(tls["certificate"]),
            "key_file": str(tls["key"]),
        }
        stack = Stack(work, processes, lib, network=network)
        from cardea.service.models import RIGHTS

        granted = rights(
            SUP,
            NAMESPACE,
            [*RIGHTS, "objects.read", "objects.write", "models.invoke", "runs.recent"],
        )
        trigger = {
            "trigger_id": TRIGGER,
            "supervisor_id": SUP,
            "event": {"source": SOURCE, "types": [TYPE], "secret_file": str(secret_file)},
            "template_kind": "agent-run",
            "template": {
                "agent_id": "hook",
                "namespace": NAMESPACE,
                "objective": "Acknowledge an acceptance event",
                "instructions": "Play the acceptance plan.",
                "operations": ["runs.recent"],
                # The supervisor reads back the run the trigger starts (stack.INSPECT).
                "grants": inspectable(
                    ["objects.read", "objects.write", "models.invoke", "runs.recent"]
                ),
                "max_attempts": 1,
                "attempt_timeout_seconds": 60,
            },
        }
        stack.provision(
            {
                "principals": [*people("runtime", SUP), *agents("hook")],
                "permissions": granted,
                "platform": platform(granted),
                "triggers": [trigger],
            }
        )
        stack.start()
        host = "127.0.0.1" if args.listen_host == "0.0.0.0" else args.listen_host
        url = f"https://{host}:{port}/v0.2/events/{TRIGGER}"
        sender = Sender(work, url, tls["ca"], secret)
        evidence.observe("listener", {"host": args.listen_host, "port": port, "names": names})

        def deliveries() -> list[dict[str, Any]]:
            return rows(
                stack.database,
                "SELECT event_id, outcome, task_id, code FROM "
                "trigger_deliveries WHERE trigger_id=? ORDER BY delivery_id",
                TRIGGER,
            )

        first_id = f"evt-{uuid.uuid4().hex}"
        payload = event(first_id)
        first = sender.post(payload, sender.headers(payload))
        task_id = first["body"].get("task_id")
        evidence.check(
            "signed_event_admitted_over_tls",
            first["status"] == 202 and first["body"].get("outcome") == "admitted",
            response=first,
        )
        if task_id:
            view = stack.finished(SUP, str(task_id))
            request = rows(
                stack.database, "SELECT request_json FROM task_bindings WHERE task_id=?", task_id
            )
            evidence.check(
                "event_run_succeeds_with_the_event",
                view["task"]["state"] == "succeeded"
                and bool(request)
                and first_id in request[0]["request_json"],
                state=view["task"]["state"],
            )
        again = sender.post(payload, sender.headers(payload))
        evidence.check(
            "redelivery_is_duplicate_of_the_same_task",
            again["body"].get("outcome") == "duplicate" and again["body"].get("task_id") == task_id,
            response=again,
        )
        stack.restart_after_kill()
        after = sender.post(payload, sender.headers(payload))
        evidence.check(
            "redelivery_after_restart_is_duplicate",
            after["body"].get("outcome") == "duplicate" and after["body"].get("task_id") == task_id,
            response=after,
        )

        recorded = len(deliveries())
        other = event(f"evt-{uuid.uuid4().hex}")
        wrong = sender.post(
            other,
            sender.headers(other, signature="v1," + base64.b64encode(os.urandom(32)).decode()),
        )
        stale = sender.post(other, sender.headers(other, stamp=int(time.time()) - 900))
        bearer = sender.post(
            other,
            {
                "authorization": f"Bearer {stack.token(SUP)}",
                "content-type": "application/cloudevents+json",
            },
        )
        evidence.check(
            "bad_signature_stale_and_bearer_refused",
            [r["status"] for r in (wrong, stale, bearer)] == [401, 401, 401]
            and len(deliveries()) == recorded,
            responses={"wrong": wrong, "stale": stale, "bearer": bearer},
        )
        plain = sender.post(other, sender.headers(other), url=url.replace("https://", "http://"))
        evidence.check(
            "plaintext_http_refused",
            plain["status"] in {None, 0, 400} and plain["body"].get("outcome") is None,
            response=plain,
        )
        old = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--cacert",
                str(tls["ca"]),
                "--tls-max",
                "1.2",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        evidence.check(
            "tls_1_2_refused",
            old.returncode != 0,
            curl_exit=old.returncode,
            stderr=old.stderr[-300:],
        )

        if args.external_wait > 0:
            external_id = f"external-{uuid.uuid4().hex}"
            outside = event(external_id)
            headers = sender.headers(outside)
            (work / "external-body.json").write_bytes(outside)
            command = curl_argv(
                f"https://{args.server_name[0] if args.server_name else '<this host>'}"
                f":{port}/v0.2/events/{TRIGGER}",
                Path("ca.pem"),
                Path("external-body.json"),
                headers,
            )
            (work / "external-curl.txt").write_text(" ".join(_quote(a) for a in command) + "\n")
            print(
                f"Copy {tls['ca']} and {work / 'external-body.json'} to another machine and run, "
                f"within five minutes:\n  {' '.join(_quote(a) for a in command)}",
                flush=True,
            )
            try:
                wait_for(
                    lambda: any(
                        d["event_id"] == external_id and d["outcome"] == "admitted"
                        for d in deliveries()
                    ),
                    args.external_wait,
                    2,
                )
                arrived = True
            except TimeoutError:
                arrived = False
            evidence.check("external_sender_delivery_admitted", arrived)
        stack.stop()

    try:
        return run_script(evidence, body)
    finally:
        processes.close()


def _quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
