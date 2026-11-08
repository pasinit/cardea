# CEL fit experiment (ADR-036 slice 1)

Runs the same approval-rule cases against the two shortlisted Python CEL implementations, `cel-python==0.5.0` and `common-expression-language==0.10.0`, and records what each one does. It is an observation harness, not Cardea's evaluator; that lives in `apps/api/src/cardea/service/approvals.py`. See [ADR-036](../../docs/decisions/036-approval-waits.md) and the recorded [evidence](../../docs/decisions/cel-fit-evidence-2026-11-08.json).

`fit.py` covers:

- threshold comparisons on JSON arguments (int, double, mixed), boolean operators, nested fields, `in`, `has`, null and bool/int equality;
- errors that must surface as errors so Cardea can fail closed: missing fields, type mismatches, overflow, division by zero, a non-boolean result, a syntax error, the reserved identifier `namespace`, and references to time, environment or request state that the activation does not provide;
- cost: linear and accumulating comprehensions, a comprehension-free near-worst case over the 64 KiB argument limit, and the deepest parenthesis nesting each parser accepts (probed in child processes);
- footprint: version, license, the dependency closure and which distributions ship native extension modules.

## Reproduce

```sh
make cel-fit                                   # pytest on the host Python (3.12 or later)
cd experiments/cel && uv run --locked python -I fit.py --out /tmp/cel-fit.json
```

Debian 13 (system Python 3.13 and a uv-managed CPython 3.12) in a disposable container:

```sh
mkdir -p /tmp/cel-out
docker run --rm -v "$PWD/experiments/cel:/fit:ro" -v /tmp/cel-out:/out debian:13 sh /fit/debian.sh
docker run --rm --platform linux/amd64 -v "$PWD/experiments/cel:/fit:ro" -v /tmp/cel-out:/out \
  debian:13 sh /fit/debian.sh
```

Passing tests mean the recorded observations still hold, including the deviations recorded for `cel-python`. Timings from an emulated `linux/amd64` container are not native.
