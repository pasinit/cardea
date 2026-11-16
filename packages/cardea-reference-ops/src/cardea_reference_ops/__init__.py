"""Reference operations packages for ADR-034's worked examples. Every operation is fake.

Nothing here touches a network, a repository or a broker. State lives in memory, in one
`Fakes` instance per process (or per test), so the examples run anywhere:

- version control and CI for the software team: `vcs.open_change`, `vcs.read_change`,
  `vcs.comment`, `ci.status`;
- risk, quotes and a broker for the trading desk: `risk.check`, `market.quotes`,
  `broker.place_order` (refuses orders over the desk limit), `broker.fills`.

Each operation is an ADR-023 installed operation: an MCP tool descriptor in `descriptors/`
plus a handler registered under the `cardea.operations` entry-point group. Effects declare
an ADR-026 idempotency scope and a reconciler that looks the effect up in the fake store.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from pydantic import JsonValue

from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciliation,
)

# The desk's per-order quantity limit; `broker.place_order` refuses anything larger.
DESK_LIMIT = 1000.0

Handler = Callable[[dict[str, JsonValue], OperationContext], Awaitable[Any]]


def descriptors() -> tuple[OperationDescriptor, ...]:
    """The package's descriptors, as installed under the host's `operations_dir`."""
    folder = files(__package__) / "descriptors"
    return tuple(
        OperationDescriptor.model_validate_json(entry.read_text())
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    )


@dataclass
class Fakes:
    """In-memory stand-ins for a code host and a broker."""

    changes: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    comments: dict[str, list[str]] = field(default_factory=dict)
    orders: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    placements: int = 0

    # Version control and CI

    async def open_change(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        record_id = str(arguments["record_id"])
        change: dict[str, JsonValue] = {
            "change_id": f"change-{ctx.effect_key[:10]}",
            "record_id": record_id,
        }
        self.changes[ctx.effect_key] = change
        return change

    async def find_change(
        self, arguments: dict[str, JsonValue], ctx: OperationContext
    ) -> Reconciliation:
        found = self.changes.get(ctx.effect_key)
        return Reconciliation(applied=found is not None, output=found)

    async def read_change(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        change_id = arguments.get("change_id")
        known = any(c["change_id"] == change_id for c in self.changes.values())
        return {"change_id": change_id, "status": "open" if known else "unknown"}

    async def comment(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        self.comments.setdefault(ctx.effect_key, []).append(str(arguments["body"]))
        return {"comment_id": f"comment-{ctx.effect_key[:10]}"}

    async def find_comment(
        self, arguments: dict[str, JsonValue], ctx: OperationContext
    ) -> Reconciliation:
        if ctx.effect_key not in self.comments:
            return Reconciliation(applied=False)
        return Reconciliation(applied=True, output={"comment_id": f"comment-{ctx.effect_key[:10]}"})

    async def ci_status(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        return {"change_id": arguments.get("change_id"), "state": "passed"}

    # Risk, quotes and the broker

    async def risk_check(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        exposure = sum(float(str(o["qty"])) for o in self.orders.values())
        return {"exposure": exposure, "limit": DESK_LIMIT}

    async def quotes(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        return {"instrument": arguments.get("instrument", "XYZ"), "bid": 10.4, "ask": 10.6}

    async def place_order(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        qty = float(str(arguments["qty"]))
        if qty > DESK_LIMIT:
            raise OperationRejected("The order would breach the desk limit", "risk_limit")
        self.placements += 1
        order: dict[str, JsonValue] = {"broker_order_id": f"B-{ctx.effect_key[:8]}", "qty": qty}
        self.orders[str(arguments["order_ref"])] = order
        return {"broker_order_id": order["broker_order_id"]}

    async def find_order(
        self, arguments: dict[str, JsonValue], ctx: OperationContext
    ) -> Reconciliation:
        found = self.orders.get(str(arguments["order_ref"]))
        if found is None:
            return Reconciliation(applied=False)
        return Reconciliation(applied=True, output={"broker_order_id": found["broker_order_id"]})

    async def fills(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        found = self.orders.get(str(arguments["order_ref"]))
        return {"order_ref": arguments["order_ref"], "filled_qty": found["qty"] if found else 0}

    def handlers(self) -> dict[str, Handler]:
        """Entry-point name to handler, bound to this instance."""
        return {
            "reference.vcs.open_change": self.open_change,
            "reference.vcs.find_change": self.find_change,
            "reference.vcs.read_change": self.read_change,
            "reference.vcs.comment": self.comment,
            "reference.vcs.find_comment": self.find_comment,
            "reference.ci.status": self.ci_status,
            "reference.risk.check": self.risk_check,
            "reference.market.quotes": self.quotes,
            "reference.broker.place_order": self.place_order,
            "reference.broker.find_order": self.find_order,
            "reference.broker.fills": self.fills,
        }


# The process-wide fakes behind the entry points (one kernel process, one fake world).
FAKES = Fakes()
open_change = FAKES.open_change
find_change = FAKES.find_change
read_change = FAKES.read_change
comment = FAKES.comment
find_comment = FAKES.find_comment
ci_status = FAKES.ci_status
risk_check = FAKES.risk_check
market_quotes = FAKES.quotes
place_order = FAKES.place_order
find_order = FAKES.find_order
fills = FAKES.fills
