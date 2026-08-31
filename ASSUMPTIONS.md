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

## `apply_envelope` rolls back on failure rather than leaving a wedged dedup entry
- **Plan:** `plans/first-wins-vote-cardinality.md`
- **Assumed:** Phase 1's `message_id` dedup must not convert a recoverable error into
  permanent silent data loss. The Phase 1 verification gate found that recording the id and
  appending to `transcript` *before* the effect means a raising `_apply_mode_message` leaves
  the envelope marked-as-seen but unapplied — and every retry is then silently swallowed.
  Pre-Phase-1, a retry recovered. This bites the plan's own motivating scenario: a supervisor
  catching an exception from `Participant.run()` resubscribes at `after_sequence=0` and
  re-feeds history, and the failed envelope is permanently skipped while `transcript` claims
  it is there.
- **Chose:** On any exception after the seen-set add and the transcript append, roll **those
  two** back to their pre-call state and re-raise the original exception unchanged. This
  preserves retry-to-recover, which is the behaviour callers had before dedup existed, and
  keeps the SDK's standing "no silent-failure paths" bar.
- **Scope of the guarantee — deliberately narrower than "atomic":** the rollback covers
  `transcript` and `_seen_message_ids` only. It does **not** restore `self.phase` (assigned by
  subclasses inside `_apply_mode_message`) or any subclass collection. A retry is therefore
  safe only because every current subclass performs its single fallible operation
  (`ParseFromString`) strictly *before* any mutation, and its record types are plain
  slotted dataclasses whose construction cannot raise. That invariant is what makes the
  narrow rollback sufficient — it is not a property the base class enforces, and
  `BaseProjection` is publicly exported (`__init__.py:143`) for third parties to subclass.
  Phases 3–4 add logic inside `_apply_mode_message` and must preserve raise-before-mutate.
  (An earlier revision of this entry claimed the call becomes "as if it never happened."
  That overstated what the code delivers; corrected here after the Phase 1 re-verification
  gate flagged it.)
- **Alternatives:** (a) Accept it and document — rejected: the failure is silent, and a
  silently-skipped envelope with a `transcript` that disagrees is precisely the class of bug
  issue #43 exists to remove. (b) Record the id only after successful application, leaving
  the transcript append early — rejected: a raise would then duplicate the transcript entry
  on retry, trading one inconsistency for another. (c) Swallow and log — rejected outright;
  the caller must learn the envelope failed.
- **Blast radius if wrong:** Contained to `BaseProjection.apply_envelope`. The rollback only
  runs on an exception path that, per the accepted-history precondition, a conforming feed
  never reaches — so conforming callers see no behaviour change at all. Reverting is deleting
  a `try`/`except`/`raise`. The risk of the change is that rollback masks the original
  exception; a test asserts the exception propagates unchanged.
- **Status:** UNCONFIRMED
