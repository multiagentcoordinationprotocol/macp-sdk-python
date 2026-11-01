# Source

These `a2a_*.json` vectors (and `vector-schema.json`) are byte-identical
copies of the spec repo's

```
multiagentcoordinationprotocol/schemas/conformance/a2a/
```

taken at spec commit `2e7d3ab` on 2026-11-01 (the pack was added in SPEC #245,
Wave 6 P6.3). Refresh them with `make sync-fixtures`; `make verify-fixtures`
checks this directory for drift in both directions (it is the
`a2a:tests/vectors/a2a` entry of `FIXTURE_DIR_PAIRS`), the same way as
`tests/vectors/receipt/`. This file is exempt because both globs are `*.json`.
The spec's `README.md` for the pack is not vendored; read it there for the
conventions and for what the vectors do not pin.

`tests/unit/test_a2a.py` runs every vector through `macp_sdk.a2a`: a positive
vector's result, re-encoded as canonical JSON where it is an Envelope or an Ack,
must equal `expected` as parsed JSON (a boolean is never equal to a number),
and a reject vector must raise `MacpA2aError` with the vector's `layer` and
`code`.
