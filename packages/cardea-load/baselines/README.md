# Measured references

The first M1 reference is retained with its complete campaign at [2026-09-24-m1/baseline.json](../reports/2026-09-24-m1/baseline.json). This avoids copying a baseline away from its raw evidence.

Create a new reference with the `baseline` CLI command and a new path. Do not overwrite or manually edit a reference. Compare only matching profile, harness, binary, lock and environment fingerprints. Different target application source is permitted and identified. A failed reference remains useful diagnostic evidence but cannot approve a comparison.
