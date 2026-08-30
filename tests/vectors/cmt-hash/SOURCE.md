# Source

These `cmt_hash_*.json` vectors (and `vector-schema.json`) were originally
copied, point-in-time, from:

```
multiagentcoordinationprotocol/schemas/conformance/cmt-hash/
```

in the spec repo, commit `646c3dd1ec6d2231fc8fc1dc9a570c2394bb3641`, on
2026-08-29. That commit and date record the provenance of the initial
import; subsequent refreshes go through `make sync-fixtures` rather than a
hand copy.

## Why this directory lives outside `tests/conformance/`

`tests/conformance/` is under the repo's zero-drift gate (`make
verify-fixtures`), which now diffs each entry of `FIXTURE_DIR_PAIRS` (a
canonical-subpath-to-local-dir pair list in the `Makefile`) against its
canonical counterpart, in both directions. The flat top level of
`SPEC_CONFORMANCE_DIR` is paired with `tests/conformance/`; the spec repo's
`schemas/conformance/cmt-hash/` subdirectory is paired separately with this
directory, `tests/vectors/cmt-hash/`. The spec repo keeps these
RFC-MACP-0013 vectors in that `cmt-hash/` subdirectory, not flat, so dropping
them directly into `tests/conformance/` would still make `verify-fixtures`
flag them as EXTRA against the flat pair (no flat canonical counterpart) and
fail the gate.

So they live here, in `tests/vectors/cmt-hash/`, entirely outside
`tests/conformance/`, exercised instead by
`tests/unit/test_commitment_hash_vectors.py` (which *is* collected by CI,
since CI runs exactly `pytest tests/unit/ -v --cov`).

**Gated by `make verify-fixtures`:** this directory is checked for drift
against `$(SPEC_CONFORMANCE_DIR)/cmt-hash/` in both directions —
canonical-but-missing-or-different-locally is `DRIFT`, local-but-no-canonical
is `EXTRA` — on every push to `main` and every PR targeting `main`, via
`.github/workflows/conformance-fixtures.yml`. `make sync-fixtures` refreshes
this directory from canonical alongside `tests/conformance/`; sync copies
but never deletes, so a file flagged `EXTRA` must be removed by hand. This
`SOURCE.md` itself is exempt from both checks because both globs are
`*.json`. This closes the gap that used to be tracked here as a known,
accepted cost — see issue #38.
