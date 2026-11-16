"""Generate the error-code reference from the kernel's own source.

`make error-codes` writes `docs/error-codes.md`; a test fails while that file is stale. The
list is read from the code, not maintained by hand:

- **API errors** are what a request can answer with `{"code", "message"}`: every
  `Fault(code, message, status)`, every literal `{"code": ...}` response body, every
  `StoreError` code and every `ContractErrorCode`.
- **Call and task failures** are recorded on a receipt or a task rather than returned by
  a request: `Failure`, `KernelFailure`, `WorkerFailed` and `OperationRejected` codes.

A meaning is the message the code is raised with (`{…}` marks a value filled in at run
time), so the reference says what the kernel says.
"""

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
REPOSITORY = Path(__file__).resolve().parents[5]
DOCUMENT = REPOSITORY / "docs" / "error-codes.md"
MAX_MEANINGS = 3


@dataclass
class Entry:
    statuses: set[int] = field(default_factory=set)
    meanings: list[str] = field(default_factory=list)
    modules: set[str] = field(default_factory=set)

    def add(self, module: str, meaning: str | None, status: int | None = None) -> None:
        self.modules.add(module)
        if status is not None:
            self.statuses.add(status)
        if meaning and meaning not in self.meanings:
            self.meanings.append(meaning)


def _text(node: ast.expr | None) -> str | None:
    """A message argument as text; the parts filled in at run time become `{…}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{…}" for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        return None if left is None or right is None else left + right
    return None


def _code(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == name), None)


def collect() -> tuple[dict[str, Entry], dict[str, Entry]]:
    api: dict[str, Entry] = defaultdict(Entry)
    failures: dict[str, Entry] = defaultdict(Entry)
    # The kernel, then the first-party operation packages, whose failures receipts record.
    roots = [SOURCE, *sorted(p for p in REPOSITORY.glob("packages/*/src/*") if p.is_dir())]
    paths = [(root, path) for root in roots for path in sorted(root.rglob("*.py"))]
    for root, path in paths:
        module = ".".join(path.relative_to(root.parent).with_suffix("").parts)
        tree = ast.parse(path.read_text())
        # The base class of the class each `super().__init__` call is made in.
        parents: dict[int, str] = {}
        docs: dict[int, str] = {}
        for owner in ast.walk(tree):
            if isinstance(owner, ast.ClassDef):
                bases = [b.id for b in owner.bases if isinstance(b, ast.Name)]
                for inner in ast.walk(owner):
                    if isinstance(inner, ast.Call) and bases:
                        parents[id(inner)] = bases[0]
                        docs[id(inner)] = (ast.get_docstring(owner) or "").split("\n")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ContractErrorCode":
                for item in node.body:
                    if isinstance(item, ast.Assign) and (code := _code(item.value)):
                        api[code].add(module, "A kernel contract rule refused the change", 409)
            if isinstance(node, ast.Dict):
                keys = [_code(k) if k is not None else None for k in node.keys]
                if "code" in keys and "message" in keys:
                    code = _code(node.values[keys.index("code")])
                    if code is not None:
                        api[code].add(module, _text(node.values[keys.index("message")]))
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "__init__" and isinstance(func, ast.Attribute):
                # `super().__init__(...)` in a subclass, e.g. `MemberBusy(Fault)`.
                name = parents.get(id(node), "")
                fallback = docs.get(id(node))
            else:
                fallback = None
            if name == "Fault" and (code := _code(_argument(node, 0, "code"))):
                status = _argument(node, 2, "status")
                api[code].add(
                    module,
                    _text(_argument(node, 1, "message")) or fallback,
                    status.value
                    if isinstance(status, ast.Constant) and isinstance(status.value, int)
                    else 403,
                )
            elif name == "StoreError" and (code := _code(_argument(node, 0, "code"))):
                api[code].add(module, _text(_argument(node, 1, "message")), 409)
            elif name in {"Failure", "WorkerFailed"} and (
                code := _code(_argument(node, 0, "code"))
            ):
                failures[code].add(module, _text(_argument(node, 2, "summary")))
            elif name == "KernelFailure" and (code := _code(_argument(node, 0, "code"))):
                failures[code].add(module, _text(_argument(node, 1, "message")))
            elif name == "OperationRejected" and (code := _code(_argument(node, 1, "code"))):
                failures[code].add(module, _text(_argument(node, 0, "message")) or fallback)
    return dict(api), dict(failures)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(entries: dict[str, Entry], statuses: bool) -> list[str]:
    head = "| Code | HTTP | Meaning | Raised in |" if statuses else "| Code | Meaning | Raised in |"
    rule = "|---|---|---|---|" if statuses else "|---|---|---|"
    lines = [head, rule]
    for code in sorted(entries):
        entry = entries[code]
        meaning = "; ".join(_cell(m) for m in entry.meanings[:MAX_MEANINGS]) or "—"
        if len(entry.meanings) > MAX_MEANINGS:
            meaning += f" (and {len(entry.meanings) - MAX_MEANINGS} more)"
        where = ", ".join(f"`{m}`" for m in sorted(entry.modules))
        if statuses:
            status = ", ".join(str(s) for s in sorted(entry.statuses)) or "—"
            lines.append(f"| `{code}` | {status} | {meaning} | {where} |")
        else:
            lines.append(f"| `{code}` | {meaning} | {where} |")
    return lines


def render() -> str:
    api, failures = collect()
    lines = [
        "# Error codes",
        "",
        "Generated from the kernel source by `make error-codes`; do not edit by hand. A test",
        "fails while this file is stale. Each meaning is the message the code is raised",
        "with, and `{…}` marks a value filled in at run time.",
        "",
        "## API errors",
        "",
        'What a request can answer with, as `{"schema_version", "code", "message"}`. A',
        "`Fault` carries its own status (403 unless it names one). Store and contract",
        "errors answer 409. A request that fails schema validation answers",
        "`invalid_request` (422), and an unexpected failure answers `internal_error` (500).",
        "",
        *_table(api, statuses=True),
        "",
        "## Call and task failures",
        "",
        "What a receipt's or a task's `failure.code` can hold. These are not request errors:",
        "a run or an operation task records them, and the run view and receipts show them.",
        "Codes raised in `cardea_coding_ops` come from that operations package, and codes",
        "raised in `cardea_reference_ops` from the fake reference package used by the examples.",
        "",
        *_table(failures, statuses=False),
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(), end="")
