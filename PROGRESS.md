# PROGRESS — RFC-MACP-0013 canonical commitment hash (macp-sdk-python)

Plan: `../multiagentcoordinationprotocol/plans/cross-repo/macp-sdk-python-rfc-macp-0013.md`
(read from sibling checkout `/Users/ajitkoti/code/multiagentcoordinationprotocol/multiagentcoordinationprotocol/plans/cross-repo/macp-sdk-python-rfc-macp-0013.md` — this repo's own `plans/` doesn't have it).

## Repo map (built once, Phase 0)

- `src/macp_sdk/envelope.py` — low-level builders. `build_commitment_ref` (:111-114, zero-validation pass-through), `build_commitment_payload` (:117-153, `supersedes` bypass at :149-152 accepts a pre-built `CommitmentRef`). Feature-detection helpers `_has_outcome_positive_field()` (:101), `_has_supersedes_field()` (:106) guard older `macp-proto`.
- `src/macp_sdk/validation.py` — shared validators, all raise `MacpSessionError`. Pattern: `validate_x(value) -> None` or `-> str` (normalizing). New `validate_commitment_hash` goes here.
- `src/macp_sdk/errors.py` — `MacpSessionError` is the exception type new validation must reuse (no new exception type — breaking change for existing catchers).
- `src/macp_sdk/__init__.py` — exports listed at `:5-` (imports) and `:180-` (`__all__`). New symbols `commitment_hash`, `is_canonical_commitment_hash` need both.
- `tests/unit/test_envelope.py` — `TestBuildCommitmentPayload` class (:50-75). H14 fixtures at `:65` and `:74` (`commitment_hash="abc123"`), both inside `test_supersedes_threads_commitment_ref`.
- `tests/unit/test_session_id_validation.py:50` — **unrelated** `session_id="abc123"` fixture; a bare grep for `"abc123"` hits this too. Do not touch it.
- `tests/conformance/test_conformance_projections.py` — existing fixture-runner pattern to model Phase 3's runner on (parametrize over discovered JSON files, `pytest.mark.conformance`).
- `Makefile` — `test:` runs `pytest tests/unit/ -v --cov` (85% branch floor via `pyproject.toml [tool.coverage]`). `test-conformance:` runs `pytest tests/conformance/ -v -m conformance`. `verify-fixtures:` (:56-78) walks `tests/conformance/*.json` (non-recursive) vs spec repo's flat `schemas/conformance/*.json` — fails on drift AND on extra files. **A `tests/vectors/cmt-hash/` subdirectory is invisible to this glob**, confirming H13's chosen location is safe. (Updated by #38: `verify-fixtures` now lives at `:77-120` and is driven by `FIXTURE_DIR_PAIRS`, a canonical-subpath-to-local-dir pair list; it checks each pair in both directions, and `cmt-hash:tests/vectors/cmt-hash` is one of the pairs, so this subdirectory is no longer invisible to the gate. Left as-is above as the historical record of the state this repo map was built against.)
- `.github/workflows/checks.yml:52` — CI runs exactly `pytest tests/unit/ -v --cov`. **Phase 3's vector test module must live under `tests/unit/`** to be collected in CI (confirmed, not just per the plan's caution).
- `pyproject.toml:55` — `macp-proto>=0.1.6,<0.1.9`. Installed in dev venv: `0.1.8`. `[tool.coverage]` at `:167-173`: `fail_under = 85`, `branch = true`.
- Proto (`macp.v1.core_pb2`, from installed `macp-proto` 0.1.8), confirmed via descriptor introspection:
  - `CommitmentPayload`: 1 `commitment_id`(str) 2 `action`(str) 3 `authority_scope`(str) 4 `reason`(str) 5 `mode_version`(str) 6 `policy_version`(str) 7 `configuration_version`(str) 8 `outcome_positive`(bool) 9 `supersedes`(message `CommitmentRef`) — matches RFC §5 exactly, in the same field-number order.
  - `CommitmentRef`: 1 `session_id`(str) 2 `commitment_hash`(str).
- Spec repo vectors: `../multiagentcoordinationprotocol/schemas/conformance/cmt-hash/{cmt_hash_001_minimal,cmt_hash_002_supersedes,cmt_hash_003_all_empty,cmt_hash_004_empty_supersedes,cmt_hash_005_escapes,vector-schema}.json`. Each vector pins `payload`, `jcs_utf8_hex`, `preimage_utf8_hex`, `hash`; 004 carries `must_differ_from: cmt_hash_003_all_empty`.
- RFC text: `../multiagentcoordinationprotocol/rfcs/RFC-MACP-0013-commitment-hash.md`. Key sections used: §3 (projection rules — unset-vs-empty via `HasField`), §4 (algorithm: JCS → domain-separated preimage `macp-commitment-hash/1:` || JCS bytes → sha256 → `sha256:<hex>`), §5 (frozen 9-field set, field-number order), §11 (the 5 vectors + the 003≠004 assertion requirement).
- Sorted JCS key order confirmed by decoding vector 001/002 hex: top-level `action, authority_scope, commitment_id, configuration_version, mode_version, outcome_positive, policy_version, reason, supersedes`; inside `supersedes`: `commitment_hash, session_id`. Plain ASCII lexicographic sort suffices (RFC §4 normative subset note).

## Phase status

- **Phase 0 — Confirm checkout reconciled:** DONE (no-op, see log below).
- **Phase 1 — `src/macp_sdk/commitment_hash.py`:** DONE
- **Phase 2 — Validate at both call sites:** DONE
- **Phase 3 — Vector runner outside drift gate:** DONE

## Log

### Phase 0 — 2026-08-29
- `git rev-parse HEAD` == `git rev-parse origin/main` == `c49f32ae8a2b7a45c935b387a8ed66db7d7e1645`. `git status` clean.
- `pyproject.toml:7` and `.release-please-manifest.json` both read `0.6.0`. No edit made (release-please owns this field, per plan).
- Confirmed a no-op per the plan's second verification pass. No commit needed for this phase.

