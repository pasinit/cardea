"""ADR-036 slice 1: CEL implementation fit probes.

Runs the same approval-rule cases against both shortlisted Python CEL implementations and
records what each one does. This is an observation harness, not Cardea's evaluator.

    python fit.py                 # print the evidence document
    python fit.py --out FILE      # also write it
"""

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

CANDIDATES = {"cel-python": "celpy", "common-expression-language": "cel"}
# The activation every approval rule sees (ADR-036 item 1): args, operation, namespace.
OPERATION = "payments.create"
NAMESPACE = "acme"

# (case id, expression, arguments, expected outcome under the CEL specification)
# Expected is True, False or "error". "error" means the implementation must surface an error
# rather than a value, so Cardea can fail closed.
CASES: list[tuple[str, str, dict[str, Any], bool | str]] = [
    ("int_gt_above", "args.amount > 500", {"amount": 600}, True),
    ("int_gt_below", "args.amount > 500", {"amount": 100}, False),
    ("int_gt_equal", "args.amount > 500", {"amount": 500}, False),
    ("double_arg_int_literal", "args.amount > 500", {"amount": 500.5}, True),
    ("int_arg_double_literal", "args.amount > 500.0", {"amount": 600}, True),
    ("le_double", "args.amount <= 99.99", {"amount": 99.99}, True),
    (
        "or_currency",
        "args.amount > 500 || args.currency != 'EUR'",
        {"amount": 1, "currency": "USD"},
        True,
    ),
    (
        "or_currency_false",
        "args.amount > 500 || args.currency != 'EUR'",
        {"amount": 1, "currency": "EUR"},
        False,
    ),
    ("nested_field", "args.order.total >= 1000", {"order": {"total": 1000}}, True),
    ("in_list", "args.env in ['prod', 'staging']", {"env": "prod"}, True),
    ("exists_macro", "args.tags.exists(t, t == 'prod')", {"tags": ["a", "prod"]}, True),
    ("has_missing", "has(args.missing)", {"amount": 1}, False),
    ("operation_and_ns", "operation == 'payments.create' && ns == 'acme'", {}, True),
    # `namespace` is a reserved CEL identifier (cel-spec langdef), so it cannot name a variable.
    ("reserved_namespace_identifier", "namespace == 'acme'", {}, "error"),
    ("null_compare", "args.note == null", {"note": None}, True),
    ("bool_not_int", "args.flag == 1", {"flag": True}, False),
    ("bool_ne_int", "args.flag != 1", {"flag": True}, True),
    ("string_size", "size(args.memo) > 3", {"memo": "hello"}, True),
    ("missing_field", "args.missing > 500", {"amount": 1}, "error"),
    ("missing_nested", "args.order.total > 5", {"order": {}}, "error"),
    ("missing_absorbed_by_or", "args.missing > 500 || true", {}, True),
    (
        "missing_in_or_false",
        "args.missing > 500 || args.currency != 'EUR'",
        {"currency": "EUR"},
        "error",
    ),
    ("type_mismatch_string", "args.amount > 'x'", {"amount": 1}, "error"),
    ("type_mismatch_arg", "args.amount > 500", {"amount": "600"}, "error"),
    ("int_overflow", "args.amount * 9223372036854775807 > 0", {"amount": 2}, "error"),
    ("divide_by_zero", "args.amount / 0 > 1", {"amount": 2}, "error"),
    ("non_bool_result", "args.amount", {"amount": 1}, "error"),
    ("syntax_error", "args.amount >", {}, "error"),
    ("no_time_function", "now() > 1", {}, "error"),
    ("no_ambient_variable", "request.time > 1", {}, "error"),
    ("no_env_variable", "env.HOME == ''", {}, "error"),
]


class Candidate:
    """Uniform compile/evaluate over one implementation."""

    def __init__(self, name: str) -> None:
        self.name = name
        if name == "cel-python":
            import celpy

            env = celpy.Environment()

            def run(expr: str, args: dict[str, Any]) -> Any:
                program = env.program(env.compile(expr))
                result = program.evaluate(
                    {
                        "args": celpy.json_to_cel(args),
                        "operation": celpy.celtypes.StringType(OPERATION),
                        "namespace": celpy.celtypes.StringType(NAMESPACE),
                        "ns": celpy.celtypes.StringType(NAMESPACE),
                    }
                )
                if isinstance(result, celpy.CELEvalError):
                    raise result
                return result

        else:
            import cel

            def run(expr: str, args: dict[str, Any]) -> Any:
                program = cel.compile(expr)
                return program.execute(
                    {"args": args, "operation": OPERATION, "namespace": NAMESPACE, "ns": NAMESPACE}
                )

        self.run: Callable[[str, dict[str, Any]], Any] = run

    def outcome(self, expr: str, args: dict[str, Any]) -> tuple[bool | str, str]:
        """(True | False | "error" | "non-bool", detail)."""
        try:
            value = self.run(expr, args)
        except BaseException as error:  # noqa: B036 - a Rust panic is a BaseException
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            return "error", f"{type(error).__name__}: {str(error).splitlines()[0][:120]}"
        if type(value) is bool or type(value).__name__ == "BoolType":
            return bool(value), repr(value)
        return "non-bool", f"{type(value).__name__}: {value!r}"[:120]


