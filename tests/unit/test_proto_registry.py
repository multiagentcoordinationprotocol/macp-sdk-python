"""Coverage for ProtoRegistry encode/decode round-trips (Q-7).

ProtoRegistry is the lookup-by-type-name adapter used by replay tools and
the observability layer to reconstruct typed payloads from raw envelope
bytes. These tests exercise:

- the known-type table (core + per-mode) resolves to real protobuf
  descriptors,
- ``encode_known_payload`` / ``decode_known_payload`` round-trip every
  mode's action payload,
- unknown mode / unknown message type raise ``ValueError``,
- multi-round ``Contribute`` encodes as canonical protobuf and decodes both
  proto and legacy-JSON bytes (JSON tried first),
- ``_try_decode_utf8`` handles empty, JSON, and non-JSON payloads.
"""

from __future__ import annotations

import base64
import json

import pytest

from macp_sdk.constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_MULTI_ROUND,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)
from macp_sdk.proto_registry import CORE_MAP, ProtoRegistry


@pytest.fixture
def registry() -> ProtoRegistry:
    return ProtoRegistry()


class TestKnownTypeLookup:
    def test_core_types_resolve(self, registry: ProtoRegistry):
        assert (
            registry.get_known_type_name("anything", "SessionStart")
            == "macp.v1.SessionStartPayload"
        )
        assert registry.get_known_type_name("anything", "Commitment") == "macp.v1.CommitmentPayload"

    def test_mode_types_resolve(self, registry: ProtoRegistry):
        assert (
            registry.get_known_type_name(MODE_DECISION, "Vote")
            == "macp.modes.decision.v1.VotePayload"
        )
        assert (
            registry.get_known_type_name(MODE_PROPOSAL, "Withdraw")
            == "macp.modes.proposal.v1.WithdrawPayload"
        )
        assert (
            registry.get_known_type_name(MODE_TASK, "TaskComplete")
            == "macp.modes.task.v1.TaskCompletePayload"
        )
        assert (
            registry.get_known_type_name(MODE_HANDOFF, "HandoffOffer")
            == "macp.modes.handoff.v1.HandoffOfferPayload"
        )
        assert (
            registry.get_known_type_name(MODE_QUORUM, "Abstain")
            == "macp.modes.quorum.v1.AbstainPayload"
        )

    def test_unknown_mode_falls_back_to_core_map(self, registry: ProtoRegistry):
        # Unknown mode + known core message → core map wins.
        assert registry.get_known_type_name("bogus", "SessionStart") == CORE_MAP["SessionStart"]

    def test_unknown_message_returns_none(self, registry: ProtoRegistry):
        assert registry.get_known_type_name(MODE_DECISION, "Bogus") is None
        assert registry.get_known_type_name("bogus", "Bogus") is None


class TestEncodeDecodeRoundTrip:
    @pytest.mark.parametrize(
        ("mode", "message_type", "value"),
        [
            (
                MODE_DECISION,
                "Proposal",
                {"proposal_id": "p1", "option": "deploy", "rationale": "ok"},
            ),
            (
                MODE_DECISION,
                "Vote",
                {"proposal_id": "p1", "vote": "APPROVE", "reason": "ship"},
            ),
            (
                MODE_PROPOSAL,
                "Proposal",
                {"proposal_id": "p1", "title": "Plan A", "summary": "x"},
            ),
            (
                MODE_TASK,
                "TaskRequest",
                {"task_id": "t1", "title": "Build", "instructions": "make"},
            ),
            (
                MODE_HANDOFF,
                "HandoffOffer",
                {"handoff_id": "h1", "target_participant": "bob", "scope": "svc"},
            ),
            (
                MODE_QUORUM,
                "ApprovalRequest",
                {"request_id": "r1", "action": "deploy", "required_approvals": 2},
            ),
            (
                "anything",
                "SessionStart",
                {
                    "intent": "decide",
                    "participants": ["alice", "bob"],
                    "ttl_ms": 1,
                },
            ),
        ],
    )
    def test_round_trip(self, registry: ProtoRegistry, mode: str, message_type: str, value: dict):
        payload = registry.encode_known_payload(mode, message_type, value)
        assert isinstance(payload, bytes) and len(payload) > 0
        out = registry.decode_known_payload(mode, message_type, payload)
        assert out is not None
        for key, expected in value.items():
            # json_format.MessageToDict renders int64 as a string (per protojson spec).
            # Normalise via str(...) so the round-trip assertion stays value-centric.
            got = out.get(key)
            if isinstance(expected, int) and not isinstance(expected, bool):
                assert got == expected or got == str(expected)
            else:
                assert got == expected


