from __future__ import annotations

import sys

import pytest
from macp.v1 import core_pb2

from macp_sdk.commitment_hash import (
    _ACTUAL_FIELD_NAMES,
    _FROZEN_FIELD_NAMES,
    _check_frozen_field_set,
    canonical_projection,
    commitment_hash,
    is_canonical_commitment_hash,
)
from macp_sdk.errors import MacpSessionError

# `macp_sdk/__init__.py` does `from .commitment_hash import commitment_hash`,
# which reassigns the package attribute `macp_sdk.commitment_hash` to the
# *function* (shadowing the submodule) -- the classic same-name
# submodule/attribute gotcha. `sys.modules` is unaffected by that and always
# holds the real module object, so tests that need to monkeypatch a
# module-level name go through it rather than `import macp_sdk.commitment_hash`.
_commitment_hash_module = sys.modules["macp_sdk.commitment_hash"]

# RFC-MACP-0013 §11 reference vectors, copied inline (see the RFC / spec repo
# for the authoritative JSON fixtures; Phase 3 wires up the vector-file
# runner under tests/vectors/cmt-hash/).

VECTOR_001_HASH = "sha256:9f58e9d114d11860d48aa2bcb8cda458b9618b1cc8560595a802b68c4af85d41"
VECTOR_001_JCS_HEX = (
    "7b22616374696f6e223a226465636973696f6e2e617070726f766564222c2261757468"
    "6f726974795f73636f7065223a227365616d222c22636f6d6d69746d656e745f696422"
    "3a226331222c22636f6e66696775726174696f6e5f76657273696f6e223a22312e302e"
    "30222c226d6f64655f76657273696f6e223a226d6163702e6d6f64652e646563697369"
    "6f6e2e7631222c226f7574636f6d655f706f736974697665223a747275652c22706f6c"
    "6963795f76657273696f6e223a22312e302e30222c22726561736f6e223a227365616c"
    "6564206279207365616d227d"
)
VECTOR_002_HASH = "sha256:7cc490432ad6b25e9c19fc7c3a84f1e33abe497fca1fd5266ff0275db3650f9d"
VECTOR_003_HASH = "sha256:3240d1a7adb7bd9420ad5490182227ce699c9e4e465f7934885fe2ded939f32e"
VECTOR_004_HASH = "sha256:9776c22ef165f26817f89bb456cf6bc56a659eb1561a576f6ea9a435bd3291d7"
VECTOR_005_HASH = "sha256:03f8ac2b8172958504092ce9fe5154dbcfe300fd30a350453d4e4bd715822ab2"


def _vector_001() -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="c1",
        action="decision.approved",
        authority_scope="seam",
        reason="sealed by seam",
        mode_version="macp.mode.decision.v1",
        policy_version="1.0.0",
        configuration_version="1.0.0",
        outcome_positive=True,
    )


def _vector_002() -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="c2",
        action="decision.approved",
        authority_scope="seam",
        reason="sealed by seam",
        mode_version="macp.mode.decision.v1",
        policy_version="1.0.0",
        configuration_version="1.0.0",
        outcome_positive=True,
        supersedes=core_pb2.CommitmentRef(
            session_id="prior-sess",
            commitment_hash=VECTOR_001_HASH,
        ),
    )


def _vector_003() -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="",
        action="",
        authority_scope="",
        reason="",
        mode_version="",
        policy_version="",
        configuration_version="",
        outcome_positive=False,
    )


def _vector_004() -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="",
        action="",
        authority_scope="",
        reason="",
        mode_version="",
        policy_version="",
        configuration_version="",
        outcome_positive=False,
        supersedes=core_pb2.CommitmentRef(session_id="", commitment_hash=""),
    )


def _vector_005() -> core_pb2.CommitmentPayload:
    # U+1F702 (astral-plane codepoint) must be written as the scalar value,
    # not as a UTF-16 surrogate pair -- see RFC-MACP-0013 §11 vector notes.
    return core_pb2.CommitmentPayload(
        commitment_id="c5",
        action='decision."appro\\ved"',
        authority_scope="café",
        reason="ré\tsumé\n— naïve \U0001f702",
        mode_version="macp.mode.decision.v1",
        policy_version="1.0.0",
        configuration_version="1.0.0",
        outcome_positive=False,
    )


