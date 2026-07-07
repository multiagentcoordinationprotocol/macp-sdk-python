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