def _closure(name: str) -> list[str]:
    """Installed distributions this one pulls in for the running interpreter."""
    seen: list[str] = []
    stack = [name]
    while stack:
        current = stack.pop()
        key = re.sub(r"[-_.]+", "-", current).lower()
        if key in seen:
            continue
        try:
            dist = distribution(current)
        except PackageNotFoundError:
            continue
        seen.append(key)
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue
            spec, _, marker = requirement.partition(";")
            if marker and not _marker_holds(marker):
                continue
            stack.append(re.split(r"[ <>=!~\[(]", spec.strip(), maxsplit=1)[0])
    return sorted(seen)


def _marker_holds(marker: str) -> bool:
    match = re.search(r'python_version\s*(==|<|>=)\s*"([0-9.]+)"', marker)
    if not match:
        return True
    have = tuple(sys.version_info[:2])
    want = tuple(int(part) for part in match.group(2).split("."))
    return {"==": have == want, "<": have < want, ">=": have >= want}[match.group(1)]


def footprint(name: str) -> dict[str, Any]:
    """Version, license and native extension modules across the dependency closure."""
    dists = {}
    native = []
    for key in _closure(name):
        dist = distribution(key)
        files = [str(file) for file in dist.files or []]
        extensions = [file for file in files if file.endswith((".so", ".pyd", ".dylib"))]
        dists[key] = dist.version
        if extensions:
            native.append({"distribution": key, "extension_modules": sorted(extensions)})
    meta = distribution(name).metadata
    return {
        "version": distribution(name).version,
        "license": meta.get("License-Expression")
        or meta.get("License")
        or ", ".join(c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")),
        "requires_python": meta.get("Requires-Python"),
        "dependency_closure": dists,
        "native_distributions": native,
    }


def _timed(candidate: Candidate, expr: str, args: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    result, detail = candidate.outcome(expr, args)
    return {"seconds": round(time.perf_counter() - start, 4), "result": result, "detail": detail}


def _max_depth(name: str) -> dict[str, Any]:
    """Deepest parenthesis nesting that evaluates, probed in child processes."""
    module = CANDIDATES[name]
    deepest = 0
    failure = ""
    for depth in (8, 16, 32, 48, 64, 80, 96, 128, 256, 100_000):
        src = "(" * depth + "1" + ")" * depth + " == 1"
        if module == "cel":
            code = f"import cel\nprint(cel.evaluate({src!r}))"
        else:
            code = (
                "import celpy\ne = celpy.Environment()\n"
                f"print(e.program(e.compile({src!r})).evaluate({{}}))"
            )
        run = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        if run.returncode == 0 and run.stdout.strip() in ("True", "BoolType(True)"):
            deepest = depth
            continue
        lines = [line for line in run.stderr.splitlines() if line and not line[0].isspace()]
        failure = f"depth {depth}: exit {run.returncode}, {lines[-1][:120] if lines else ''}"
        break
    return {"deepest_ok": deepest, "first_failure": failure}


def probe(name: str) -> dict[str, Any]:
    candidate = Candidate(name)
    cases = []
    for case_id, expr, args, expected in CASES:
        observed, detail = candidate.outcome(expr, args)
        # A non-bool result is acceptable only where the caller must treat it as an error.
        ok = observed == expected or (expected == "error" and observed == "non-bool")
        cases.append(
            {
                "id": case_id,
                "expression": expr,
                "expected": expected,
                "observed": observed,
                "detail": detail,
                "matches_spec": ok,
            }
        )
    # 64 KiB is the ADR-029 argument limit; 11000 small integers fill it.
    full = {"l": list(range(11_000))}
    concat = " + ".join(["args.l"] * 60) + " == []"
    cost = {
        "argument_bytes": len(json.dumps(full)),
        "exists_linear_11000": _timed(candidate, "args.l.exists(x, x < 0)", full),
        "map_filter_1000": _timed(
            candidate, "args.l.map(x, x * 2).filter(x, x > 5).size() > 0", {"l": list(range(1000))}
        ),
        "map_filter_2000": _timed(
            candidate, "args.l.map(x, x * 2).filter(x, x > 5).size() > 0", {"l": list(range(2000))}
        ),
        "exists_in_list_2000": _timed(
            candidate, "args.l.exists(x, x in args.l && x < 0)", {"l": list(range(2000))}
        ),
        "no_comprehension_60_concat_11000": {
            "expression_chars": len(concat),
            **_timed(candidate, concat, full),
        },
        "no_comprehension_60_list_eq_11000": _timed(
            candidate, " && ".join(["args.l == args.l"] * 60), full
        ),
        "nesting": _max_depth(name),
        "built_in_limits": "none exposed (no cost budget, timeout or depth option in the API)",
    }
    return {
        "footprint": footprint(name),
        "cases": cases,
        "spec_mismatches": [case["id"] for case in cases if not case["matches_spec"]],
        "cost": cost,
    }


def evidence() -> dict[str, Any]:
    os_release = ""
    try:
        with open("/etc/os-release") as handle:
            os_release = next(
                (
                    line.split("=", 1)[1].strip().strip('"')
                    for line in handle
                    if line.startswith("PRETTY_NAME=")
                ),
                "",
            )
    except OSError:
        os_release = f"{platform.system()} {platform.mac_ver()[0]}".strip()
    return {
        "environment": {
            "os": os_release,
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "candidates": {name: probe(name) for name in CANDIDATES},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    options = parser.parse_args()
    document = json.dumps(evidence(), indent=2, default=str)
    if options.out:
        with open(options.out, "w") as handle:
            handle.write(document + "\n")
    print(document)


if __name__ == "__main__":
    main()
