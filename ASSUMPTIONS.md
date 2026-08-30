# ASSUMPTIONS

Decisions taken during implementation that were not pinned by the issue or an
existing convention. Reconciled via `/reconcile`.

## Declarative `FIXTURE_DIR_PAIRS` instead of a copy-pasted second loop pair
- **Plan:** `plans/gate-cmt-hash-vectors.md`
- **Assumed:** Issue #38's "extend `verify-fixtures` with a second pair of loops" describes
  the required *behaviour*, not a required *implementation shape*.
- **Chose:** One Make variable (`.:tests/conformance cmt-hash:tests/vectors/cmt-hash`)
  iterated by both `sync-fixtures` and `verify-fixtures`. Removes ~20 lines of duplicated
  shell and makes a future vector subdirectory a one-token edit, instead of a third and
  fourth hand-maintained loop that can drift apart.
- **Alternatives:** Literal copy-paste of the two loops (what the issue text says);
  a standalone Python/shell script invoked by the target (more testable in isolation, but
  moves the gate out of the Makefile where every other repo command lives).
- **Blast radius if wrong:** Contained to the Makefile. Reverting to duplicated loops is a
  mechanical ~20-line edit; the 17 tests pin the behaviour either way.

## `MISSING` is a distinct failure class, not `DRIFT`
- **Plan:** `plans/gate-cmt-hash-vectors.md`
- **Assumed:** A canonical directory that does not exist is an operator problem, not a
  fixture-content problem.
- **Chose:** A separate `MISSING:` line that accumulates (does not abort the pair loop, so
  it cannot mask drift in another pair) plus a closing note that `make sync-fixtures`
  cannot fix it. `sync-fixtures` hard-fails on the same condition in a pre-flight pass,
  before copying anything.
- **Alternatives:** Report it as `DRIFT` — rejected because `DRIFT`'s stated remedy is
  `make sync-fixtures`, which cannot create a nonexistent source and would itself fail,
  looping the developer.
- **Blast radius if wrong:** `make verify-fixtures` can no longer run green against a spec
  checkout predating `schemas/conformance/cmt-hash/` (spec commit `646c3dd`). CI pins the
  spec repo's default branch, which has it. Escape hatch is deleting the pair entry.

## The gate's own tests live in `tests/unit/` and shell out to `make`
- **Plan:** `plans/gate-cmt-hash-vectors.md`
- **Assumed:** A subprocess-driving test is acceptable in `tests/unit/` despite crossing a
  process boundary.
- **Chose:** `tests/unit/test_fixture_drift_gate.py`, synthesizing *both* the canonical and
  the vendored trees in tmpdirs so it never needs the real spec repo. Placed under
  `tests/unit/` because CI runs exactly `pytest tests/unit/ -v --cov`; `tests/integration/`
  self-skips without a running runtime and `tests/conformance/` is marker-gated, so the
  gate would go unexercised in either.
- **Alternatives:** A new `fixtures` pytest marker (needs a `pyproject.toml` marker
  registration under `--strict-markers`, and a new CI invocation to actually run it).
- **Blast radius if wrong:** Adds ~0.5s and a `make` dependency to the unit suite. Guarded
  by `skipif(shutil.which("make") is None)`. Coverage gate is unaffected —
  `[tool.coverage.run] source = ["macp_sdk"]` excludes test modules.
