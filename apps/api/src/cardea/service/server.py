"""Single-owner native daemon entry point: private Unix socket, optional TLS 1.3 listener."""

import asyncio
import json
import logging
import os
import socket
import ssl
import stat
import threading
from collections.abc import Callable, Sequence
from importlib.metadata import version
from uuid import uuid4

import uvicorn
from dbos import DBOS
from starlette.types import ASGIApp

from cardea.hosts.debian import DebianHost
from cardea.runtime.agent_session import AgentSession, ClaudeAgentSession
from cardea.runtime.openrouter import OpenRouterModel
from cardea.service.agent_runtime import AgentRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, IdentityProvider
from cardea.service.confined import ConfinedAgentSession, Workers
from cardea.service.database import Database, Fault, private_file, service_lock
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel
from cardea.service.models import NetworkListener, ServiceConfig
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import Registry
from cardea.service.runtime import Runtime

log = logging.getLogger(__name__)

# Structured fields callers may attach via `extra`; anything else is dropped.
LOG_FIELDS = ("task_id", "code", "reason", "method", "path")


class JournalFormatter(logging.Formatter):
    """One JSON object per line for journald; never includes request bodies or credentials."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        entry |= {k: getattr(record, k) for k in LOG_FIELDS if hasattr(record, k)}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter())
    root = logging.getLogger("cardea")
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("CARDEA_LOG_LEVEL", "INFO").upper())
    root.propagate = False


def api_config(app: ASGIApp) -> uvicorn.Config:
    """One set of HTTP limits for every listener; the app's `Bounds` caps bodies."""
    config = uvicorn.Config(
        app,
        log_level="critical",
        access_log=False,
        # Peer addresses and forwarding headers grant nothing; never rewrite them.
        proxy_headers=False,
        ws="none",
        server_header=False,
        timeout_keep_alive=2,
        timeout_graceful_shutdown=5,
        limit_concurrency=64,
        h11_max_incomplete_event_size=32768,
    )
    config.load()
    return config