class TestCommitmentHashVectors:
    def test_vector_001_minimal(self):
        assert commitment_hash(_vector_001()) == VECTOR_001_HASH

    def test_vector_001_canonical_projection_matches_jcs_bytes(self):
        assert canonical_projection(_vector_001()).hex() == VECTOR_001_JCS_HEX

    def test_vector_002_supersedes_chained(self):
        assert commitment_hash(_vector_002()) == VECTOR_002_HASH

    def test_vector_003_all_empty(self):
        assert commitment_hash(_vector_003()) == VECTOR_003_HASH

    def test_vector_004_empty_supersedes(self):
        assert commitment_hash(_vector_004()) == VECTOR_004_HASH

    def test_vector_005_escapes(self):
        assert commitment_hash(_vector_005()) == VECTOR_005_HASH

    def test_vector_003_and_004_differ(self):
        # The sole vector-level check (RFC-MACP-0013 §11) that unset vs.
        # empty `supersedes` is actually implemented, not collapsed.
        assert commitment_hash(_vector_003()) != commitment_hash(_vector_004())


class TestSupersedesHasFieldVsTruthiness:
    def test_unset_supersedes_differs_from_explicit_empty_supersedes(self):
        unset = core_pb2.CommitmentPayload(outcome_positive=False)
        explicit_empty = core_pb2.CommitmentPayload(
            outcome_positive=False,
            supersedes=core_pb2.CommitmentRef(),
        )
        assert not unset.HasField("supersedes")
        assert explicit_empty.HasField("supersedes")
        # A naive `if payload.supersedes:` truthiness check would treat an
        # all-empty CommitmentRef as falsy and collapse this to "unset",
        # which would make these two projections/hashes identical. They
        # must not be.
        assert canonical_projection(unset) != canonical_projection(explicit_empty)
        assert commitment_hash(unset) != commitment_hash(explicit_empty)

    def test_unset_supersedes_omits_key_entirely(self):
        unset = core_pb2.CommitmentPayload(outcome_positive=False)
        assert b'"supersedes"' not in canonical_projection(unset)

    def test_explicit_empty_supersedes_includes_key(self):
        explicit_empty = core_pb2.CommitmentPayload(
            outcome_positive=False,
            supersedes=core_pb2.CommitmentRef(),
        )
        projection = canonical_projection(explicit_empty)
        assert b'"supersedes":{"commitment_hash":"","session_id":""}' in projection


class TestJcsEscaping:
    def _project_reason(self, reason: str) -> bytes:
        payload = core_pb2.CommitmentPayload(reason=reason, outcome_positive=False)
        return canonical_projection(payload)

    def test_backslash_escaped(self):
        assert b'"reason":"a\\\\b"' in self._project_reason("a\\b")

    def test_quote_escaped(self):
        assert b'"reason":"a\\"b"' in self._project_reason('a"b')

    def test_tab_short_form(self):
        assert b'"reason":"a\\tb"' in self._project_reason("a\tb")

    def test_newline_short_form(self):
        assert b'"reason":"a\\nb"' in self._project_reason("a\nb")

    def test_backspace_short_form(self):
        assert b'"reason":"a\\bb"' in self._project_reason("a\bb")

    def test_formfeed_short_form(self):
        assert b'"reason":"a\\fb"' in self._project_reason("a\fb")

    def test_carriage_return_short_form(self):
        assert b'"reason":"a\\rb"' in self._project_reason("a\rb")

    def test_other_c0_control_uses_u00xx(self):
        assert b'"reason":"a\\u0001b"' in self._project_reason("a\x01b")
        assert b'"reason":"a\\u001fb"' in self._project_reason("a\x1fb")

    def test_non_ascii_bmp_character_literal(self):
        projection = self._project_reason("café")
        assert "café".encode() in projection
        assert b"\\u" not in projection

    def test_astral_plane_codepoint_literal_utf8(self):
        projection = self._project_reason("\U0001f702")
        assert "\U0001f702".encode() in projection
        assert b"\\u" not in projection


