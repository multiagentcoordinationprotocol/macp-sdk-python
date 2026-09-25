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
  narrow rollback sufficient — it is not a property the base class *enforces* (it cannot:
  a slotted subclass has no `__dict__` to snapshot, and deep-copying every projection per
  envelope is a real per-message cost), and `BaseProjection` is publicly exported
  (`__init__.py:151`) for third parties to subclass.
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
- **Reconciled 2026-09-06 (Fable):** CONFIRM as-is. The invariant was re-verified per
  branch against the *current* tree, not the tree it was written against — the first-wins
  logic from #47/#48 landed inside these very methods afterwards and did preserve
  raise-before-mutate (`projections.py:100-127` parses at :102, and every mutation --
  `:112`'s anomaly append, `:121`, `:127` -- follows it; `quorum.py:102`'s `setdefault`
  precedes the duplicate check but nothing fallible follows it). Two prose defects in this
  entry were corrected: `__init__.py:143` → `:151`, and `task.py:103`/`handoff.py:71` →
  `:102`/`:70` in `base_projection.py`'s rollback comment.
  One claim was **withdrawn as overstated** — that the docstring advertises a rollback the
  base class does not deliver. It does not; `base_projection.py:145-151` states the narrow
  scope explicitly, and docstring and code agree.
- **Follow-ups applied in the same pass (additive, no contract change):** the
  raise-before-mutate requirement now lives on the `_apply_mode_message` abstract docstring
  — the one place a subclasser is guaranteed to look — instead of only in an internal
  comment; and `tests/unit/test_projection_rollback_invariant.py` pins the
  parse-then-mutate ordering across all 23 dispatch arms plus seeded Decision/Quorum
  cases. That is narrower than the full contract: it feeds a payload that cannot parse,
  so it catches a mutation moved ahead of `ParseFromString`, not one moved ahead of some
  *other* fallible call. The docstring states the broader rule. Both guards were
  mutation-tested: moving a mutation ahead of the parse fails `decision-Vote`, and
  dropping a row from the branch table fails the exhaustiveness check, which discovers
  SDK projections rather than listing them so a new projection cannot arrive unpinned.
- **Known gap, accepted:** `_record_anomaly` appends to `self.anomalies` then logs. An
  exception raised by a user-installed logging *filter* on the `macp_sdk` logger would
  propagate and leave the anomaly record behind (base-class state, not rolled back).
  Unreachable without a raising filter; not worth code.
- **Status:** CONFIRMED (2026-09-06)

## Contribute payload canonicality tie-break (option A vs. B)
- **Plan:** `plans/contribute-payload-decode-non-canonical-inputs.md` (Phase 1)
- **Assumed:** The plan's Open question 1b flagged this as the real open decision, recommending
  option B (an `isinstance(dict)` guard plus a separate `_is_canonical_proto` tie-break) over
  option A (bare `isinstance(dict)`), and asked `/implement` to log the choice as `UNCONFIRMED`.
- **Chose:** A variant of B, simpler than the plan's literal snippet: `_is_canonical_proto` is
  called unconditionally after any successful `json.loads` (not gated behind a prior
  `isinstance(dict)` check), and only its result decides proto-vs-JSON. This closes the same
  forward-direction residual set as the plan's B (verified: zero corrupting lengths 1-127 across
  five value shapes) while also making the `transports.py` non-dict-JSON crash risk (plan's
  Phase 1 Edge cases / criterion 6) moot by construction — a non-dict JSON parse that isn't
  canonical proto still returns the existing `{"encoding": "json", "json": parsed}` wrapper, so
  `_envelope_to_message` never raises on `b"null"`/`b"0"`/`b'"x"'`/`b"[]"`/`b"true"`. Also added
  `msg.DiscardUnknownFields()` inside `_is_canonical_proto` before the round-trip comparison —
  found necessary during verification (not anticipated by the plan): without it, Python's
  protobuf runtime's default unknown-field preservation through `ParseFromString`/
  `SerializeToString` made the check misclassify several whitespace-prefixed legacy-JSON
  payloads as canonical proto (a false-positive/data-corruption class the plan's Approach section
  never considered, since it only reasoned about the forward direction).
- **Alternatives:** Option A alone (rejected — plan itself found this leaves lengths 9, 10, 13,
  32, 123 corrupting, reachable by ordinary values). The plan's literal B snippet with a separate
  `isinstance(dict)` branch (rejected as unnecessary extra surface — the unified check is
  strictly simpler and was verified to cover the same cases, including cases B's literal form
  does not by itself address, i.e. the `transports.py` crash risk).
- **Blast radius if wrong:** Contained to `_decode_json_first_then_proto` /
  `_is_canonical_proto` in `src/macp_sdk/proto_registry.py`. Reverting to the plan's literal B
  snippet (adding back a separate `isinstance(dict)` branch) is a small, mechanical diff — no
  test depends on the specific branch structure, only on the observable decode results, which
  are exhaustively pinned in `tests/unit/test_proto_registry.py::TestMultiRoundContribute`. One
  narrow residual survives regardless of A/B: a payload whose first byte is a literal `0x0A` and
  whose remainder forms a complete, well-formed proto field-1 string is symmetrically ambiguous
  and cannot be closed by `DiscardUnknownFields` (nothing unknown to discard) — documented and
  pinned by `test_newline_prefixed_legacy_json_collision_is_a_documented_residual`, not previously
  named in the plan's known-residual list.
- **Status:** UNCONFIRMED
