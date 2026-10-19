"""Use `python -m cardea.service --help`; never print credentials or raw provider failures."""

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

from cardea.service import admin, maintenance
from cardea.service.client import ClientError, KernelClient
from cardea.service.database import Database, Fault, private_file
from cardea.service.kernel import provision
from cardea.service.login import OIDCLogin, session_credentials, store_session
from cardea.service.models import (
    AgentInput,
    AgentTaskRequest,
    DelegationTaskRequest,
    HostObjectTaskRequest,
    HostTaskRequest,
    Provisioning,
    ServiceConfig,
    Signal,
    TaskRequest,
)
from cardea.service.operations import Registry
from cardea.service.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardea authenticated kernel")
    commands = parser.add_subparsers(dest="command", required=True)
    administrative = {
        "init",
        "provision",
        "serve",
        "backup",
        "restore",
        "verify",
        "diagnostics",
        "prune-backups",
    }
    for name in sorted(administrative):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name in {"provision", "backup", "restore"}:
            command.add_argument("path", type=Path)
        if name == "prune-backups":
            command.add_argument("--keep", type=int, default=3)
            command.add_argument("--days", type=int, default=7)
            command.add_argument("--apply", action="store_true")
    login = commands.add_parser("login")
    login.add_argument("--issuer", required=True)
    login.add_argument("--client-id", required=True)
    login.add_argument("--port", type=int, default=8765)
    login.add_argument("--session", type=Path, required=True)
    login.add_argument("--loopback-test-provider", action="store_true")
    for name in (
        "submit",
        "submit-host",
        "submit-host-object",
        "submit-agent",
        "submit-delegation",
        "children",
        "append-input",
        "ingest-agent-directory",
        "agent-receipts",
        "inspect",
        "resume",
        "cancel",
        "output",
        "events",
        "receipt",
    ):
        command = commands.add_parser(name)
        endpoint = command.add_mutually_exclusive_group(required=True)
        endpoint.add_argument("--socket", type=Path)
        endpoint.add_argument("--url", help="https://host:port of a TLS listener (ADR-025)")
        command.add_argument("--ca-bundle", type=Path, help="CA certificates for --url")
        command.add_argument("--client-cert", type=Path, help="mutual-TLS certificate for --url")
        command.add_argument("--client-key", type=Path, help="mutual-TLS key for --url")
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--session", type=Path)
        group.add_argument("--spiffe-socket")
        command.add_argument("--spiffe-id")
        command.add_argument("--audience", default="cardea-kernel")
        command.add_argument("value", help="request JSON file for submit; task ID otherwise")
        if name in {"append-input", "ingest-agent-directory"}:
            command.add_argument("--task-id", required=True)
        if name in {"cancel", "resume"}:
            command.add_argument("--request-id", default=None)
        if name == "events":
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.command in administrative:
        config = ServiceConfig.model_validate_json(private_file(args.config))
        if args.command == "init":
            admin.initialize(config.data_dir)
        elif args.command == "serve":
            serve(config)
        elif args.command == "backup":
            admin.backup(config.data_dir, args.path)
        elif args.command == "restore":
            print(json.dumps(admin.restore(config.data_dir, args.path)))
        elif args.command == "verify":
            print(json.dumps(maintenance.verify(config.data_dir)))
        elif args.command == "diagnostics":
            print(json.dumps(maintenance.diagnostics(config)))
        elif args.command == "prune-backups":
            print(
                json.dumps(
                    maintenance.prune_backups(
                        config.data_dir, keep=args.keep, days=args.days, apply=args.apply
                    )
                )
            )
        else:
            database = Database(config.data_dir)
            try:
                provision(
                    database,
                    Provisioning.model_validate_json(private_file(args.path)),
                    Registry.load(config.operations_dir, resolve=False),
                )
            finally:
                database.close()
        return
    if args.command == "login":
        redirect = f"http://127.0.0.1:{args.port}/callback"
        flow = OIDCLogin(
            args.issuer, args.client_id, redirect, loopback_test=args.loopback_test_provider
        )
        response_url: list[str] = []

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/callback":
                    self.send_error(404)
                    return
                response_url.append(f"http://127.0.0.1:{args.port}" + self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Return to the Cardea client to finish signing in.")

            def log_message(self, format: str, *values: object) -> None:
                pass

        try:
            with HTTPServer(("127.0.0.1", args.port), Callback) as server:
                server.timeout = 180
                url = flow.start()
                print("Open this provider login URL:", url)
                webbrowser.open(url)
                server.handle_request()
            if not response_url:
                raise Fault("login_timeout", "Login timed out", 401)
            store_session(args.session, flow.complete(response_url[0]))
            print("Session saved privately.")
        finally:
            flow.close()
        return
    if args.url and not args.ca_bundle:
        parser.error("--ca-bundle is required with --url")
    if (args.client_cert is None) != (args.client_key is None) or (
        args.client_cert and not args.url
    ):
        parser.error("--client-cert and --client-key go together, with --url")
    target: Path | str = args.socket or args.url
    client_cert = (args.client_cert, args.client_key) if args.client_cert else None
    if args.session:
        client = KernelClient(
            target,
            lambda: session_credentials(args.session),
            ca_bundle=args.ca_bundle,
            client_cert=client_cert,
        )
    else:
        if not args.spiffe_id:
            parser.error("--spiffe-id is required with --spiffe-socket")
        client = KernelClient.for_workload(
            target,
            args.spiffe_socket,
            args.spiffe_id,
            args.audience,
            ca_bundle=args.ca_bundle,
            client_cert=client_cert,
        )
    try:
        if args.command == "submit":
            result = client.submit(
                TaskRequest.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "submit-delegation":
            result = client.submit_delegation(
                DelegationTaskRequest.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "submit-agent":
            result = client.submit_agent(
                AgentTaskRequest.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "ingest-agent-directory":
            from cardea.runtime.directory import deliver

            result = deliver(client, args.task_id, Path(args.value)).model_dump(mode="json")
        elif args.command == "append-input":
            result = client.append_input(
                args.task_id, AgentInput.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "submit-host-object":
            result = client.submit_host_object(
                HostObjectTaskRequest.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "submit-host":
            result = client.submit_host(
                HostTaskRequest.model_validate_json(Path(args.value).read_bytes())
            ).model_dump(mode="json")
        elif args.command == "inspect":
            result = client.inspect(args.value).model_dump(mode="json")
        elif args.command in {"cancel", "resume"}:
            result = client.control(
                args.value,
                Signal(request_id=args.request_id or str(uuid4())),
                cancel=args.command == "cancel",
            ).model_dump(mode="json")
        elif args.command == "events":
            result = client.request(
                "GET",
                f"/tasks/{args.value}/events",
                params={"after": args.after, "limit": args.limit},
            )
        else:
            result = client.request("GET", f"/tasks/{args.value}/{args.command}")
        print(json.dumps(result, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except (Fault, ClientError) as error:
        raise SystemExit(error.code) from None
    except Exception:
        raise SystemExit("command_failed") from None
