"""Acceptance-only daemon launcher: the installed kernel's real `serve()`, with two stand-ins.

    launcher.py CONFIG WORK [--reference-ops]

- the UNENFORCED development identity of `experiments/try/devauth.py` instead of SPIFFE
  (it refuses root and host execution, and needs a `*.invalid` domain). When the
  configuration has an `oidc` block, OIDC tokens are checked by the kernel's own OIDC
  path (`cardea.service.auth.Authentication`: discovery, then RFC 7662 introspection) against
  the issuer it names, for example `oidc_issuer.Issuer`;
- `plan.PlanHarness` as the only agent harness, instead of a model;
- with `--reference-ops`, the fake operations of `packages/cardea-reference-ops`.

Everything else is the production path: DBOS dispatch and schedules, the SQLite store,
admission and the live ceiling, the per-attempt MCP endpoints, receipts and the journal.
Never point it at a real store; the scripts give it only the store they created.
"""

import argparse
import logging
from pathlib import Path
from typing import Any


class AcceptanceIdentity:
    """Development SPIFFE tokens, and real OIDC validation when an issuer is configured."""

    def __init__(self, config: Any) -> None:
        from cardea.service.auth import Authentication, discovery
        from devauth import DevAuthentication

        # The development identity refuses an OIDC block; it never sees one.
        self.dev = DevAuthentication(config.model_copy(update={"oidc": None}))
        self.oidc: Authentication | None = None
        if config.oidc is not None:
            # The kernel's Authentication without its SPIFFE Workload API client: only its
            # OIDC branch is used, exactly as the daemon runs it.
            oidc = Authentication.__new__(Authentication)
            oidc.config = config
            oidc.oidc_metadata = discovery(
                config.oidc.issuer, allow_loopback_http=config.oidc.allow_loopback_http
            )
            self.oidc = oidc

    def validate(self, token: str, kind: str = "spiffe") -> Any:
        if kind == "oidc":
            if self.oidc is None:
                from cardea.service.database import Fault

                raise Fault("invalid_credential", "Identity proof could not be validated", 401)
            return self.oidc.validate(token, "oidc")
        return self.dev.validate(token, kind)

    def runtime_token(self) -> str:
        return str(self.dev.runtime_token())

    def runtime_proof(self) -> Any:
        return self.dev.runtime_proof()

    def close(self) -> None:
        self.dev.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--reference-ops", action="store_true")
    args = parser.parse_args()

    from cardea.service import server
    from cardea.service.database import private_file
    from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, implicit_entry
    from cardea.service.models import ServiceConfig
    from cardea.service.operations import Registry
    from plan import PlanHarness

    config = ServiceConfig.model_validate_json(private_file(args.config))
    if args.work.resolve() not in config.data_dir.resolve().parents:
        raise SystemExit("the store must be inside the acceptance work directory")
    harness = PlanHarness(args.work, config.data_dir / "kernel.sqlite3")

    def harnesses(config: ServiceConfig, workers: object) -> Harnesses:
        entry = implicit_entry("acceptance-plan")
        catalog = Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
        return Harnesses({IMPLICIT_ID: harness}, catalog, concurrent_sessions=8)

    server.start_harnesses = harnesses  # type: ignore[assignment]
    if args.reference_ops:
        from cardea_reference_ops import Fakes, descriptors

        fakes = Fakes()

        def load(directory: Path | None, *, resolve: bool = True) -> Registry:
            return Registry(descriptors(), fakes.handlers() if resolve else None)

        server.Registry.load = load  # type: ignore[assignment,method-assign]
    logging.getLogger("cardea.acceptance").warning(
        "UNENFORCED development identity and scripted harness: acceptance store only"
    )
    server.serve(config, identity=AcceptanceIdentity)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