class TestOutcomePositiveAlwaysMaterialized:
    def test_false_is_present_not_omitted(self):
        payload = core_pb2.CommitmentPayload(outcome_positive=False)
        assert b'"outcome_positive":false' in canonical_projection(payload)

    def test_true_is_present(self):
        payload = core_pb2.CommitmentPayload(outcome_positive=True)
        assert b'"outcome_positive":true' in canonical_projection(payload)


class TestIsCanonicalCommitmentHash:
    def test_accepts_well_formed(self):
        assert is_canonical_commitment_hash("sha256:" + "a" * 64)

    def test_rejects_uppercase_hex(self):
        assert not is_canonical_commitment_hash("sha256:" + "A" * 64)

    def test_rejects_wrong_length(self):
        assert not is_canonical_commitment_hash("sha256:" + "a" * 63)

    def test_rejects_missing_prefix(self):
        assert not is_canonical_commitment_hash("a" * 64)

    def test_rejects_wrong_prefix(self):
        assert not is_canonical_commitment_hash("sha255:" + "a" * 64)

    def test_rejects_empty_string(self):
        assert not is_canonical_commitment_hash("")

    def test_rejects_leading_whitespace(self):
        assert not is_canonical_commitment_hash(" sha256:" + "a" * 64)

    def test_rejects_trailing_whitespace(self):
        assert not is_canonical_commitment_hash("sha256:" + "a" * 64 + " ")

    def test_rejects_trailing_newline(self):
        # Regression guard: Python's `$` in a `match`-anchored regex matches
        # immediately *before* a trailing "\n", so a naive
        # `_HASH_RE.match(value)` check alone would wrongly accept this
        # value. `is_canonical_commitment_hash` must use `re.fullmatch`
        # (which anchors both ends with no such quirk) to reject it.
        value = "sha256:" + "a" * 64 + "\n"
        assert not is_canonical_commitment_hash(value)


class TestFrozenFieldSetGuard:
    """RFC-MACP-0013 §5, §12: a CommitmentPayload carrying a field outside
    the frozen nine-field set is not hashable under this label and MUST
    raise, never be silently ignored."""

    def test_frozen_set_matches_installed_proto(self):
        # Sanity check: the installed macp-proto's CommitmentPayload fields
        # are exactly the frozen nine today. If this ever fails, a real
        # field was added/removed upstream and this module needs updating.
        assert _ACTUAL_FIELD_NAMES == _FROZEN_FIELD_NAMES

    def test_accepts_exactly_the_frozen_nine(self):
        _check_frozen_field_set(_FROZEN_FIELD_NAMES)  # must not raise

    def test_accepts_subset_of_frozen_nine(self):
        _check_frozen_field_set(frozenset({"commitment_id", "action"}))  # must not raise

    def test_rejects_extra_field(self):
        simulated_fields = _FROZEN_FIELD_NAMES | {"new_field_from_future_proto"}
        with pytest.raises(MacpSessionError, match="new_field_from_future_proto"):
            _check_frozen_field_set(simulated_fields)

    def test_default_arg_checks_actual_installed_proto(self):
        _check_frozen_field_set()  # must not raise against the real descriptor

    def test_canonical_projection_raises_when_proto_has_extra_field(self, monkeypatch):
        # Simulate a future macp-proto that added a 10th field, by making the
        # module believe the installed descriptor carries one, and confirm
        # canonical_projection() (and therefore commitment_hash()) refuses
        # to silently hash it.
        monkeypatch.setattr(
            _commitment_hash_module,
            "_ACTUAL_FIELD_NAMES",
            _FROZEN_FIELD_NAMES | {"unexpected_field"},
        )
        payload = core_pb2.CommitmentPayload(outcome_positive=False)
        with pytest.raises(MacpSessionError, match="unexpected_field"):
            canonical_projection(payload)
        with pytest.raises(MacpSessionError, match="unexpected_field"):
            commitment_hash(payload)
