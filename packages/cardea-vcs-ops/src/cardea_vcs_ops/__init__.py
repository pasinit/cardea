"""Repository-host operations for Cardea agent teams (ADR-037 slices 4 and 5).

`vcs.push_branch`, `vcs.open_change`, `vcs.comment`, `vcs.review`, `vcs.read_change` and
`vcs.check_repository`
against a GitHub-compatible REST API. Each is an ADR-023 installed operation: an MCP tool
descriptor in `descriptors/` plus a handler under the `cardea.operations` entry-point group.

The entry points use one process-wide `Vcs`, built on first use from the host-owner
configuration file (`CARDEA_VCS_CONFIG`) and the repository registry
(`CARDEA_REPOSITORIES`). Tests build their own `Vcs` and pass `handlers()` to the
operation registry, as the reference package does.
"""

from importlib.resources import files

from pydantic import JsonValue

from cardea.service.database import Fault
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciliation,
)
from cardea_vcs_ops.config import load
from cardea_vcs_ops.ops import Vcs

__all__ = ["Vcs", "descriptors", "in_process_descriptors"]


def descriptors() -> tuple[OperationDescriptor, ...]:
    """The package's descriptors, as installed under the host's `operations_dir`."""
    folder = files(__package__) / "descriptors"
    return tuple(
        OperationDescriptor.model_validate_json(entry.read_text())
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    )


def in_process_descriptors() -> tuple[OperationDescriptor, ...]:
    """The descriptors with in-process handlers instead of the `vcs` worker, for tests and
    rehearsals against an in-process fake host only: installed, they would put the
    repository credential in the service process."""
    return tuple(
        d.model_copy(
            update={"cardea": d.cardea.model_copy(update={"executor": None, "handler": d.name})}
        )
        for d in descriptors()
    )


_DEFAULT: Vcs | None = None


def _vcs() -> Vcs:
    global _DEFAULT
    if _DEFAULT is None:
        try:
            _DEFAULT = Vcs(load())
        except Fault as error:
            # An unreadable or disagreeing configuration sends nothing: a definite refusal,
            # never an effect in doubt.
            raise OperationRejected(error.message, error.code) from None
    return _DEFAULT


async def push_branch(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().push_branch(arguments, ctx)


async def find_branch(arguments: dict[str, JsonValue], ctx: OperationContext) -> Reconciliation:
    return await _vcs().find_branch(arguments, ctx)


async def open_change(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().open_change(arguments, ctx)


async def find_change(arguments: dict[str, JsonValue], ctx: OperationContext) -> Reconciliation:
    return await _vcs().find_change(arguments, ctx)


async def comment(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().comment(arguments, ctx)


async def find_comment(arguments: dict[str, JsonValue], ctx: OperationContext) -> Reconciliation:
    return await _vcs().find_comment(arguments, ctx)


async def review(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().review(arguments, ctx)


async def find_review(arguments: dict[str, JsonValue], ctx: OperationContext) -> Reconciliation:
    return await _vcs().find_review(arguments, ctx)


async def read_change(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().read_change(arguments, ctx)


async def check_repository(arguments: dict[str, JsonValue], ctx: OperationContext) -> JsonValue:
    return await _vcs().check_repository(arguments, ctx)