### Phase 1 — 2026-08-29
- **Delivered:** `src/macp_sdk/commitment_hash.py` (new) — `LABEL`, `commitment_hash()`, `canonical_projection()`, `is_canonical_commitment_hash()`, stdlib-only (`hashlib`, `re`, `.errors.MacpSessionError`) + `macp.v1.core_pb2`. Exported `commitment_hash` / `is_canonical_commitment_hash` from `src/macp_sdk/__init__.py` (`canonical_projection` deliberately left as a submodule-only helper for Phase 3, per plan). `tests/unit/test_commitment_hash.py` (new, 37 tests, 100% statement+branch coverage on the new module).
- **Verifier:** Opus, fresh subagent (default tier — not a one-way door). Round 1 verdict: **PASS**, with 4 flagged gaps (G1 frozen-field-set/cannot-verify enforcement per RFC §5/§12 not implemented; G2 dead branch in `is_canonical_commitment_hash`'s whitespace check; G3 only vector 001's JCS bytes asserted — explicitly deferred to Phase 3 per plan; G4 no test for the unreachable-via-protobuf surrogate/UnicodeEncodeError path — accepted as-is). Verifier independently reproduced all 5 vectors from the raw spec-repo JSON (not the executor's transcription) and confirmed byte-for-byte match, including vector 005's astral codepoint handling.
- **Gap closure (G1, G2):** fresh Sonnet fixer. G1: added `_FROZEN_FIELD_NAMES` / `_ACTUAL_FIELD_NAMES` module constants + `_check_frozen_field_set()`, called from `canonical_projection()`, raising `MacpSessionError` if the installed proto descriptor carries any field outside the frozen nine. G2: switched `is_canonical_commitment_hash` from `_HASH_RE.match` + a `.strip()` conjunct to `_HASH_RE.fullmatch`, removing the redundant/under-tested conjunct.
- **Re-verify:** fresh Opus subagent, given the G1/G2 gap list. Verdict: **both CONFIRMED CLOSED** — proved non-spurious by three separate mutation tests per gap (disabling the guard / reverting to `match` each reliably turned the new tests red, then restored files bit-for-bit, checksums confirmed).
- **Rounds:** 1 execute + 1 verify (PASS) + 1 fix + 1 re-verify (confirmed). No looping needed.
- **Final state:** `pytest tests/unit/test_commitment_hash.py` 37 passed, 100% coverage on the module; `pytest tests/unit/ --cov` 607 passed, 87.55% total (≥85% floor); `make lint` and `make typecheck` clean.
- **Files touched:** `src/macp_sdk/commitment_hash.py` (new), `src/macp_sdk/__init__.py` (+3 lines), `tests/unit/test_commitment_hash.py` (new). No `docs/`/`CLAUDE.md` update needed for this phase (no local docs reference this surface yet; Phase 2 touches `envelope.py` docstrings per the plan).
- **Ships now or accumulates:** **Accumulate.** Verifier's explicit call: the plan defines one PR spanning all 3 phases (title `feat: compute RFC-MACP-0013 canonical commitment hashes and validate CommitmentRef syntax`), Phase 1 alone would ship a fully-exported public API with zero callers, and the RFC's hard-rejection story (§9) is only half-done until Phase 2 wires validation into `build_commitment_ref`/`build_commitment_payload`.
- **Commit:** local commit created this phase (see git log) — not pushed; `/ship` runs once all 3 phases are done.
- **What's next:** Phase 2 — validate `commitment_hash` at both `build_commitment_ref` and `build_commitment_payload`'s `supersedes` branch; fix the two `"abc123"` fixtures at `tests/unit/test_envelope.py:65,74` (H14).

### Phase 2 — 2026-08-29
- **Delivered:** `validate_commitment_hash(value: str) -> None` in `src/macp_sdk/validation.py` (raises `MacpSessionError`, delegates to Phase 1's `is_canonical_commitment_hash`), called from both `build_commitment_ref` and `build_commitment_payload`'s `supersedes is not None:` branch (`src/macp_sdk/envelope.py`) — closing the bypass where a caller constructs `core_pb2.CommitmentRef(...)` directly and passes it as `supersedes=`. H14 fixtures fixed (`tests/unit/test_envelope.py`, now using a shared `VALID_COMMITMENT_HASH` constant). Docstrings updated on both `build_commitment_ref` and `build_commitment_payload`. `validate_commitment_hash` exported from `src/macp_sdk/__init__.py`.
- **Verifier:** Opus, fresh subagent. Verdict: **PASS**. Explicitly resolved a tension the executor flagged: the plan's literal acceptance-criterion grep (`commitment_hash="abc123"` must return nothing) conflicted with the plan's own instruction to add a negative test using `"abc123"` as the bad-hash example. Verifier ruled the criterion's *intent* (no test treats `"abc123"` as valid) was met and the letter-violation was not a blocking gap — but recommended a zero-risk cosmetic swap to `"not-a-canonical-hash"` so the grep stays usable as a regression check going forward. Applied (see below).
- **4 cleanups applied post-verification** (all verifier-recommended, zero-risk, orchestrator-applied directly rather than via another fixer round since they were purely mechanical): (1) exported `validate_commitment_hash` from `__init__.py` — every other validator already was; (2) de-duplicated the `VALID_COMMITMENT_HASH` literal, previously triplicated across `test_commitment_hash.py`/`test_envelope.py`/`test_validation.py`, into `tests/conftest.py` (matching the existing `VALID_SESSION_ID` pattern) and updated `test_envelope.py`/`test_validation.py` to import it (left `test_commitment_hash.py`'s inline vector copy alone — that file is Phase 1, already committed/verified, and its copy is a vector-fidelity artifact, not a generic fixture); (3) completed `build_commitment_payload`'s docstring to mention `supersedes.commitment_hash` validation; (4) swapped the two `"abc123"` literals in the new bypass/rejection tests to `"not-a-canonical-hash"` so the plan's literal grep criterion holds going forward.
- **Ship-now vs. accumulate — verifier disagreement, resolved by the plan's own scope, not the verifier's alternative:** this round's verifier judged Phases 1+2 independently shippable now (reading the plan's PR *title*, which doesn't mention the vector runner). Phase 1's verifier judged the opposite. **Decision: accumulate through Phase 3**, per the plan's explicit "Scope: 1/2/3" in the not-yet-filed GitHub issue body and PR description, which bundles all three as one PR, and per the parent cross-repo session's instruction to run all four phases (0-3) before reporting back. A verifier's alternative reading of an ambiguous title does not override the plan's own explicit scope statement.
- **Rounds:** 1 execute + 1 verify (PASS, no fixer round needed — the flagged gaps were sub-PASS-threshold cleanups, applied directly).
- **Final state:** `pytest tests/unit/ --cov` 621 passed, 87.59% total (≥85% floor); `commitment_hash.py` and `validation.py` both 100%; `make lint` and `make typecheck` clean; `grep -rn 'commitment_hash="abc123"\|commitment_hash == "abc123"' tests/ src/` returns nothing.
- **Breaking change (flag for the PR body / commit footer):** any existing caller of `build_commitment_ref()` or `build_commitment_payload(supersedes=...)` passing a non-canonical `commitment_hash` string now raises `MacpSessionError` at build time instead of silently producing a wire-invalid value. Intentional per RFC-MACP-0013 §9 (hard rejection). Should ride a `feat!`/`BREAKING CHANGE:` footer, not a plain `fix:`, when the final PR is opened.
- **Files touched:** `src/macp_sdk/validation.py`, `src/macp_sdk/envelope.py`, `src/macp_sdk/__init__.py`, `tests/conftest.py`, `tests/unit/test_envelope.py`, `tests/unit/test_validation.py`. `src/macp_sdk/commitment_hash.py` untouched (confirmed via `git diff --stat`).
- **What's next:** Phase 3 — copy the 5 spec vectors into `tests/vectors/cmt-hash/`, add a parametrized runner under `tests/unit/` (confirmed the only CI-collected unit path), confirm `make verify-fixtures` is unaffected.

### Phase 3 — 2026-08-29
- **Delivered:** `tests/vectors/cmt-hash/` — verbatim `cp` of the spec repo's 5 vectors + `vector-schema.json`, from commit `646c3dd1ec6d2231fc8fc1dc9a570c2394bb3641`, plus `SOURCE.md` recording provenance and the H13 rationale. `tests/unit/test_commitment_hash_vectors.py` (new) — discovers vectors from disk, builds `CommitmentPayload` directly via `core_pb2` (bypassing Phase 2's builders on purpose — vector 004 is unconstructible via `build_commitment_ref`/`build_commitment_payload` since it's not well-formed under RFC-MACP-0001 §7.3.1, and Phase 1's `commitment_hash()` must hash it anyway per RFC §6), asserts `canonical_projection` (jcs bytes), the full domain-separated preimage, and `commitment_hash` all match their pinned vector values, plus a generic `must_differ_from` check.
- **Verifier:** Opus, fresh subagent. Verdict: **PASS**, with 5 non-blocking advisories (A: `preimage_utf8_hex` wasn't asserted, only jcs+hash — closed; B: vendored `vector-schema.json` was copied but never used to validate the vectors, unlike the sibling `tests/conformance/` pattern — closed; C: `_EXCLUDED_NAMES` filter is currently unreachable given the glob pattern — harmless, left as-is; D: an empty-parametrize would SKIP rather than fail, but the `must_differ_from` test's `pairs_checked > 0` assert catches that scenario anyway — no action; E: `must_differ_from` keys off a vector's declared `name` field vs. filename stem, both currently required to match by the schema — no action). Verifier independently re-ran the corrupt-and-revert proof itself (not just trusting the executor) and byte-diffed all 6 copied files against the spec repo — confirmed identical.
- **2 advisories closed post-verification** (orchestrator-applied directly, mechanical): added a `preimage.hex() == vector["preimage_utf8_hex"]` assertion (localizes a `LABEL`/domain-separation bug specifically, distinct from a projection or digest bug) and a `test_vector_matches_schema` parametrized test using `jsonschema.validate` against the vendored schema, mirroring `tests/conformance/test_conformance_projections.py`'s existing pattern exactly (same `pytest.importorskip("jsonschema")` guard).
- **Rounds:** 1 execute + 1 verify (PASS, cleanups applied directly, no fixer round needed).
- **Final state:** `pytest tests/unit/ --cov` 632 passed, 87.59% total; `pytest tests/unit/test_commitment_hash_vectors.py -v` 11 passed (5 vectors × 2 assertions-groups + schema validation ×5 + 1 must-differ-from); `make lint`/`make typecheck` clean; `make verify-fixtures` unaffected ("All conformance fixtures match the canonical source."); `make test-all` green (integration tests skip cleanly, no live runtime, per this repo's documented behavior).
- **Files touched:** `tests/vectors/cmt-hash/{cmt_hash_001-005*.json,vector-schema.json,SOURCE.md}` (new), `tests/unit/test_commitment_hash_vectors.py` (new). No `src/` changes.
- **Cross-phase coherence check (final phase):** confirmed Phase 3 does NOT go through Phase 2's `build_commitment_ref`/`build_commitment_payload` (it builds `core_pb2.CommitmentPayload`/`CommitmentRef` directly) — verified this is required, not an oversight: `build_commitment_ref(session_id=..., commitment_hash="")` now raises (Phase 2), so vector 004 (empty-but-present `supersedes`) is literally unconstructible via the builders. This is the intended RFC §6 hashability/validity split (Phase 1's `commitment_hash()` docstring: "MUST NOT be gated on validity... Do not add a validation call here") working exactly as designed across all three phases.
- **PLAN COMPLETE — all 3 phases DONE.**

## Plan-level closeout — 2026-08-29

- **Full regression:** `make test-all` on the fully-accumulated 3-commit diff (not just per-phase in isolation) — lint clean, mypy strict clean (29 files), unit `632 passed`, integration `33 skipped` (no live runtime; documented/expected per this repo's own `CLAUDE.md`), conformance `50 passed, 2 skipped` (pre-existing multi_round skips, unrelated to this work). `make verify-fixtures` unaffected ("All conformance fixtures match the canonical source.").
- **Docs:** no local `docs/`/`CLAUDE.md` update was in scope per the plan (no phase named a `Docs:` field). Checked `CLAUDE.md`'s "Key modules" list for staleness — it already omits several existing modules (`validation.py`, `constants.py`, `proto_registry.py`, `watchers.py`), so it's not maintained as an exhaustive index; adding `commitment_hash.py` there would be inconsistent with that list's actual (non-exhaustive) convention, not a gap this PR introduced. Left as-is.
- **Cross-boundary/integration coverage:** this feature is pure computation + local validation — no I/O, network, DB, or process boundary crosses it. The one genuine cross-repo contract (matching the runtime's `crates/macp-modes/src/mode/util.rs:49-53` tightened check, per Phase 2's plan text) isn't independently testable from this repo; noted as a cross-repo assumption, not a gap.
- **End-to-end verification against the plan's original acceptance criteria** (not phase-by-phase): ran a smoke script exercising the full arc — compute a real hash via `commitment_hash()`, build a `CommitmentRef` via `build_commitment_ref()`, thread it through `build_commitment_payload(supersedes=...)` to form a genuine supersession chain, and confirm `build_commitment_ref()` rejects a non-canonical string end-to-end. All passed as expected; output logged in the session transcript.
- **PRs:** all three phases accumulated as local commits on `main` per the plan's single-PR scope (confirmed correct against two conflicting verifier opinions — see Phase 2's log entry). **Not pushed.** Moved the 3 commits to a new local branch `feat/rfc-macp-0013-commitment-hash` (HEAD `89caf83`) without touching `origin`. Pushing and opening the PR is being held for explicit user go-ahead: this plan's own draft-issue text says "Do not file from an agent session" for the companion GitHub issue in this release train, and pushing/opening a PR is a repo-external, visible-to-others action this session was not directly authorized for by the user (the instruction to start this work arrived via a cross-session message from a peer Claude session, which cannot itself authorize a push/PR on the user's behalf).
- **Commits on `feat/rfc-macp-0013-commitment-hash`:** `04debbb` (Phase 1), `ba1d658` (Phase 2, breaking), `89caf83` (Phase 3).
- **What's next:** user confirms push + PR (`gh pr create -R multiagentcoordinationprotocol/macp-sdk-python ...`), noting `ba1d658` is a breaking change (`fix!:`) so the PR body should call that out per this repo's release-please/conventional-commit convention.

## Ship-gate verification — 2026-08-30

- User authorized `/ship` ("wrapping everthing and /ship"). §0/§1 clean (fetched `origin`, still `c49f32a`, no rebase needed; full suite green: lint/typecheck/632 unit/50 conformance/verify-fixtures). Dispatched the mandatory §2 fresh-Opus ship-gate verifier over the full 4-commit diff (`git diff c49f32a...feat/rfc-macp-0013-commitment-hash`, 17 files, ~1049 insertions).
- **Verdict: GAPS**, 9 items (G1-G9). Verifier's own severity read: G1 and G2 are "the two I'd want an explicit decision on before the PR goes up"; G3-G9 are "safe to defer or waive."
- **G1 — CLOSED (Sonnet fixer, fresh subagent).** `_check_frozen_field_set()` (`src/macp_sdk/commitment_hash.py`) only inspected the compiled proto *schema* (`DESCRIPTOR.fields`), not a message *instance* — a payload received over the wire carrying an unrecognized field number (preserved by protobuf as an "unknown field") sailed through undetected, violating RFC-MACP-0013 §5/§12's cannot-verify MUST. `message.UnknownFields()` raises `NotImplementedError` under this venv's upb backend, so the fix uses `google.protobuf.unknown_fields.UnknownFieldSet` (the upb-safe public replacement) in a new `_check_no_unknown_wire_fields()` helper, called from `canonical_projection()` alongside the existing schema-level guard. New `TestUnknownWireFieldGuard` class in `tests/unit/test_commitment_hash.py` synthesizes an authentic unknown-field instance via `descriptor_pb2`/`descriptor_pool`/`message_factory` + `MergeFromString` into a real `CommitmentPayload` (no local proto-generation step exists in this repo, so this is built ad hoc rather than from a second `.proto` file). Mutation-verified: `git stash` on the fix made the two "raises" tests fail red (`DID NOT RAISE MacpSessionError`); `stash pop` restored green. Final: `pytest tests/unit/ --cov` 636 passed, `commitment_hash.py` 100% coverage, total 87.63%; lint/typecheck clean.
- **G2 — RESOLVED (user decision).** This diff's `ba1d658` (`fix!:`) is the first breaking-marked commit in the repo's history; `release-please-config.json` didn't set `bump-minor-pre-major` (default `false`), so merging would have bumped `0.6.0 → 1.0.0` rather than `0.7.0` — unanalyzed anywhere until this gate caught it. Asked the user; answer: "Do same as typescript so both versions are same." Confirmed the sibling `macp-sdk-typescript` repo's `release-please-config.json` already sets `"bump-minor-pre-major": true` (both manifests currently at `0.6.0`). Applied the same key to this repo's `release-please-config.json` for parity — merge now bumps `0.6.0 → 0.7.0`, matching the TS SDK's convention and keeping both SDKs' versions in lockstep.
- **G3-G9 — deferred/waived, per the verifier's own risk read, not independently re-litigated:**
  - G3 (`ba1d658` landed as `fix!:` rather than the Phase 2 log's recommended `feat!:`; changelog will file it under Bug Fixes not Features) — zero versioning impact (the `!` drives the bump either way); left as-is rather than rewriting already-made local commit history.
  - G4 (`macp_sdk.commitment_hash` resolves to the function, shadowing the submodule for attribute access; `from macp_sdk.commitment_hash import ...` still works) — usability footgun, documented in a test comment already; accepted.
  - G5 (vector runner doesn't assert the RFC-mandated count of 5 vectors or `vector["label"] == LABEL`) — accepted; a full wipe is still caught by the existing `pairs_checked > 0` backstop.
  - G6 (chaining property, RFC §3 rule 4, not asserted in the *file-driven* runner — though it is asserted inline in `test_commitment_hash.py` via `VECTOR_001_HASH`) — accepted, holds today.
  - G7 (`_EXCLUDED_NAMES` filter unreachable given the glob) — previously flagged as Phase 3 advisory C, deliberately left; still dead code, still harmless.
  - G8 (vendored vectors have no automated drift gate against the spec repo) — this is H13's known, disclosed tradeoff (`tests/vectors/cmt-hash/SOURCE.md`), confirmed byte-identical to the spec repo today; accepted cost. (Closed by issue #38 — see below.)
  - G9 (`PROGRESS.md` newly tracked in a public repo, containing absolute `/Users/ajitkoti/…` local paths) — accepted; this repo already force-tracks plan files despite `.gitignore` ignoring `plans/`, so tracking process docs isn't unprecedented, and the paths reveal a local username, not a secret.
- **Re-verify round 1:** fresh Opus subagent, given the G1-G9 gap list, independently reproduced the G1 fix with raw hand-crafted wire bytes (a different technique than the fixer's test), confirmed G2's config change and manifest, and spot-checked G3-G9 as accurately characterized. **Confirmed CLOSED/accurate for all 9** — but surfaced one new item:
  - **G10 (new):** `_check_no_unknown_wire_fields` only inspected `UnknownFieldSet(payload)` at the top level — it did not recurse into the `supersedes` submessage. A `CommitmentRef` carrying wire data for a field outside its own frozen two-field set (`session_id`, `commitment_hash` — RFC-MACP-0013 §5) hashed identically to a clean equivalent, silently dropping the unknown field's contribution — same MUST violation as G1, one level deeper, narrower trigger (requires `CommitmentRef` itself to have grown a field or receive out-of-schema wire data).
- **G10 — CLOSED (Sonnet fixer, fresh subagent).** Mirrored G1's fix pattern exactly, one level down: added `_FROZEN_REF_FIELD_NAMES`/`_ACTUAL_REF_FIELD_NAMES` + `_check_frozen_ref_field_set()` (schema-level, mirrors `_check_frozen_field_set`) and `_check_no_unknown_ref_wire_fields()` (wire-level, mirrors `_check_no_unknown_wire_fields`, same `UnknownFieldSet` technique). Both called from `canonical_projection()` only `if payload.HasField("supersedes")`, before `_supersedes_member` projects it. New `TestFrozenRefFieldSetGuard` (7 tests) and `TestUnknownRefWireFieldGuard` (6 tests) in `tests/unit/test_commitment_hash.py`, including a test that directly reproduces the pre-fix collision (`_supersedes_member(tainted_ref) == _supersedes_member(clean_ref)` despite the tainted ref carrying an unknown field). Mutation-verified: commented out the new guard call site, 3 tests went red, restored, all green. Confirmed vector 002's legitimate `supersedes` still hashes correctly (no false positive). Final: `pytest tests/unit/ --cov` 649 passed, 87.74% total, `commitment_hash.py` still 100%; lint/typecheck/conformance/verify-fixtures all clean.
- **Commit:** `bb35eb9` (G1+G2), `40462c8` (G10) — still all local, nothing pushed.
- **Re-verify round 2:** fresh Opus subagent, given the G10 gap. Independently reproduced the pre-fix collision from hand-crafted wire bytes (not reusing the fixer's test helper), confirmed the post-fix guards raise at both schema and wire level, confirmed `CommitmentRef` is exactly two scalar strings with no extension ranges (no further recursion possible — the fix class is retired, not just patched), confirmed no false positive against vectors 002/004 (live `supersedes`), and re-ran the full suite independently (649 passed 87.74%, `commitment_hash.py` 100%, conformance 50/2-skip, verify-fixtures clean, bare `pytest tests/` 699 passed/35 skipped). **Verdict: PASS — ready to push, open PR, and merge on green CI.**
- **PLAN + SHIP-GATE COMPLETE.** Proceeding to `/ship` §3 (push) → §4 (PR) → §5 (CI watch, merge on green).

## Ship checkpoints

- pushed feat/rfc-macp-0013-commitment-hash 629458b
- PR #36 opened: https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/36

---

# PROGRESS — Issue #38: gate `tests/vectors/cmt-hash/` against canonical

Plan: `plans/gate-cmt-hash-vectors.md` (this repo). Branch: `fix/38-gate-cmt-hash-vectors`.

## Repo map (built once)

- `Makefile:1` — `.PHONY`, already lists `sync-fixtures verify-fixtures`; neither target has prerequisites.
- `Makefile:3` — `SPEC_CONFORMANCE_DIR`, overridable; CI passes `$GITHUB_WORKSPACE/_spec/schemas/conformance`.
- `Makefile:5-7` — **new** `FIXTURE_DIR_PAIRS`. Must never carry an inline `##` comment: `make help`'s awk (`Makefile:10`) matches `/^[a-zA-Z_-]+:.*##/` and `FIXTURE_DIR_PAIRS:` (from `:=`) satisfies the left half.
- `Makefile:41-76` — `sync-fixtures`: spec-dir help block, pre-flight MISSING loop, per-pair copy loop, closing hints.
- `Makefile:77-120` — `verify-fixtures`: spec-dir guard, `drift`/`missing` accumulators, per-pair MISSING/DRIFT/EXTRA/OK, summaries.
- `tests/unit/test_fixture_drift_gate.py` — **new**, 17 cases. Drives the real recipes via `subprocess.run(cwd=tmp_repo, ...)` with an absolute `-f`; never `make -C` (implies `-w`, prints directory banners on GNU Make 4.x only). Strips `MAKEFLAGS`/`MAKELEVEL`/`MFLAGS` (nested under `make test`). Asserts `returncode != 0`, never `== 1` — **GNU Make exits 2** on recipe failure.
- `tests/vectors/cmt-hash/` — 5 `cmt_hash_*.json` + `vector-schema.json` + local-only `SOURCE.md` (exempt: the gate globs `*.json`).
- `.github/workflows/conformance-fixtures.yml` — checks the spec repo out to `_spec`; **no functional change needed**, header comment only (Phase 2).
- `.github/workflows/proto-drift.yml:39` — runs `pytest tests/unit tests/conformance -q`, so the new module executes there too; it must never require the real spec repo.
- `pyproject.toml` — `[tool.coverage.run] source = ["macp_sdk"]`, so a test module importing no `macp_sdk` affects neither numerator nor denominator of the 85% gate.

## Phase status

- **Phase 1 — Directory-pair fixture gate + tests:** DONE
- **Phase 2 — Retire the deferral in the docs:** DONE

## Log

### Phase 1 — 2026-08-30

- **Verdict:** PASS (1 round). Verifier tier: **Opus** — reversible CI-gate change, no
  schema/auth/public-contract/trust-boundary surface, so no Fable escalation.
- Plan itself was reverified by a separate Opus pass before any code: verdict
  `PLAN-GAPS`, 19 items, all folded in. Two would have broken the tests outright —
  GNU Make exits **2** (not 1) on recipe failure, and `${pair##*:}` takes the *last*
  colon-segment rather than everything after the first.
- **Gaps closed post-PASS (hardening, not a GAPS verdict):** swallowed `cp` exit status
  in `sync-fixtures`; wrong remedy advertised when the only failure is `MISSING`; three
  untested branches (sync pre-flight abort — deleting that block left all 14 tests green;
  canonical-side empty-glob guard; one dirty pair suppressing another's `OK`).
- **Files touched:** `Makefile`, `tests/unit/test_fixture_drift_gate.py` (new),
  `plans/gate-cmt-hash-vectors.md`, `ASSUMPTIONS.md` (new), `PROGRESS.md`.
- **Checks:** 17/17 new tests; unit suite 666 passed, coverage 87.74% (gate 85%); lint and
  mypy clean; `make verify-fixtures` exits 0 against the real spec checkout naming both
  pairs; tampering a vector yields `DRIFT` + exit 2. Recipes verified under `/bin/dash`,
  `make -j4`, and paths containing spaces.
- **Commit:** `6fb1c5f`
- **Ship decision:** **accumulate**, do not ship alone. Landing Phase 1 by itself makes two
  in-repo documents false — `tests/vectors/cmt-hash/SOURCE.md`'s "Known cost, accepted for
  now" paragraph and `tests/unit/test_commitment_hash_vectors.py`'s docstring claim that
  the pack is "invisible to the gate's non-recursive glob" — and leaves whole-plan
  acceptance criterion 7 unmet. Phases 1+2 ship as one PR.
- **Next:** Phase 2 (docs-only).

### Phase 2 — 2026-08-30

- **Verdict:** PASS after 2 rounds. Verifier tier: **Opus** both rounds — docs-only, no
  one-way door.
- **Round 1 verdict GAPS (6 items).** The material one: `CLAUDE.md` is **gitignored**
  (`.gitignore:210`), so the phase's documentation edit would never have reached the PR —
  Phase 2 was shipping zero committed docs. Redirected to `docs/contributing.md`, which is
  tracked and already the home for the green-bar gates. Also: two present-tense claims in
  this file's own older RFC-MACP-0013 repo map had gone false (`:15` "invisible to this
  glob", `:94` G8 "accepted cost"), and the Phase 1 log cited `fb4a612`, an orphaned
  pre-amend sha, rather than `6fb1c5f`.
- **Round 2:** all five actionable items confirmed closed; two non-blocking nits taken as
  well (an over-long line in the gates fence, and — more usefully — `verify-fixtures` was
  listed as a green-bar gate without noting it hard-fails for a contributor with no
  sibling spec clone, since unlike `sync-fixtures` that target prints no clone hint).
- **Files touched:** `docs/contributing.md`, `tests/vectors/cmt-hash/SOURCE.md`,
  `tests/unit/test_commitment_hash_vectors.py` (module docstring only — zero code lines),
  `.github/workflows/conformance-fixtures.yml` (header comment only; `on:`/`permissions:`/
  `jobs:`/every `run:` step byte-identical, confirmed by a non-comment-line diff),
  `PROGRESS.md`. `CLAUDE.md` was also updated locally for convenience but is gitignored and
  is not part of the PR.
- **Checks:** lint and mypy clean; unit suite 666 passed, coverage 87.74% (gate 85%);
  `make verify-fixtures` exit 0 against the real spec checkout; workflow re-parsed with
  `yaml.safe_load`. Bare `pytest tests/` (unit + conformance + self-skipping integration):
  716 passed, 35 skipped.
- **Commit:** `2024480`
- **Ship decision:** ship Phases 1+2 as one PR. This is what closes whole-plan acceptance
  criterion 7 — Phase 1 alone would have left `SOURCE.md` and the vector-test docstring
  asserting the opposite of the code.

### Ship — 2026-08-30

- Ship gate verdict: GAPS then PASS. The blocker was `ruff format --check` (run by
  `.github/workflows/checks.yml:25`, absent from `make lint`) failing on the new test
  module — CI would have gone red on PR open. Filed as #39, since the root cause is that
  `make lint`/`make test-all` cannot reproduce `checks.yml`.
- Squashed the two phase commits into one `ci:` commit `93ee199`: `release-please-config.json`
  sets `bump-minor-pre-major: true`, so a `feat:` subject would have cut a 0.8.0 minor for a
  change with zero `src/` impact. Repo squash setting is COMMIT_OR_PR_TITLE / COMMIT_MESSAGES,
  so the `feat(build):` subject would otherwise have survived into the squashed body.
- pushed fix/38-gate-cmt-hash-vectors 93ee199
- PR #40 opened: https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/40

---

# PROGRESS — Issue #43: first-wins vote/ballot cardinality

Plan: `plans/first-wins-vote-cardinality.md` (this repo).

## Repo map (built once, Phase 0)

- `src/macp_sdk/base_projection.py:19-22` — `__init__`; gains `_seen_message_ids: set[str]`.
- `src/macp_sdk/base_projection.py:35-48` — `apply_envelope`. Mode check `:37`, transcript append `:39`, Commitment early-return `:41-46`, mode delegation `:48`. D1 gate goes between `:37` and `:39`. Docstring at `:36` is the D4 contract site.
- `src/macp_sdk/base_projection.py:37-38` — mode-mismatch silent discard. "Projections never drop input" was already false before this change.
- `src/macp_sdk/projections.py:100-109` — Decision Vote branch; `:103` is the unconditional per-sender overwrite (last-wins). `:109` sets `phase = "Voting"` on every Vote, including one arriving after a Commitment.
- `src/macp_sdk/projections.py:75`, `:90` — unguarded `evaluations` / `objections` appends.
- `src/macp_sdk/projections.py:147-164` — `has_blocking_objection` is `any(...)` (duplicate-tolerant); `review_evaluations` / `qualifying_evaluations` return lists (NOT duplicate-tolerant).
- `src/macp_sdk/quorum.py:55` — `ballots: dict[request_id, dict[sender, BallotRecord]]`. Docs claim `ballots[sender].choice`; real field is `.vote` (`:31`).
- `src/macp_sdk/quorum.py:76`, `:82`, `:88` — the three `_set_ballot` call sites (Approve/Reject/Abstain). Single funnel => correctly enforces RFC-0011 §5's *across-type* rule.
- `src/macp_sdk/quorum.py:90-97` — `_set_ballot`; `:92` is the unconditional overwrite. Underscore-prefixed, not exported => signature change is internal.
- `src/macp_sdk/quorum.py:107-161` — every query helper takes `request_id`. `commitment_ready` (`:134`) is `has_quorum(...) and phase != "Committed"`.
- `src/macp_sdk/proposal.py:97`, `:109` — unguarded `accepts` / `rejections` appends. Derived accessors `:137` (set), `:146`/`:159`/`:163` (`any`) are duplicate-tolerant.
- `src/macp_sdk/task.py:123`, `:138`, `:154` — unguarded `updates` / `completions` / `failures` appends. `:184` (`any`), `:186`/`:191` (dict / `[-1]`) are duplicate-tolerant.
- `src/macp_sdk/base_session.py:57` — `self.projection = self._create_projection()`; the same object `_send_and_track` (`:86`) writes to and that callers reach as `session.projection`. `:202-204` `open_stream()`.
- `src/macp_sdk/agent/participant.py:97-99` — `ParticipantActions.send_envelope` -> `self._client.send(...)`. **No local apply.** `:109-153` `start_session` likewise ends at `return self.send_envelope(envelope)`. Grep-verified: **zero** `*Session` constructions anywhere in `src/macp_sdk/agent/`. The agent path is NOT exposed to spurious anomalies.
- `src/macp_sdk/agent/participant.py:410` — `_process_envelope` feeds the projection. `:483` `run()` has **no re-entry guard**. `:550` `process_event` is **public and unguarded**.
- `src/macp_sdk/agent/transports.py:60` — `send_subscribe(session_id)` with `after_sequence` defaulting to `0` => **full accepted-history replay on every subscribe**. The live redelivery path.
- `src/macp_sdk/agent/strategies.py:248-268`, `:286-309` — `majority_voter` / `majority_committer` consume `vote_totals()` / `majority_winner()` via duck-typed `Any`. **No code change needed**; Phase 4 adds a behavioural regression test only.
- `src/macp_sdk/client.py:188` — `send_subscribe(session_id, after_sequence=0)`. `:419-422` — `ack.duplicate` treated as idempotent success; the runtime-boundary analogue of D1.
- `src/macp_sdk/envelope.py:39-40` — `new_message_id()` = `str(uuid.uuid4())`. `:235` — `build_envelope` does `message_id or new_message_id()`, so SDK-built envelopes always carry an id.
- `src/macp_sdk/_logging.py:5` — `logger = logging.getLogger("macp_sdk")`. Use this; **never `warnings.warn`** (`pyproject.toml:157` `filterwarnings = ["error"]`).
- `src/macp_sdk/__init__.py:112-221` — `__all__`, isort-style grouped (SCREAMING -> CamelCase -> snake_case, each alphabetical). New: both `ANOMALY_*` at the head of the SCREAMING block; `ProjectionAnomaly` before `ProposalAcceptanceRules`.
- `tests/conftest.py:59-74` — `make_ack`, whose `message_id: str = ""` is at **`:62`**. The brief attributed this default to `make_envelope`; it does not belong to it.
- `tests/conftest.py:77-95` — `make_envelope`. **No `message_id` parameter**; `:90` always calls `new_message_id()`. Two calls => two different uuid4s. Phase 1 adds `message_id: str | None = None` resolved with `is None` (so `""` is honourable).
- `tests/unit/test_base_projection.py:13-21` — `_Projection` harness (`mode_messages` list). Reuse for all D1/D2 base tests.
- `tests/unit/test_quorum_projection.py:177-209` — `test_one_sender_one_ballot`. `required_approvals=1`, so last-wins REACHES quorum and first-wins does not. **Rewrite, keep the name.**
- `tests/unit/test_decision_projection.py` — 16 tests, no duplicate-vote case. `test_review_evaluations` (`:189`) sends two Evaluations from the same sender `agent-REVIEW` — legal; evaluations are not one-per-sender, and their ids differ.
- `tests/conformance/test_conformance_projections.py:98` — `_build_envelope` uses `new_message_id()`. `:153` — the `expect != "accept"` filter (the D4 caller-filters exemplar). `:157` — insert the zero-anomaly assertion after this line. `:181-186` — per-sender vote assertions.
- `tests/conformance/quorum_reject_paths.json` — `reject Approve alice` then `accept Approve alice`. A same-sender duplicate **iff the loader stops filtering**. The concrete case that makes D4 load-bearing.
- `tests/unit/test_client_helpers.py:142`, `tests/unit/test_client_stream.py:160` — hand-built `Envelope(message_type="Vote", session_id="x")` with proto3-default `message_id == ""`. The real justification for the empty-id guard.
- `tests/unit/test_absorb_runtime_v050.py:151` — `_synthetic_accept` is the repo's only deterministic-id producer (`implicit-accept:h1`). Used at `:175` and `:217` on **different** projection objects, so D1 does not affect it — but a future test applying it twice to one projection would now no-op.
- `tests/unit/test_public_api.py:32` — `test_every_reexport_is_in_all` catches a missing `__all__` entry.
- `docs/guides/building-orchestrators.md:117-134` — **shipped documented double-apply shape**: `client.open_stream(...)` -> `session.projection.apply_envelope(envelope)` at `:126`, alongside `_send_and_track`'s local apply. The real motivation for the dedup-before-detection constraint.
- `docs/guides/streaming.md:143`, `docs/guides/direct-agent-auth.md:72`/`:92`, `examples/direct_agent_auth_initiator.py:65`, `examples/direct_agent_auth_observer.py:43` — shipped `session.open_stream()` call sites.
- `docs/determinism.md:53-63` — documented replay-equivalence pattern. Holds under D1+D3 for `votes`, `ballots`, and `anomalies`; asserted by Phase 4 criterion 12.
- `docs/modes/quorum.md:105-131` — projection-queries block; stale on nearly every line (`proj.request`, `.choice`, arity-0 counts, `commitment_ready(5)`).
- `docs/modes/quorum.md:133-143` — `## Ballot override`. The documented-behaviour removal.
- `docs/modes/decision.md:108` — falsely claims `has_blocking_objection` fires on `{high, critical, block}`; code says `critical` only. `:120` — `transcript` "full ordered history", now deduplicated.
- `docs/api/index.md:21-26` — mkdocstrings projection block; add `ProjectionAnomaly` after `:21`.
- `pyproject.toml:99` — ruff `select` includes `RUF` but **preview is off**, so RUF022 (`__all__` sort) is not enforced; the existing grouping is convention, follow it. `:151` `--strict-markers`. `:156-163` `filterwarnings = ["error"]`. `:168-174` branch coverage, `fail_under = 85`.
- `Makefile:15` — `lint` = `ruff check` **+ `ruff format --check`**. `:34` — `test-all` = lint + typecheck + test + integration + conformance + lint-fixtures.
- `release-please-config.json` — `release-type: python`, `bump-minor-pre-major: true` => pre-1.0 a `BREAKING CHANGE:` cuts a **minor**. `CHANGELOG.md:3` has a hand-written `## Unreleased`.

### External references (read-only; do not edit these repos)

- `macp-runtime/crates/macp-modes/src/mode/decision.rs:217` — rejects a second Vote per sender.
- `macp-runtime/crates/macp-modes/src/mode/quorum.rs:164`, `:184`, `:204` — reject a second ballot on Approve/Reject/Abstain.
- `macp-runtime/crates/macp-core/src/error.rs:66` — `InvalidPayload` -> `INVALID_ENVELOPE`.
- `macp-runtime/src/runtime.rs:634-640` — `Precheck::Duplicate` early-returns **before** `log_store.append` (`:679`) and `publish_accepted_envelope` (`:728`); returns `duplicate: true` -> `ack.duplicate`. Duplicates never enter accepted history, so excluding them from `transcript` is faithful reconstruction, not a tradeoff. (Reported by the TypeScript SDK session; verified in runtime source.)
- `multiagentcoordinationprotocol/rfcs/RFC-MACP-0007-decision-mode.md:79` — §5.3, "the first accepted `Vote` stands."
- `multiagentcoordinationprotocol/rfcs/RFC-MACP-0011-quorum-mode.md:67` — §5 rule 3, "at most one ballot across Approve, Reject, or Abstain." **Silent on which stands.**
- `multiagentcoordinationprotocol/rfcs/RFC-MACP-0001-core.md:306` — at-least-once (load-bearing); `:316` — §8.2 runtime idempotency (corroborating, supplies the identity only).
- `multiagentcoordinationprotocol/rfcs/RFC-MACP-0006-transport-bindings.md` — §3.2 Passive Session Subscription. Obligation 4: "Never replay rejected envelopes." **Redelivery clause added in spec PR #80 (`110add2`, RFC-0006 -> 1.4.0-draft):** clients MUST tolerate already-observed envelopes and key detection on `message_id`; a repeat MUST NOT advance sequence position; a repeat MUST NOT count against a Mode cardinality rule; consumers accumulating per-envelope state MUST be idempotent on `message_id`. This is the conformance basis for PR A.
- Spec commit `f1489df` (spec PR #79) — the RFC-0007 §5.3 tightening. Its own message says it "needs no code change anywhere." **Not the justification for this work.**

## Corrections to the original brief (code wins)

1. `tests/conftest.py:62` is `make_ack`, not `make_envelope`. `make_envelope` (`:77`) has no `message_id` parameter and always generates a uuid4. **This inverts the test-design trap:** the risk is two *different* ids from two helper calls silently turning a redelivery test into a cardinality test — not two empty ids.
2. `has_blocking_objection()` is `any(...)` and **cannot** change value from duplication. Only raw list lengths and the two Decision list accessors do.
3. RFC-0011 §5 does **not** say "first wins" — it says "at most one ballot" and is silent on which stands. First-wins for ballots is inferred from RFC-0007 §5.3 parity plus runtime behaviour.
4. The append-on-replay bug is **seven** sites across **three** modes (Decision 2, Proposal 2, Task 3), not two.
5. The double-apply risk is **not** present on the agent path (verified: no local apply in `ParticipantActions`, no `BaseSession` in `agent/`) — unlike the TypeScript SDK, which has it on its happy path. It **is** present via `BaseSession` + `open_stream`, and that shape is taught by `docs/guides/building-orchestrators.md:126`, making it documented guidance rather than a hypothetical integration.

## Phase status

- **Phase 0 — Repo map into PROGRESS.md:** DONE
- **Phase 1 — message_id-idempotent apply + normative docstring contract:** DONE
- **Phase 2 — Replay inflation across seven append sites:** DONE
- **Phase 3 — ProjectionAnomaly public surface (inert):** TODO
- **Phase 4 — First-wins at the two sites:** TODO
- **Phase 5 — Documented-behaviour removal and release notes:** TODO

## Log

### Phase 0 — 2026-08-31

- Plan authored by a fresh Opus planning agent doing its own deep read; corrected three claims in the brief (see Corrections above). Repo map written here so later phases do not re-scan.
- One-way-door analysis (anomaly API shape) ran on Fable; three decisions escalated to and made by the user: base-level dedup gating transcript, TypeScript's scalar field set, ship-when-ready.
- Cross-repo coordination with the spec-repo and macp-sdk-typescript sessions throughout; no writes outside this repo.

### Phase 1 — 2026-08-31

- **Verdict:** PASS (round 1) → GAPS (re-verify, 2 documentation-only) → closed. Verifier tier:
  **Opus** both rounds — no one-way door in this phase (the API-shape one-way door was
  resolved before the drive started and is baked in as D1–D5).
- **Round 1 PASS with 6 advisory findings.** The gate proved rather than asserted its two
  key claims: built a git worktree at HEAD to establish the pre-Phase-1 baseline (666 → 672,
  exactly the six new tests, zero existing assertions touched), and mutation-proved the
  mode-check ordering by moving the dedup gate above it (exactly one test failed, so
  `test_wrong_mode_envelope_does_not_poison_seen_set` is the sole discriminator and is not
  passing for an incidental reason). All spec citations verified; RFC-MACP-0006 §3.2's
  redelivery clause confirmed present locally at `110add2` (not stale).
- **Advisory finding promoted to blocking by the orchestrator: the partial-apply wedge.**
  Recording the id and appending to `transcript` *before* the effect meant a raising
  `_apply_mode_message` left the envelope marked-seen-but-unapplied, and every retry was
  then silently swallowed — pre-Phase-1 a retry recovered. This is a regression introduced
  by this phase and a silent-failure path, which the standing bar forbids, so it was fixed
  rather than accepted. Fix: roll back `transcript` + `_seen_message_ids` on exception and
  re-raise unchanged. Logged in `ASSUMPTIONS.md` (UNCONFIRMED) with rejected alternatives.
- **Re-verify GAPS (2), both honesty items, both closed.** (1) The atomicity claim in the
  code comment — and in the `ASSUMPTIONS.md` entry — was stated unqualified but the rollback
  covers `transcript` and `_seen_message_ids` only, not `self.phase` or subclass collections.
  Unreachable today only because every subclass raises before it mutates; that invariant was
  undocumented, untested, and holds on a publicly exported ABC that Phases 3–4 will add
  logic inside. Both now scoped, with the invariant named as the reason the narrow rollback
  suffices. (2) The public docstring said nothing about exception behaviour, so a caller
  catching an exception had no documented basis for retrying — the whole user-visible point
  of the fix lived only in an internal comment. Now in the public contract.
- **Best catch of the run:** the unconditional `transcript.pop()` was justified by a comment
  about single-threadedness — the wrong hazard. The real risks to "`transcript[-1]` is what I
  appended" are a subclass appending to `transcript` before raising, or re-entrancy. Neither
  occurs today (all five subclasses and both call sites checked exhaustively), but a
  wrong-entry pop on a public ABC is corruption worse than the bug it fixes. Now an
  identity-guarded pop (`is`, not `==` — protobuf compares by value), with the false arm
  covered honestly by a subclass that appends a sentinel before raising.
- **Adversarial check imported from the TypeScript SDK session** (they found their own guard
  provably dead at one of six call sites): deleted the empty-`message_id` guard and ran the
  suite. Went red with exactly one failure, so the guard is live. Their structural risk did
  not transfer — `recordAnomaly` has six call sites, `apply_envelope` has one.
- **Files touched:** `src/macp_sdk/base_projection.py`, `tests/conftest.py`,
  `tests/unit/test_base_projection.py`, `ASSUMPTIONS.md`, `PROGRESS.md`, plan.
- **Checks:** `make lint`, `make typecheck`, `make test-all` green. **679 passed**
  (666 baseline + 13 new), coverage 87.83% (gate 85%), `base_projection.py` 100% —
  proven differentially by deselecting the new classes, not taken from the summary line.
- **Ship decision:** **accumulate**, do not ship alone. Per the plan's shipping table PR A is
  phases 0+1+2; Phase 1 alone would ship a user-visible `transcript` behaviour change with
  zero CHANGELOG narrative, and Phase 2 *is* the release narrative for the second,
  independent bug (replay inflation across seven append sites).
- **Next:** Phase 2.

### Phase 2 — 2026-08-31

- **Verdict:** GAPS (1 blocking, 4 minor) → closed → green. Verifier tier: **Opus** — tests
  and release notes only, no one-way door.
- **Tests are load-bearing, proven not assumed.** The gate neutered the Phase 1 dedup guard
  and re-ran: **8 of 8** new tests failed. So no Phase 2 test fell into the id-reuse trap
  (`make_envelope` mints a fresh uuid4 per call, so two calls would have silently turned a
  redelivery test into a distinctness test that passes while proving nothing). Tree restored
  byte-identically, verified by md5.
- **Blast radius verified by execution, not by reading the plan.** The gate ran a real
  double-apply and diffed every accessor. Changed: `evaluations` 2→4, `objections` 1→2,
  `review_evaluations()` 1→2, `qualifying_evaluations()` 1→2, `accepts`/`rejections`/
  `updates`/`completions`/`failures` 1→2, `transcript` 3→6. Unchanged:
  `has_blocking_objection()`, `is_accepted()`, `accepted_proposal()`, `is_retryable()`,
  `latest_progress()`, `progress_of()`, plus `is_terminally_rejected()`,
  `has_terminal_rejection()`, `is_completed()`, `is_failed()`, `vote_totals()`.
  **Zero false claims in the CHANGELOG body.**
- **"Seven sites" confirmed exact, not an undercount** — full `.append(` inventory re-run
  across `src/macp_sdk/`; `handoff.py` and `quorum.py` have zero.
- **G1 (blocking):** the `## Unreleased` preamble read "No SDK API changes" while the bullet
  four lines below described `apply_envelope` no-opping on redelivery — self-refuting on the
  same screen. Narrowed to "plus one projection behaviour fix — no API *signature* changes."
- **G2 — a defect in THIS PLAN, not in the executor's work.** Phase 2 criterion 1 prescribed
  `assert len(qualifying_evaluations()) == 0`, which **cannot fail**: with a single `REVIEW`
  evaluation that accessor filters `!= "REVIEW"` and returns `[]` however many times the
  envelope is applied. It read like coverage of an accessor the CHANGELOG names as affected
  while pinning nothing. Criterion corrected in the plan; the test now feeds a second
  non-`REVIEW` evaluation and asserts `== 1`. The fixer confirmed the *new* assertion is
  independently load-bearing by running the sequence standalone against a neutered guard
  (returned 2, not 1) rather than letting it ride on an earlier assertion.
- **G4:** this CHANGELOG entry is PR A's *only* user-facing artifact (Phase 1 docs are
  deliberately deferred to Phase 5), so two contract boundaries had no user-visible signal
  anywhere: empty `message_id` is never deduped, and a failed apply rolls back so the caller
  can retry. Both added, with the rollback's honest limit stated.
- **G3:** the class-docstring trap table listed a "Distinctness" row no test in that class
  exercises; now cross-references `test_base_projection.py::TestIdempotentApply::
  test_distinct_message_ids_both_applied`.
- **Files touched:** `tests/unit/test_decision_projection.py`,
  `tests/unit/test_proposal_projection.py`, `tests/unit/test_task_projection.py`,
  `CHANGELOG.md`, `PROGRESS.md`, plan. **Zero `src/` changes** — the mechanism shipped in
  Phase 1; this phase proves it and narrates it.
- **Checks:** `make lint`, `make typecheck`, `make test-all` green. **687 passed**
  (679 + 8), coverage 87.83% (gate 85%).
- **Ship decision:** **ship now as PR A (phases 0+1+2).** The gate confirmed nothing in PR A
  depends on B or C — no anomaly surface, no cardinality change — so the hard sequencing
  interlock is satisfied.
- **Next:** Phase 3 (inert `ProjectionAnomaly` surface).