def tls_context(network: NetworkListener) -> ssl.SSLContext:
    """TLS 1.3-only server context from private files; optional client-CA verification."""
    for path in (network.certificate_file, network.key_file):
        private_file(path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_TICKET
    try:
        context.load_cert_chain(network.certificate_file, network.key_file)
        if network.client_ca_file:
            context.load_verify_locations(cadata=private_file(network.client_ca_file).decode())
            context.verify_mode = ssl.CERT_REQUIRED
    except (ssl.SSLError, ValueError):
        raise Fault("tls_configuration", "TLS certificate, key or CA is invalid", 500) from None
    return context


def bind_network(host: str, port: int) -> socket.socket:
    family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    listener = socket.create_server((host, port), family=family[0][0], backlog=128)
    listener.setblocking(False)
    return listener


def serve_listeners(
    app: ASGIApp,
    listeners: Sequence[tuple[socket.socket, ssl.SSLContext | None]],
    stop: threading.Event | None = None,
) -> None:
    """Serve one app on several bound sockets; stopping any listener stops them all."""
    servers: list[tuple[uvicorn.Server, socket.socket]] = []
    for listener, context in listeners:
        config = api_config(app)
        # Uvicorn wraps every socket of a server in `config.ssl`; one server per listener.
        config.ssl = context
        servers.append((uvicorn.Server(config), listener))

    async def run() -> None:
        async def together() -> None:
            while not any(server.should_exit for server, _ in servers):
                if stop is not None and stop.is_set():
                    break
                await asyncio.sleep(0.05)
            for server, _ in servers:
                server.should_exit = True

        watcher = asyncio.create_task(together())
        try:
            await asyncio.gather(*(server.serve(sockets=[sock]) for server, sock in servers))
        finally:
            watcher.cancel()

    asyncio.run(run())


def serve(
    config: ServiceConfig,
    fault: Callable[[str], None] = lambda stage: None,
    identity: Callable[[ServiceConfig], IdentityProvider] = Authentication,
) -> None:
    """Run the daemon. The installed CLI never passes `identity`; only test/dev launchers do."""
    if any(name.startswith("DBOS_") for name in os.environ):
        raise Fault("unsafe_environment", "Remove inherited DBOS environment overrides", 500)
    configure_logging()
    with service_lock(config.data_dir):
        database = Database(config.data_dir)
        auth: IdentityProvider | None = None
        kernel: Kernel | None = None
        runtime_launched = False
        exit_code = 1
        try:
            # Explicit initialization/provisioning is required, never implicit identity creation.
            database.check_version()
            # DBOS owns its schema migration; retain a whole-store preflight snapshot.
            # A different runtime release needs an explicit, tested upgrade procedure.
            runtime_version = f"dbos={version('dbos')};workflow=cardea-m1-v1"
            if version("dbos") != "3.0.0":
                raise Fault("unsupported_runtime", "Use the tested DBOS release", 500)
            with database.engine.begin() as conn:
                previous = conn.exec_driver_sql(
                    "SELECT value FROM service_meta WHERE name='runtime_version'"
                ).scalar_one_or_none()
            if previous is not None and previous != runtime_version:
                raise Fault("unsupported_runtime", "Runtime upgrade requires migration", 500)
            if previous is None:
                database.backup(config.data_dir / f"before-runtime-{uuid4().hex}.sqlite3")
            auth = identity(config)
            DBOS(
                config={
                    "name": "cardea-kernel",
                    "system_database_url": database.url,
                    "application_version": "cardea-m1-v1",
                    "executor_id": "cardea-single-host",
                    "enable_otlp": False,
                    "log_level": "ERROR",
                    "use_listen_notify": False,
                    "notification_listener_polling_interval_sec": 0.05,
                }
            )
            host = DebianHost() if config.host_execution else None
            if host:
                host.probe()
            model = None
            if config.agent_runtime:
                model = OpenRouterModel(
                    private_file(config.agent_runtime.api_key_file).decode().strip()
                    if config.agent_runtime.api_key_file
                    else os.environ.get(config.agent_runtime.api_key_env or "", ""),
                    config.agent_runtime.model,
                )
                if host is None:
                    raise Fault(
                        "unsupported_host", "Agent execution requires the confined host", 503
                    )
            operations = Registry.load(config.operations_dir)
            # Confined workers share the M2 helper; profiles are root-owned (ADR-028).
            workers = Workers(host) if host else None
            session: AgentSession | None = None
            if config.agent_session and config.agent_session.confined_profile:
                chosen = config.agent_session
                if workers is None:
                    raise Fault(
                        "unsupported_host", "Confined sessions require the confined host", 503
                    )
                assert chosen.confined_worker is not None and chosen.confined_profile
                session = ConfinedAgentSession(
                    workers, chosen.confined_profile, chosen.confined_worker, chosen.model
                )
            elif config.agent_session:
                chosen = config.agent_session
                session = ClaudeAgentSession(
                    model=chosen.model,
                    api_key=private_file(chosen.api_key_file).decode().strip()
                    if chosen.api_key_file
                    else None,
                    oauth_token=private_file(chosen.oauth_token_file).decode().strip()
                    if chosen.oauth_token_file
                    else None,
                )
            kernel = Kernel(
                database,
                auth,
                fault,
                host=host,
                model=model,
                operations=operations,
                session=session,
                workers=workers,
                max_task_request_bytes=config.max_task_request_bytes,
            )
            if config.agent_session:
                kernel.session_limit = config.agent_session.concurrent_sessions
            runtime = Runtime(kernel)
            runtime_launched = True
            runtime.launch()
            if host:

                def maintain() -> None:
                    timer = threading.Event()
                    while True:
                        # Pending rows stay visible and retry after transient cleanup failure.
                        try:
                            HostRuntime(kernel).cleanup()
                            AgentRuntime(kernel).cleanup()
                        except Exception:
                            log.exception("periodic host cleanup failed")
                        timer.wait(5)

                threading.Thread(target=maintain, name="cardea-host-cleanup", daemon=True).start()
            if operations.descriptors:

                def reconcile() -> None:
                    timer = threading.Event()
                    while True:
                        # Uncertain effects settle only from observed destination state.
                        try:
                            OperationRuntime(kernel).reconcile_pending()
                        except Exception:
                            log.exception("periodic reconciliation failed")
                        timer.wait(30)

                threading.Thread(target=reconcile, name="cardea-reconcile", daemon=True).start()
            with database.engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO service_meta(name,value) VALUES('runtime_version',?)",
                    (runtime_version,),
                )
            path = config.data_dir / "kernel.sock"
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise Fault("unsafe_socket", "Refusing to replace a non-owned socket", 500)
                path.unlink()
            listeners: list[tuple[socket.socket, ssl.SSLContext | None]] = []
            try:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listeners.append((listener, None))
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(128)
                if config.network:
                    # Validate certificate files before binding; no plaintext fallback.
                    context = tls_context(config.network)
                    network = bind_network(config.network.host, config.network.port)
                    listeners.append((network, context))
                serve_listeners(create_app(kernel, config.max_request_bytes), listeners)
                exit_code = 0
            finally:
                for sock, _ in listeners:
                    sock.close()
        finally:
            if runtime_launched:
                # DBOS 3's synchronous executor shutdown does not join active workflows.
                # This entry point owns the process: retain the lock until OS termination.
                # Uvicorn has drained requests; SQLite rolls back any uncommitted phase.
                # Never let a surviving worker outlive the exclusive store owner.
                os._exit(exit_code)
            DBOS.destroy(destroy_registry=True)
            if kernel:
                kernel.close()
            if auth:
                auth.close()
            database.close()
