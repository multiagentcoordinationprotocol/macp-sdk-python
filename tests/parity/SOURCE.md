# Source

`contract.json` is a point-in-time copy of:

```
schemas/parity/contract.json
```

from the spec repo (`multiagentcoordinationprotocol/multiagentcoordinationprotocol`),
commit [`99756f897cace4ae3f65ab3bdf7bdadcc6e4f13e`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/commit/99756f897cace4ae3f65ab3bdf7bdadcc6e4f13e)
(spec-repo PR [#154](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/pull/154),
bumping `contract_version` to `1.1.1`), first vendored into this repo 2026-09-28 as
part of issue #93 item 1 -- this repo had no vendored copy at all before this, unlike
`macp-runtime` and `macp-sdk-typescript`, both of which already vendor and assert
against this manifest.

## Why this directory lives outside `tests/conformance/`

Same reasoning as `tests/vectors/cmt-hash/SOURCE.md`: `tests/conformance/` is covered
by the flat, non-recursive `verify-fixtures` gate, which diffs every `*.json` directly
inside `tests/conformance/` against the spec repo's flat `schemas/conformance/*.json`.
`contract.json` lives at `schemas/parity/contract.json` in the spec repo -- a different
top-level directory entirely, not a subdirectory of `schemas/conformance/`. Folding it
into `tests/conformance/` would make `verify-fixtures` flag it `EXTRA:` (no flat
canonical counterpart under `schemas/conformance/`). So it gets its own directory,
`tests/parity/`, gated by its own `Makefile` targets (`sync-parity`/`verify-parity`)
mirroring `sync-fixtures`/`verify-fixtures` but scoped to this one file.

## How the copy is kept honest

`make verify-parity` (`Makefile`) does a single-file `diff -q` against
`$(SPEC_PARITY_DIR)/contract.json` -- a canonical file that differs from (or is
missing against) the vendored copy fails the gate with a clear error, the same guard
style as `verify-fixtures`. `make sync-parity` refreshes the vendored copy from
canonical in one step. CI runs the gate on every push to `main` and every PR via
`.github/workflows/conformance-fixtures.yml`, which already checks the spec repo out
to `_spec` for `verify-fixtures` and adds one more step reusing that same checkout.

`tests/parity/test_contract.py` asserts this SDK's actual runtime values against every
section of the vendored manifest whose `applies_to` names `macp-sdk-python` -- see that
file's own header for the full list. This is a parity-specific test; it does not
replace the hand-picked unit tests in `tests/unit/test_base_projection.py` or the
commitment-hash vectors under `tests/vectors/cmt-hash/`.

**Do not hand-edit `contract.json`.** Refresh it with `make sync-parity` and commit the
result, then re-run `make verify-parity` to confirm zero drift.

## Non-normative

`contract.json` is explicitly non-normative -- see its own `$comment` field and
`schemas/parity/README.md` in the spec repo. Each section names its real normative
home (an RFC, a registry, a proto file) or says plainly that none exists yet. This
SDK's own docs must never cite `contract.json` itself as a source of truth; cite the
named RFC/registry/proto instead.

## Open items (from the spec repo's README, not resolved by this SDK)

- `contribute_payload` -- what a decoder does with valid legacy JSON whose `value` is
  not a string remains genuinely unpinned; no two of the three implementations agree.
  Tracked upstream as spec-repo issue #142.
- `contribute_acceptance` (`applies_to: [macp-runtime]` only) -- settled, not pending an
  upstream bump. This SDK's decode layer is observational, not an acceptance gate;
  `macp-runtime` alone rejects an empty `Contribute` payload.
- `projection_anomaly.kinds` -- whether a discarded competing `TaskAccept` or an
  already-settled `Handoff` message should also record an anomaly is the two SDKs' to
  settle (issue #94). This repo has since adopted `duplicate_task_accept` and
  `settled_handoff` unilaterally (see `src/macp_sdk/base_projection.py`), pending
  `macp-sdk-typescript` landing a matching kind -- **do not add either to the assertion
  in `test_contract.py` until the manifest itself lists them**; asserting ahead of the
  manifest would defeat the point of a manifest that "mirrors, never originates"
  agreement.
