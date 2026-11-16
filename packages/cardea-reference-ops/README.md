# cardea-reference-ops

Reference operations packages for the worked examples in [ADR-034](../../docs/decisions/034-agent-groups-and-shared-records.md): a software team and a trading desk. **Every operation is fake.** They keep state in memory, in one `Fakes` instance per kernel process, and never touch a network, a repository or a broker. Use them to learn and test the primitives, never as integrations.

| Operation | Effect | What the fake does |
|---|---|---|
| `vcs.open_change` | effect, scope `/record_id`, reconciled | records a change for a work record |
| `vcs.read_change` | read | reports whether a change exists |
| `vcs.comment` | effect, scope `/change_id`+`/body`, reconciled | records a comment |
| `ci.status` | read | always `passed` |
| `risk.check` | read | the desk's exposure and its limit (1000 per order) |
| `market.quotes` | read | a fixed quote |
| `broker.place_order` | effect, scope `/order_ref`, reconciled | places an order; refuses one over the limit with `risk_limit` |
| `broker.fills` | read | the placed quantity, as filled |

Each operation is an ADR-023 installed operation:
- a descriptor, an MCP tool plus Cardea's binding, in `src/cardea_reference_ops/descriptors/`;
- handlers and reconcilers registered under the `cardea.operations` entry-point group.

## Install

Install the package into the kernel's environment, then copy the descriptors into the host's `operations_dir` (each file `0600`, owned by the service user):

```sh
uv pip install --python apps/api/.venv packages/cardea-reference-ops
install -m 0600 packages/cardea-reference-ops/src/cardea_reference_ops/descriptors/*.json /etc/cardea/operations/
```

The kernel's API tests install it as an editable development dependency.

For the examples that use these operations, see [`examples/teams`](../../examples/teams/README.md).