class TestErrorPaths:
    def test_encode_unknown_mapping_raises(self, registry: ProtoRegistry):
        with pytest.raises(ValueError, match="unknown payload mapping"):
            registry.encode_known_payload("bogus", "Bogus", {})

    def test_encode_unknown_mode_with_known_message(self, registry: ProtoRegistry):
        # Unknown mode + known core message ("Commitment") works via CORE_MAP.
        payload = registry.encode_known_payload(
            "bogus", "Commitment", {"action": "x", "reason": "y"}
        )
        assert payload

    def test_decode_unknown_returns_utf8_fallback(self, registry: ProtoRegistry):
        # Unknown mode/message → decode falls through to UTF-8 sniff.
        decoded = registry.decode_known_payload("bogus", "Bogus", b'{"foo": "bar"}')
        assert decoded == {"encoding": "json", "json": {"foo": "bar"}}


class TestMultiRoundContribute:
    """Runtime v0.5.0 / macp-proto >= 0.1.4: Contribute is canonical protobuf,
    with legacy JSON still decoded (tried first, permanently)."""

    def test_encode_produces_proto_bytes(self, registry: ProtoRegistry):
        from macp.modes.multi_round.v1 import multi_round_pb2

        payload = registry.encode_known_payload(MODE_MULTI_ROUND, "Contribute", {"value": "opt_a"})

        # Round-trips through the proto message (not JSON).
        msg = multi_round_pb2.ContributePayload()
        msg.ParseFromString(payload)
        assert msg.value == "opt_a"
        assert payload != json.dumps({"value": "opt_a"}).encode("utf-8")

    def test_decode_proto_bytes(self, registry: ProtoRegistry):
        payload = registry.encode_known_payload(MODE_MULTI_ROUND, "Contribute", {"value": "opt_a"})
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", payload)
        assert decoded == {"value": "opt_a"}

    def test_decode_legacy_json_still_works(self, registry: ProtoRegistry):
        # Pre-proto histories carry raw JSON bytes; the registry tries JSON
        # first and preserves the legacy ``{"encoding": "json", ...}`` shape.
        legacy = json.dumps({"value": "opt_a"}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", legacy)
        assert decoded == {"encoding": "json", "json": {"value": "opt_a"}}

    def test_decode_empty_returns_none(self, registry: ProtoRegistry):
        assert registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", b"") is None

    def test_decode_legacy_json_with_leading_whitespace(self, registry: ProtoRegistry):
        # issue #69: confirms this SDK does NOT use a first-byte shortcut
        # (0x7B vs 0x0A) to pick JSON vs. proto -- ``_decode_json_first_then_proto``
        # always attempts ``json.loads`` first, and the JSON spec (and Python's
        # parser) treat leading whitespace as insignificant, so this decodes
        # identically to the no-whitespace case rather than falling through to
        # proto (which would fail to parse and raise).
        legacy = b"   " + json.dumps({"value": "opt_a"}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", legacy)
        assert decoded == {"encoding": "json", "json": {"value": "opt_a"}}

    @pytest.mark.parametrize("non_string_value", [42, {"nested": "object"}, ["a", "list"], None])
    def test_decode_legacy_json_with_non_string_value_passes_through(
        self, registry: ProtoRegistry, non_string_value: object
    ):
        # issue #69: the registry is a generic decode adapter with no
        # ``Contribute``-specific schema of its own -- it hands back whatever
        # JSON was present under ``value`` uninterpreted (no type coercion, no
        # rejection). A non-string ``value`` here is a currently-open
        # cross-SDK/runtime acceptance question (should the *runtime* reject
        # this at admission?), not a decode-layer concern.
        legacy = json.dumps({"value": non_string_value}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", legacy)
        assert decoded == {"encoding": "json", "json": {"value": non_string_value}}

    @pytest.mark.parametrize("value_length", list(range(1, 128)))
    @pytest.mark.parametrize(
        "value_shape",
        [
            "digits_nonzero",
            "digits_zero",
            "leading_nonzero_digit",
            "json_object_shaped",
            "json_value_key_shaped",
        ],
    )
    def test_canonical_proto_round_trips_at_every_collision_length(
        self, registry: ProtoRegistry, value_length: int, value_shape: str
    ):
        # issue #69: the canonical proto tag byte (0x0A) is JSON whitespace,
        # so for specific payload *lengths* the length varint -- or, when the
        # varint is itself whitespace, the value's own leading byte -- becomes
        # the first significant character a JSON parser sees. Before the
        # ``_is_canonical_proto`` tie-break, a canonical ``ContributePayload``
        # at one of those lengths silently mis-decoded as a JSON number,
        # string, array, or object instead of the real string value. This
        # sweeps every length from 1 to 127 (the two-byte varint boundary --
        # length >= 128 is immune, since its first byte is not a valid UTF-8
        # start byte and the JSON attempt never gets that far) across five
        # value shapes chosen to hit both known collision mechanisms:
        # a significant length byte (34, 45, 48, 49-57, 91) and a whitespace
        # length byte that hands the opening character to the value itself
        # (9, 10, 13, 32, and the value-supplied ``{`` at 123).
        if value_shape == "digits_nonzero":
            value = "9" * value_length
        elif value_shape == "digits_zero":
            value = "0" * value_length
        elif value_shape == "leading_nonzero_digit":
            value = "1" + "2" * (value_length - 1)
        elif value_shape == "json_object_shaped":
            if value_length < 8:
                pytest.skip('shape needs >= 8 bytes for {"a":"..."}')
            value = '{"a":"' + "x" * (value_length - 8) + '"}'
        else:
            if value_length < 11:
                pytest.skip('shape needs >= 11 bytes for "value":"...."}')
            value = '"value":"' + "x" * (value_length - 11) + '"}'
        assert len(value) == value_length

        wire = registry.encode_known_payload(MODE_MULTI_ROUND, "Contribute", {"value": value})
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", wire)
        assert decoded == {"value": value}

    @pytest.mark.parametrize(
        "non_dict_json_payload",
        [b"null", b"0", b'"x"', b"[]", b"true"],
        ids=["null", "zero", "string", "empty-array", "true"],
    )
    def test_decode_non_dict_json_still_returns_safe_wrapper(
        self, registry: ProtoRegistry, non_dict_json_payload: bytes
    ):
        # issue #69: none of these bytes are a canonical proto encoding of
        # ContributePayload (each fails to round-trip through
        # ``_is_canonical_proto``), so they must keep decoding as the legacy
        # JSON wrapper -- exactly as they did before the collision fix. A
        # decoder that instead routed non-canonical-but-valid JSON through
        # ``decode_message`` would raise ``DecodeError`` here, which the sole
        # in-repo caller (``agent/transports.py``) does not safely absorb --
        # see ``tests/unit/test_agent_transports.py`` for that regression
        # test.
        decoded = registry.decode_known_payload(
            MODE_MULTI_ROUND, "Contribute", non_dict_json_payload
        )
        assert decoded == {
            "encoding": "json",
            "json": json.loads(non_dict_json_payload),
        }

    @pytest.mark.parametrize(
        ("prefix", "value_length"),
        [
            (b"\t", 29),
            (b" ", 108),
            (b"   ", 108),
        ],
        ids=["tab-prefix", "single-space-prefix", "three-space-prefix"],
    )
    def test_whitespace_prefixed_legacy_json_survives_canonicality_tie_break(
        self, registry: ProtoRegistry, prefix: bytes, value_length: int
    ):
        # issue #69 regression: an earlier version of the canonicality
        # tie-break (``_is_canonical_proto`` without ``DiscardUnknownFields``)
        # misclassified these exact (prefix, length) combinations as
        # canonical proto, because Python's protobuf runtime preserves
        # unknown fields verbatim through a parse/reserialize round-trip --
        # a byte-identical round-trip is not proof there are no unknown
        # fields. These three cases are real bytes that reproduced that
        # false positive before the fix (a `\t`/` `/`   ` prefix is not
        # proto field 1's tag byte 0x0A, so the tag byte itself is always
        # readable as an *unknown* field here). They must decode as legacy
        # JSON, not silently lose or corrupt the value.
        value = "z" * value_length
        legacy = prefix + json.dumps({"value": value}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", legacy)
        assert decoded == {"encoding": "json", "json": {"value": value}}

    def test_newline_prefixed_legacy_json_collision_is_a_documented_residual(
        self, registry: ProtoRegistry
    ):
        # issue #69: unlike other JSON whitespace bytes, a literal 0x0A
        # (``\n``) prefix is *itself* proto field 1's tag byte -- there is
        # no unknown field for ``DiscardUnknownFields`` to strip, because
        # the remaining bytes here form a complete, well-formed field-1
        # string with nothing left over. This specific (prefix, length)
        # combination is therefore genuinely, symmetrically ambiguous: it
        # is simultaneously valid JSON (``{"value": "z"*111}``) and the
        # exact canonical proto encoding of some string. No JSON-first
        # strategy can tell these apart from the bytes alone -- this
        # mirrors the already-known forward-direction residual (a real
        # Contribute value of length 123 collides because its length byte
        # is ``0x7B`` = ``{``), just entered from the opposite direction.
        # Characterized here, not fixed, so a future change to this
        # behavior is a conscious act, not a silent regression. A real
        # legacy-JSON sender would need to deliberately prefix with a
        # literal newline byte for this to matter -- ``json.dumps`` never
        # emits one, and the SDK's own pinned whitespace test
        # (``test_decode_legacy_json_with_leading_whitespace``) uses spaces.
        value = "z" * 111
        legacy = b"\n" + json.dumps({"value": value}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", legacy)
        assert decoded != {"encoding": "json", "json": {"value": value}}
        assert decoded == {"value": legacy[2:].decode("utf-8")}

    def test_canonical_proto_length_123_collision_is_resolved_toward_proto(
        self, registry: ProtoRegistry
    ):
        # issue #69: the forward-direction counterpart of the residual
        # above. A genuine Contribute value of length 123 makes the length
        # varint byte 0x7B = ``{``, so the wire bytes also parse as a JSON
        # object (``{"value": "xxx...xxx"}``-shaped). Unlike the reverse
        # residual, this direction *is* fully resolved by the canonicality
        # tie-break: the wire bytes are the exact canonical proto encoding
        # (no unknown fields to begin with), so they correctly decode as
        # the real proto value rather than the coincidental JSON reading.
        value = '"value":"' + "x" * (123 - 11) + '"}'
        assert len(value) == 123
        wire = registry.encode_known_payload(MODE_MULTI_ROUND, "Contribute", {"value": value})
        assert wire[0:1] == b"\n" and wire[1] == 123
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", wire)
        assert decoded == {"value": value}


class TestTryDecodeUtf8:
    def test_empty_payload_returns_none(self):
        assert ProtoRegistry._try_decode_utf8(b"") is None

    def test_valid_json_payload(self):
        assert ProtoRegistry._try_decode_utf8(b'{"x": 1}') == {
            "encoding": "json",
            "json": {"x": 1},
        }

    def test_plain_text_payload_becomes_text_plus_base64(self):
        result = ProtoRegistry._try_decode_utf8(b"not-json")
        assert result is not None
        assert result["encoding"] == "text"
        assert result["text"] == "not-json"
        assert base64.b64decode(result["payload_base64"]) == b"not-json"

    def test_invalid_utf8_payload_raises(self):
        # Random high-bit bytes aren't valid UTF-8 and aren't text.
        with pytest.raises(UnicodeDecodeError):
            ProtoRegistry._try_decode_utf8(b"\xff\xfe\xfd")
