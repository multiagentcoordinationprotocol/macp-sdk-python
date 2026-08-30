# Source

These `cmt_hash_*.json` vectors (and `vector-schema.json`) are a manual,
point-in-time copy of:

```
multiagentcoordinationprotocol/schemas/conformance/cmt-hash/
```

from the spec repo, commit `646c3dd1ec6d2231fc8fc1dc9a570c2394bb3641`,
copied on 2026-08-29.

## Why this directory lives outside `tests/conformance/`

`tests/conformance/` is under the repo's zero-drift gate (`make
verify-fixtures`), which diffs every `*.json` file directly inside
`tests/conformance/` (non-recursive) against the spec repo's **flat**
`schemas/conformance/*.json`. The spec repo keeps these RFC-MACP-0013
vectors in a `schemas/conformance/cmt-hash/` *subdirectory*, not flat:

- Dropping them directly into `tests/conformance/` would make
  `verify-fixtures` flag them as EXTRA (no flat canonical counterpart) and
  fail the gate.
- Dropping them into a `tests/conformance/cmt-hash/` subdirectory would be
  invisible to `verify-fixtures`'s non-recursive glob — nothing would ever
  check them for drift, silently, forever.

So they live here, in `tests/vectors/cmt-hash/`, entirely outside
`tests/conformance/`, exercised instead by
`tests/unit/test_commitment_hash_vectors.py` (which *is* collected by CI,
since CI runs exactly `pytest tests/unit/ -v --cov`).

**Known cost, accepted for now:** nothing automatically diffs this copy
against the spec repo on future spec changes — it is a manual snapshot, not
a synced fixture set. If the spec repo's `cmt-hash` vectors change, this
directory must be re-copied by hand (or a future `verify-fixtures`-style
gate should be added for it).
