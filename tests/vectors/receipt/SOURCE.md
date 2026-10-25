# Source

These `receipt_*.json` vectors (and `vector-schema.json`) are byte-identical
copies of the spec repo's

```
multiagentcoordinationprotocol/schemas/conformance/receipt/
```

taken at spec commit `10ca480` on 2026-10-25 (the pack was added in SPEC #217).
Refresh them with `make sync-fixtures`; `make verify-fixtures` checks this
directory for drift in both directions (it is the `receipt:tests/vectors/receipt`
entry of `FIXTURE_DIR_PAIRS`), the same way as `tests/vectors/cmt-hash/` (see
its `SOURCE.md`). This file is exempt because both globs are `*.json`.

`tests/unit/test_receipt.py` replays every vector through `verify_receipt` and
asserts the result equals the vector's `expected`. `receipt_001_attested` is the
Appendix A.5 example (`examples/discovery/decision_receipt.json` in the spec
repo, unchanged), so rebuilding it with the A.5 key (seed
`SHA-256("macp-receipt example key")`, kid `example-1`) is the round-trip check.
