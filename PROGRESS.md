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
- `Makefile` — `test:` runs `pytest tests/unit/ -v --cov` (85% branch floor via `pyproject.toml [tool.coverage]`). `test-conformance:` runs `pytest tests/conformance/ -v -m conformance`. `verify-fixtures:` (:56-78) walks `tests/conformance/*.json` (non-recursive) vs spec repo's flat `schemas/conformance/*.json` — fails on drift AND on extra files. **A `tests/vectors/cmt-hash/` subdirectory is invisible to this glob**, confirming H13's chosen location is safe.
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
- **Phase 2 — Validate at both call sites:** TODO
- **Phase 3 — Vector runner outside drift gate:** TODO

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
