from __future__ import annotations

import itertools
import sys

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, unknown_fields
from macp.v1 import core_pb2

from macp_sdk.commitment_hash import (
    _ACTUAL_FIELD_NAMES,
    _ACTUAL_REF_FIELD_NAMES,
    _FROZEN_FIELD_NAMES,
    _FROZEN_REF_FIELD_NAMES,
    _check_frozen_field_set,
    _check_frozen_ref_field_set,
    _supersedes_member,
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


class TestFrozenRefFieldSetGuard:
    """RFC-MACP-0013 §5: `supersedes`, when set, carries exactly two fields
    (session_id, commitment_hash). A `CommitmentRef` carrying a field outside
    that set is not hashable under this label and MUST raise, never be
    silently ignored. Mirrors `TestFrozenFieldSetGuard` one level down."""

    def test_frozen_ref_set_matches_installed_proto(self):
        # Sanity check: the installed macp-proto's CommitmentRef fields are
        # exactly the frozen two today. If this ever fails, a real field was
        # added/removed upstream and this module needs updating.
        assert _ACTUAL_REF_FIELD_NAMES == _FROZEN_REF_FIELD_NAMES

    def test_accepts_exactly_the_frozen_two(self):
        _check_frozen_ref_field_set(_FROZEN_REF_FIELD_NAMES)  # must not raise

    def test_accepts_subset_of_frozen_two(self):
        _check_frozen_ref_field_set(frozenset({"session_id"}))  # must not raise

    def test_rejects_extra_field(self):
        simulated_fields = _FROZEN_REF_FIELD_NAMES | {"new_field_from_future_proto"}
        with pytest.raises(MacpSessionError, match="new_field_from_future_proto"):
            _check_frozen_ref_field_set(simulated_fields)

    def test_default_arg_checks_actual_installed_proto(self):
        _check_frozen_ref_field_set()  # must not raise against the real descriptor

    def test_canonical_projection_raises_when_ref_has_extra_field(self, monkeypatch):
        # Simulate a future macp-proto that added a 3rd field to
        # CommitmentRef, by making the module believe the installed
        # descriptor carries one, and confirm canonical_projection() (and
        # therefore commitment_hash()) refuses to silently hash it -- but
        # only when supersedes is actually set.
        monkeypatch.setattr(
            _commitment_hash_module,
            "_ACTUAL_REF_FIELD_NAMES",
            _FROZEN_REF_FIELD_NAMES | {"unexpected_ref_field"},
        )
        payload_with_supersedes = core_pb2.CommitmentPayload(
            outcome_positive=False,
            supersedes=core_pb2.CommitmentRef(session_id="s", commitment_hash="h"),
        )
        with pytest.raises(MacpSessionError, match="unexpected_ref_field"):
            canonical_projection(payload_with_supersedes)
        with pytest.raises(MacpSessionError, match="unexpected_ref_field"):
            commitment_hash(payload_with_supersedes)

    def test_canonical_projection_unaffected_when_supersedes_absent(self, monkeypatch):
        # The guard must only fire when supersedes is actually present -- an
        # absent supersedes has no CommitmentRef to check.
        monkeypatch.setattr(
            _commitment_hash_module,
            "_ACTUAL_REF_FIELD_NAMES",
            _FROZEN_REF_FIELD_NAMES | {"unexpected_ref_field"},
        )
        payload_without_supersedes = core_pb2.CommitmentPayload(outcome_positive=False)
        canonical_projection(payload_without_supersedes)  # must not raise
        commitment_hash(payload_without_supersedes)  # must not raise


# Monotonic counter so each call to `_build_payload_with_unknown_field` (across
# tests, and across repeated calls within a single test) registers its
# throwaway "shadow" proto under a fresh package/file name -- `descriptor_pool`
# raises if the same fully-qualified name is added twice, even into separate
# `DescriptorPool()` instances in some versions.
_shadow_counter = itertools.count()


def _build_payload_with_unknown_field() -> core_pb2.CommitmentPayload:
    """Build a *real* ``core_pb2.CommitmentPayload`` whose wire bytes include
    a field number the installed schema does not recognize -- i.e. an actual
    "unknown field" on the parsed instance, not merely a schema that has
    grown a 10th field (that case is `TestFrozenFieldSetGuard`, simulated via
    monkeypatching `_ACTUAL_FIELD_NAMES`).

    This can't be done with a second ``.proto`` file (this repo has no local
    proto-generation step -- see CLAUDE.md), so instead it synthesizes one at
    runtime: a throwaway "shadow" message type is built via
    ``descriptor_pb2``/``descriptor_pool`` that mirrors
    ``CommitmentPayload``'s known field numbers 1-2 (``commitment_id``,
    ``action``) plus one extra field at number 10 (one past the real
    ``supersedes`` at 9) that the real schema has never heard of. Serializing
    the shadow message and parsing those bytes into a real
    ``core_pb2.CommitmentPayload`` reproduces exactly what happens when a
    peer running a newer wire format sends an extra field this SDK's pinned
    ``macp-proto`` doesn't know about: protobuf parses it successfully and
    stashes the field-10 bytes as an "unknown field" on the real message,
    invisible to ``CommitmentPayload.DESCRIPTOR.fields``.
    """
    n = next(_shadow_counter)
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = f"shadow_commitment_{n}.proto"
    fdp.package = f"shadow.v1.n{n}"
    fdp.syntax = "proto3"

    msg = fdp.message_type.add()
    msg.name = "ShadowCommitmentPayload"

    def add_field(name: str, number: int, ftype: int) -> None:
        f = msg.field.add()
        f.name = name
        f.number = number
        f.type = ftype
        f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    add_field("commitment_id", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    add_field("action", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    # One past real CommitmentPayload's highest field number (9, `supersedes`)
    # -- a field number this installed schema has never heard of at all.
    add_field(
        "field_from_the_future", 10, descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    )

    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    shadow_cls = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"shadow.v1.n{n}.ShadowCommitmentPayload")
    )
    shadow = shadow_cls(
        commitment_id="c1",
        action="decision.approved",
        field_from_the_future="unknown-wire-data",
    )

    payload = core_pb2.CommitmentPayload()
    payload.MergeFromString(shadow.SerializeToString())
    return payload


class TestUnknownWireFieldGuard:
    """RFC-MACP-0013 §5: a peer may send a ``CommitmentPayload`` carrying
    wire data for a field number this installed schema has never heard of at
    all. Protobuf preserves that as an "unknown field" on the parsed
    instance -- invisible to ``DESCRIPTOR.fields`` (and hence to
    `_check_frozen_field_set`, which only sees schema drift). A verifier
    presented with such a payload MUST return a cannot-verify result, never
    silently omit the unknown field's contribution from the hash."""

    def test_unknown_field_reproduced_on_instance(self):
        # Sanity check on the fixture itself, independent of this module:
        # confirm the "shadow" round-trip actually produces a real
        # CommitmentPayload instance carrying wire data for an unrecognized
        # field number (field 10), using the same upb-safe accessor the fix
        # uses. If this ever fails, the fixture stopped reproducing the
        # scenario and the rest of this class is testing nothing.
        from google.protobuf import unknown_fields

        payload = _build_payload_with_unknown_field()
        assert payload.commitment_id == "c1"
        assert payload.action == "decision.approved"
        unknown = unknown_fields.UnknownFieldSet(payload)
        assert len(unknown) == 1
        assert unknown[0].field_number == 10

        # And confirm the classic accessor is indeed unusable here (the
        # constraint that ruled out `UnknownFields()` for the fix itself).
        with pytest.raises(NotImplementedError):
            payload.UnknownFields()

    def test_canonical_projection_raises_on_unknown_wire_field(self):
        payload = _build_payload_with_unknown_field()
        with pytest.raises(MacpSessionError, match=r"\[10\]"):
            canonical_projection(payload)

    def test_commitment_hash_raises_on_unknown_wire_field(self):
        payload = _build_payload_with_unknown_field()
        with pytest.raises(MacpSessionError, match=r"\[10\]"):
            commitment_hash(payload)

    def test_ordinary_payload_without_unknown_fields_is_unaffected(self):
        # Additive, not a replacement: a normal payload with no unknown wire
        # data must still hash exactly as before.
        payload = core_pb2.CommitmentPayload(
            commitment_id="c1", outcome_positive=False
        )
        canonical_projection(payload)  # must not raise
        commitment_hash(payload)  # must not raise


def _build_ref_with_unknown_field() -> core_pb2.CommitmentRef:
    """Build a *real* ``core_pb2.CommitmentRef`` whose wire bytes include a
    field number the installed schema does not recognize -- the same
    "shadow" descriptor technique as `_build_payload_with_unknown_field`,
    applied one level down to ``CommitmentRef`` (frozen fields
    ``session_id``=1, ``commitment_hash``=2) instead of ``CommitmentPayload``
    (frozen fields up to ``supersedes``=9).

    This reproduces the G10 gap verbatim: a ``supersedes.CommitmentRef``
    carrying wire data for field number 7 (chosen arbitrarily, matching the
    verifier's own repro) that protobuf parses successfully and stashes as an
    "unknown field", invisible to ``CommitmentRef.DESCRIPTOR.fields``.
    """
    n = next(_shadow_counter)
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = f"shadow_commitment_ref_{n}.proto"
    fdp.package = f"shadow.ref.v1.n{n}"
    fdp.syntax = "proto3"

    msg = fdp.message_type.add()
    msg.name = "ShadowCommitmentRef"

    def add_field(name: str, number: int, ftype: int) -> None:
        f = msg.field.add()
        f.name = name
        f.number = number
        f.type = ftype
        f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    add_field("session_id", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    add_field("commitment_hash", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    # Field number 7, outside CommitmentRef's frozen two-field set (1, 2) --
    # a field number this installed schema has never heard of at all.
    add_field(
        "field_from_the_future", 7, descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    )

    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    shadow_cls = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"shadow.ref.v1.n{n}.ShadowCommitmentRef")
    )
    shadow = shadow_cls(
        session_id="prior-sess",
        commitment_hash=VECTOR_001_HASH,
        field_from_the_future="unknown-wire-data",
    )

    ref = core_pb2.CommitmentRef()
    ref.MergeFromString(shadow.SerializeToString())
    return ref


class TestUnknownRefWireFieldGuard:
    """RFC-MACP-0013 §5, gap G10: a ``supersedes.CommitmentRef`` may carry
    wire data for a field number this installed schema has never heard of at
    all. Without recursing into the nested message, this was silently
    dropped -- a payload with such an unknown field hashed identically to
    the clean equivalent. This class confirms the collision existed
    (empirically, via the fixture) and that the fix now raises instead."""

    def test_unknown_field_reproduced_on_ref_instance(self):
        # Sanity check on the fixture itself: confirm the "shadow" round-trip
        # actually produces a real CommitmentRef instance carrying wire data
        # for an unrecognized field number (field 7). If this ever fails,
        # the fixture stopped reproducing the scenario and the rest of this
        # class is testing nothing.
        ref = _build_ref_with_unknown_field()
        assert ref.session_id == "prior-sess"
        assert ref.commitment_hash == VECTOR_001_HASH
        unknown = unknown_fields.UnknownFieldSet(ref)
        assert len(unknown) == 1
        assert unknown[0].field_number == 7

    def test_unknown_ref_field_collides_with_clean_equivalent_before_fix(self):
        # Empirical reproduction of the G10 collision: build the payload with
        # the tainted supersedes, and a clean payload with the same visible
        # session_id/commitment_hash, and confirm that -- with the new guard
        # bypassed -- they would hash identically. We can't literally "undo"
        # the fix from the test, so instead we confirm the two refs are
        # wire-distinguishable (the unknown field is really there) while the
        # *visible* projected members are identical, which is exactly the
        # condition that made the pre-fix code collide (it only projected
        # session_id/commitment_hash and never looked at anything else).
        tainted_ref = _build_ref_with_unknown_field()
        clean_ref = core_pb2.CommitmentRef(
            session_id="prior-sess", commitment_hash=VECTOR_001_HASH
        )
        assert _supersedes_member(tainted_ref) == _supersedes_member(clean_ref)
        assert len(unknown_fields.UnknownFieldSet(tainted_ref)) == 1
        assert len(unknown_fields.UnknownFieldSet(clean_ref)) == 0

    def test_canonical_projection_raises_on_unknown_ref_wire_field(self):
        payload = core_pb2.CommitmentPayload(
            outcome_positive=False, supersedes=_build_ref_with_unknown_field()
        )
        with pytest.raises(MacpSessionError, match=r"\[7\]"):
            canonical_projection(payload)

    def test_commitment_hash_raises_on_unknown_ref_wire_field(self):
        payload = core_pb2.CommitmentPayload(
            outcome_positive=False, supersedes=_build_ref_with_unknown_field()
        )
        with pytest.raises(MacpSessionError, match=r"\[7\]"):
            commitment_hash(payload)

    def test_ordinary_supersedes_without_unknown_fields_is_unaffected(self):
        # Additive, not a replacement: a normal, valid supersedes must still
        # hash exactly as before (also covered by vector 002, but asserted
        # directly here too).
        payload = core_pb2.CommitmentPayload(
            outcome_positive=False,
            supersedes=core_pb2.CommitmentRef(
                session_id="prior-sess", commitment_hash=VECTOR_001_HASH
            ),
        )
        canonical_projection(payload)  # must not raise
        commitment_hash(payload)  # must not raise

    def test_absent_supersedes_is_unaffected(self):
        payload = core_pb2.CommitmentPayload(outcome_positive=False)
        canonical_projection(payload)  # must not raise
        commitment_hash(payload)  # must not raise
