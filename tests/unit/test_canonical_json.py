"""Canonical JSON envelope codec (RFC-MACP-0001 §10, Wave 6 S6.1)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import symbol_database
from google.protobuf.descriptor import FieldDescriptor
from macp.modes.decision.v1 import decision_pb2
from macp.modes.multi_round.v1 import multi_round_pb2
from macp.modes.quorum.v1 import quorum_pb2
from macp.modes.task.v1 import task_pb2
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk import (
    INVALID_ENVELOPE,
    CanonicalJsonError,
    MacpSdkError,
    ack_from_canonical_json,
    ack_to_canonical_json,
    envelope_from_canonical_json,
    envelope_to_canonical_json,
)
from macp_sdk.canonical_json import MAX_SAFE_INTEGER
from macp_sdk.proto_registry import ProtoRegistry

ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "6f1c2a9e-4b7d-4c1e-9a53-2d8f0b7e5a10"
BASE_MS = 1761382800000  # 2025-10-25T09:00:00Z


def _envelope(
    message_type: str,
    payload: bytes,
    *,
    mode: str = "macp.mode.decision.v1",
    timestamp_unix_ms: int = BASE_MS,
) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=mode,
        message_type=message_type,
        message_id="m-1",
        session_id=SESSION_ID,
        sender="agent://a",
        timestamp_unix_ms=timestamp_unix_ms,
        payload=payload,
    )


def _proposal() -> envelope_pb2.Envelope:
    return _envelope(
        "Proposal",
        decision_pb2.ProposalPayload(proposal_id="p1", option="ship").SerializeToString(),
    )


# ── vendored conformance fixtures: every message round-trips ─────────


def _fixture_payload(payload_type: str, data: dict[str, Any]) -> bytes:
    # Fixture format (RFC-MACP-0001 §10.14): bytes are plain text, or [] for empty.
    ProtoRegistry()
    cls = symbol_database.Default().GetSymbol(payload_type)
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        field = cls.DESCRIPTOR.fields_by_name[name]
        if field.type == FieldDescriptor.TYPE_BYTES and not field.is_repeated:
            value = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        kwargs[name] = value
    return bytes(cls(**kwargs).SerializeToString(deterministic=True))


def _fixture_envelopes() -> list[Any]:
    cases = []
    for path in sorted((ROOT / "conformance").glob("*.json")):
        fixture = json.loads(path.read_text())
        if "mode" not in fixture:
            continue
        for index, msg in enumerate(fixture["messages"]):
            env = envelope_pb2.Envelope(
                macp_version="1.0",
                mode=fixture["mode"],
                message_type=msg["message_type"],
                message_id=f"{path.stem}-{index}",
                session_id=SESSION_ID,
                sender=msg["sender"],
                timestamp_unix_ms=BASE_MS + index * 1001,
                payload=_fixture_payload(msg["payload_type"], msg["payload"]),
            )
            cases.append(pytest.param(env, id=f"{path.stem}-{index}"))
    return cases


FIXTURE_ENVELOPES = _fixture_envelopes()


def test_fixture_corpus_is_non_trivial() -> None:
    assert len(FIXTURE_ENVELOPES) > 100
    modes = {p.values[0].mode for p in FIXTURE_ENVELOPES}
    assert "ext.multi_round.v1" in modes and "macp.mode.decision.v1" in modes


@pytest.mark.parametrize("env", FIXTURE_ENVELOPES)
def test_every_fixture_envelope_round_trips(env: envelope_pb2.Envelope) -> None:
    encoded = envelope_to_canonical_json(env)
    # Through real JSON text, as a transport would carry it.
    decoded = envelope_from_canonical_json(json.loads(json.dumps(encoded)))
    assert decoded.SerializeToString(deterministic=True) == env.SerializeToString(
        deterministic=True
    )
    core = env.message_type in _CORE_DESCRIPTORS
    if env.mode.startswith("ext.") and not core:
        assert "payload" not in encoded
        assert base64.b64decode(encoded["payload_b64"]) == env.payload
    else:
        assert "payload_b64" not in encoded and isinstance(encoded["payload"], dict)


# ── the A2A example's embedded envelopes ─────────────────────────────


def _example_envelopes() -> list[Any]:
    doc = json.loads((ROOT / "fixtures" / "a2a_binding_session.json").read_text())
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("mediaType") == "application/macp-envelope+json" and "data" in node:
                found.append(node["data"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return [pytest.param(e, id=f"{i}-{e['message_type']}") for i, e in enumerate(found)]


EXAMPLE_ENVELOPES = _example_envelopes()

_CORE_DESCRIPTORS = {
    "Signal": core_pb2.SignalPayload.DESCRIPTOR,
    "Progress": core_pb2.ProgressPayload.DESCRIPTOR,
    "SessionStart": core_pb2.SessionStartPayload.DESCRIPTOR,
    "SessionCancel": core_pb2.SessionCancelPayload.DESCRIPTOR,
    "SessionSuspend": core_pb2.SessionSuspendPayload.DESCRIPTOR,
    "SessionResume": core_pb2.SessionResumePayload.DESCRIPTOR,
    "Commitment": core_pb2.CommitmentPayload.DESCRIPTOR,
}


def _proto3_default(field: FieldDescriptor) -> Any:
    """The canonical JSON a field without presence carries when unset, or a
    sentinel for a field with presence (which must never be added)."""
    if _is_map_field(field):
        return {}
    if field.is_repeated:
        return []
    if field.has_presence:
        return _PRESENCE
    if field.type == FieldDescriptor.TYPE_ENUM:
        return field.enum_type.values_by_number[0].name
    if field.type in (FieldDescriptor.TYPE_STRING, FieldDescriptor.TYPE_BYTES):
        return ""
    if field.type == FieldDescriptor.TYPE_BOOL:
        return False
    return 0


_PRESENCE = object()


def _is_map_field(field: FieldDescriptor) -> bool:
    return bool(
        field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.GetOptions().map_entry
    )


def _assert_matches_example(
    ours: dict[str, Any], example: dict[str, Any], desc: Any, path: str = ""
) -> None:
    # The example omits defaults; we emit them (§10.10). Every key it has must
    # match exactly, and every key only we have must be a field without
    # presence holding exactly its proto3 default.
    for key, value in example.items():
        assert key in ours, f"{path}{key} missing"
        field = desc.fields_by_name[key]
        if isinstance(value, dict) and not _is_map_field(field):
            _assert_matches_example(ours[key], value, field.message_type, f"{path}{key}.")
        else:
            assert ours[key] == value, f"{path}{key}: {ours[key]!r} != {value!r}"
    for key in ours.keys() - example.keys():
        expected = _proto3_default(desc.fields_by_name[key])
        assert expected is not _PRESENCE, f"{path}{key} has presence but was added"
        assert ours[key] == expected, f"{path}{key} = {ours[key]!r}, default {expected!r}"


def test_example_has_envelopes() -> None:
    assert len(EXAMPLE_ENVELOPES) >= 10
    assert {p.values[0]["message_type"] for p in EXAMPLE_ENVELOPES} >= {
        "SessionStart",
        "Proposal",
        "Vote",
        "Commitment",
        "Signal",
    }


@pytest.mark.parametrize("example", EXAMPLE_ENVELOPES)
def test_example_envelope_encodes_to_the_same_json(example: dict[str, Any]) -> None:
    env = envelope_from_canonical_json(example)
    ours = envelope_to_canonical_json(env)
    for key in ("macp_version", "mode", "message_type", "message_id", "session_id", "sender"):
        assert ours[key] == example.get(key, "")
    assert ours["timestamp"] == example["timestamp"]
    form = "payload" if "payload" in example else "payload_b64"
    assert form in ours
    if form == "payload":
        ProtoRegistry()
        cls_name = ProtoRegistry().get_known_type_name(env.mode, env.message_type)
        desc = (
            symbol_database.Default().GetSymbol(cls_name).DESCRIPTOR
            if cls_name
            else _CORE_DESCRIPTORS[env.message_type]
        )
        _assert_matches_example(ours["payload"], example["payload"], desc)
    else:
        assert ours["payload_b64"] == example["payload_b64"]
    # Decoding our own output is a fixed point, at the JSON and at the byte level.
    again = envelope_from_canonical_json(json.loads(json.dumps(ours)))
    assert envelope_to_canonical_json(again) == ours
    assert again.SerializeToString() == env.SerializeToString()


# ── payload form ─────────────────────────────────────────────────────


def test_signal_with_empty_mode_is_core_payload() -> None:
    signal = core_pb2.SignalPayload(signal_type="risk", data=b'{"score":0.12}', confidence=0.5)
    env = _envelope("Signal", signal.SerializeToString(), mode="")
    env.session_id = ""
    encoded = envelope_to_canonical_json(env)
    assert encoded["mode"] == "" and encoded["session_id"] == ""
    assert encoded["payload"] == {
        "signal_type": "risk",
        "data": "eyJzY29yZSI6MC4xMn0=",
        "confidence": 0.5,
        "correlation_session_id": "",
    }
    assert envelope_from_canonical_json(encoded) == env


def test_commitment_in_an_extension_mode_is_core_payload() -> None:
    commitment = core_pb2.CommitmentPayload(commitment_id="c1", action="multi_round.converged")
    env = _envelope("Commitment", commitment.SerializeToString(), mode="ext.multi_round.v1")
    encoded = envelope_to_canonical_json(env)
    assert encoded["payload"]["commitment_id"] == "c1"
    assert envelope_from_canonical_json(encoded) == env


@pytest.mark.parametrize("message_type", ["SessionCancel", "SessionSuspend", "SessionResume"])
def test_lifecycle_types_are_core_payloads(message_type: str) -> None:
    payload = getattr(core_pb2, f"{message_type}Payload")(reason="r").SerializeToString()
    encoded = envelope_to_canonical_json(_envelope(message_type, payload, mode="ext.x.v1"))
    assert encoded["payload"]["reason"] == "r"


def test_extension_mode_payload_is_always_b64() -> None:
    contribute = multi_round_pb2.ContributePayload(value="A").SerializeToString()
    encoded = envelope_to_canonical_json(
        _envelope("Contribute", contribute, mode="ext.multi_round.v1")
    )
    assert encoded["payload_b64"] == base64.b64encode(contribute).decode()
    assert "payload" not in encoded


def test_reverse_domain_mode_payload_is_b64() -> None:
    encoded = envelope_to_canonical_json(
        _envelope("Bid", b"\x0a\x01x", mode="com.example.auction.v1")
    )
    assert encoded["payload_b64"] == "CgF4"


def test_standard_mode_unknown_message_type_is_b64() -> None:
    encoded = envelope_to_canonical_json(_envelope("Nonsense", b"\x01\x02"))
    assert encoded["payload_b64"] == "AQI="


def test_custom_registry_is_consulted_for_standard_modes() -> None:
    class NoDecision(ProtoRegistry):
        def get_known_type_name(self, mode: str, message_type: str) -> str | None:
            return None

    env = _proposal()
    assert "payload" in envelope_to_canonical_json(env)
    assert "payload_b64" in envelope_to_canonical_json(env, registry=NoDecision())
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(envelope_to_canonical_json(env), registry=NoDecision())


def test_defaults_are_emitted_in_field_number_order() -> None:
    encoded = envelope_to_canonical_json(_proposal())
    assert list(encoded) == [
        "macp_version",
        "mode",
        "message_type",
        "message_id",
        "session_id",
        "sender",
        "timestamp",
        "payload",
    ]
    assert encoded["payload"] == {
        "proposal_id": "p1",
        "option": "ship",
        "rationale": "",
        "supporting_data": "",
    }


def test_undecodable_core_payload_raises_on_encode() -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_to_canonical_json(_envelope("SessionStart", b"\xff\xff\xff"))


# ── payload_b64 alphabet ─────────────────────────────────────────────


def test_payload_b64_is_written_standard_and_padded() -> None:
    encoded = envelope_to_canonical_json(_envelope("Bid", b"\xfb\xff", mode="ext.x.v1"))
    assert encoded["payload_b64"] == "+/8="


@pytest.mark.parametrize("text", ["+/8=", "+/8", "-_8=", "-_8"])
def test_payload_b64_decode_accepts_url_safe_and_unpadded(text: str) -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"", mode="ext.x.v1"))
    obj["payload_b64"] = text
    assert envelope_from_canonical_json(obj).payload == b"\xfb\xff"


@pytest.mark.parametrize("text", ["+/8==x", "a", "ab!c", "a=b=", "abc==="])
def test_bad_base64_is_rejected(text: str) -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"", mode="ext.x.v1"))
    obj["payload_b64"] = text
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


# ── timestamp ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("unix_ms", "text"),
    [
        (BASE_MS, "2025-10-25T09:00:00Z"),
        (BASE_MS + 5, "2025-10-25T09:00:00.005Z"),
        (BASE_MS + 120, "2025-10-25T09:00:00.120Z"),
        (0, "1970-01-01T00:00:00Z"),
        (-1, "1969-12-31T23:59:59.999Z"),
        (-62135596800000, "0001-01-01T00:00:00Z"),
        (253402300799999, "9999-12-31T23:59:59.999Z"),
    ],
)
def test_timestamp_format(unix_ms: int, text: str) -> None:
    encoded = envelope_to_canonical_json(
        _envelope("Bid", b"", mode="ext.x.v1", timestamp_unix_ms=unix_ms)
    )
    assert encoded["timestamp"] == text
    assert envelope_from_canonical_json(encoded).timestamp_unix_ms == unix_ms


@pytest.mark.parametrize("unix_ms", [-62135596800001, 253402300800000])
def test_timestamp_outside_rfc3339_years_raises_on_encode(unix_ms: int) -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_to_canonical_json(
            _envelope("Bid", b"", mode="ext.x.v1", timestamp_unix_ms=unix_ms)
        )


@pytest.mark.parametrize(
    ("text", "unix_ms"),
    [
        ("2025-10-25T09:00:00Z", BASE_MS),
        ("2025-10-25t09:00:00z", BASE_MS),
        ("2025-10-25T11:00:00+02:00", BASE_MS),
        ("2025-10-25T04:30:00-04:30", BASE_MS),
        ("2025-10-25T09:00:00.1Z", BASE_MS + 100),
        ("2025-10-25T09:00:00.123456789Z", BASE_MS + 123),
        ("2025-10-25T09:00:00.999999Z", BASE_MS + 999),
    ],
)
def test_timestamp_decode_accepts_any_rfc3339(text: str, unix_ms: int) -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"", mode="ext.x.v1"))
    obj["timestamp"] = text
    assert envelope_from_canonical_json(obj).timestamp_unix_ms == unix_ms


@pytest.mark.parametrize(
    "text",
    [
        "2025-10-25 09:00:00Z",
        "2025-10-25T09:00:00",
        "2025-10-25T09:00Z",
        "2025-10-25",
        "2025-13-01T00:00:00Z",
        "2025-02-30T00:00:00Z",
        "2025-10-25T23:59:60Z",
        "2025-10-25T09:00:00+24:00",
        "2025-10-25T09:00:00.Z",
        "not a time",
        "",
    ],
)
def test_bad_timestamp_is_rejected(text: str) -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"", mode="ext.x.v1"))
    obj["timestamp"] = text
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_non_string_timestamp_is_rejected() -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"", mode="ext.x.v1"))
    obj["timestamp"] = BASE_MS
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


# ── int64 ────────────────────────────────────────────────────────────


def _start(ttl_ms: int) -> envelope_pb2.Envelope:
    return _envelope(
        "SessionStart", core_pb2.SessionStartPayload(ttl_ms=ttl_ms).SerializeToString()
    )


def test_int64_is_a_number_up_to_2_53_and_quoted_above() -> None:
    assert (
        envelope_to_canonical_json(_start(MAX_SAFE_INTEGER))["payload"]["ttl_ms"]
        == MAX_SAFE_INTEGER
    )
    big = envelope_to_canonical_json(_start(MAX_SAFE_INTEGER + 1))
    assert big["payload"]["ttl_ms"] == str(MAX_SAFE_INTEGER + 1)
    assert big["payload"]["max_suspend_ms"] == 0
    assert envelope_from_canonical_json(big) == _start(MAX_SAFE_INTEGER + 1)


@pytest.mark.parametrize("value", [60000, "60000"])
def test_int64_decode_accepts_number_and_quoted(value: Any) -> None:
    obj = envelope_to_canonical_json(_start(1))
    obj["payload"]["ttl_ms"] = value
    assert envelope_from_canonical_json(obj) == _start(60000)


@pytest.mark.parametrize("value", [MAX_SAFE_INTEGER + 1, 1.5, "1e3", "0x10", " 5", True])
def test_bad_int64_is_rejected(value: Any) -> None:
    obj = envelope_to_canonical_json(_start(1))
    obj["payload"]["ttl_ms"] = value
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_int64_deep_in_a_mode_payload() -> None:
    request = task_pb2.TaskRequestPayload(task_id="t", deadline_unix_ms=BASE_MS)
    env = _envelope("TaskRequest", request.SerializeToString(), mode="macp.mode.task.v1")
    encoded = envelope_to_canonical_json(env)
    assert encoded["payload"]["deadline_unix_ms"] == BASE_MS
    assert envelope_from_canonical_json(encoded) == env


# ── envelope rejects and leniency ────────────────────────────────────


def test_both_payload_forms_are_rejected() -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj["payload_b64"] = ""
    with pytest.raises(CanonicalJsonError, match="both"):
        envelope_from_canonical_json(obj)


def test_neither_payload_form_is_rejected() -> None:
    obj = envelope_to_canonical_json(_proposal())
    del obj["payload"]
    with pytest.raises(CanonicalJsonError, match="required"):
        envelope_from_canonical_json(obj)


def test_null_counts_as_absent() -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"x", mode="ext.x.v1"))
    obj["payload"] = None
    obj["session_id"] = None
    env = envelope_from_canonical_json(obj)
    assert env.payload == b"x" and env.session_id == ""


@pytest.mark.parametrize("mode", ["com.example.auction.v1", "ext.multi_round.v1"])
def test_object_payload_without_a_schema_is_rejected(mode: str) -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj["mode"], obj["message_type"] = mode, "Contribute"
    with pytest.raises(CanonicalJsonError, match="no payload schema"):
        envelope_from_canonical_json(obj)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("payload", "not an object"),
        ("payload", []),
        ("mode", 5),
        ("sender", ["a"]),
    ],
)
def test_wrong_json_types_are_rejected(key: str, value: Any) -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj[key] = value
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_non_string_payload_b64_is_rejected() -> None:
    obj = envelope_to_canonical_json(_envelope("Bid", b"x", mode="ext.x.v1"))
    obj["payload_b64"] = 7
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_payload_with_wrong_field_type_is_rejected() -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj["payload"]["option"] = 5
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_null_inside_a_repeated_field_is_rejected() -> None:
    obj = envelope_to_canonical_json(_start(1))
    obj["payload"]["participants"] = ["agent://a", None]
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_wrong_map_and_array_shapes_are_rejected() -> None:
    for key, value in (("extensions", ["x"]), ("participants", "agent://a")):
        obj = envelope_to_canonical_json(_start(1))
        obj["payload"][key] = value
        with pytest.raises(CanonicalJsonError):
            envelope_from_canonical_json(obj)


def test_non_object_envelope_is_rejected() -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json([])  # type: ignore[arg-type]


def test_camel_case_and_unknown_keys_are_ignored() -> None:
    obj = envelope_to_canonical_json(_start(1))
    obj["sessionId"] = "smuggled"
    obj["future_field"] = {"x": 1}
    obj["payload"]["ttlMs"] = 999
    obj["payload"]["intent_v2"] = "x"
    env = envelope_from_canonical_json(obj)
    assert env.session_id == SESSION_ID
    assert env == _start(1)


def test_lower_camel_case_alone_does_not_fill_the_field() -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj["payload"] = {"proposalId": "p1", "option": "ship"}
    decoded = decision_pb2.ProposalPayload.FromString(envelope_from_canonical_json(obj).payload)
    assert decoded.proposal_id == "" and decoded.option == "ship"


def test_missing_strings_and_timestamp_default() -> None:
    env = envelope_from_canonical_json(
        {"message_type": "Bid", "mode": "ext.x.v1", "payload_b64": ""}
    )
    assert env.session_id == "" and env.macp_version == "" and env.timestamp_unix_ms == 0


def test_payload_bytes_field_accepts_url_safe_base64() -> None:
    obj = envelope_to_canonical_json(_envelope("Signal", b"", mode=""))
    obj["payload"]["data"] = "-_8"
    payload = envelope_from_canonical_json(obj).payload
    assert core_pb2.SignalPayload.FromString(payload).data == b"\xfb\xff"


def test_payload_decodes_to_deterministic_bytes() -> None:
    obj = envelope_to_canonical_json(_start(1))
    obj["payload"]["extensions"] = {"z": "AQ==", "a": "Ag=="}
    first = envelope_from_canonical_json(obj).payload
    obj["payload"]["extensions"] = {"a": "Ag==", "z": "AQ=="}
    assert envelope_from_canonical_json(obj).payload == first


# ── Ack ──────────────────────────────────────────────────────────────


def test_ack_emits_every_field_and_omits_unset_error() -> None:
    ack = envelope_pb2.Ack(
        ok=True,
        message_id="m-1",
        session_id=SESSION_ID,
        accepted_at_unix_ms=BASE_MS,
        session_state=envelope_pb2.SESSION_STATE_OPEN,
    )
    encoded = ack_to_canonical_json(ack)
    assert encoded == {
        "ok": True,
        "duplicate": False,
        "message_id": "m-1",
        "session_id": SESSION_ID,
        "accepted_at_unix_ms": BASE_MS,
        "session_state": "SESSION_STATE_OPEN",
    }
    assert ack_from_canonical_json(json.loads(json.dumps(encoded))) == ack


def test_ack_with_error_round_trips() -> None:
    ack = envelope_pb2.Ack(
        ok=False,
        message_id="m-1",
        error=envelope_pb2.MACPError(code="POLICY_DENIED", message="no", details=b"\x01\x02"),
    )
    encoded = ack_to_canonical_json(ack)
    assert encoded["error"] == {
        "code": "POLICY_DENIED",
        "message": "no",
        "session_id": "",
        "message_id": "",
        "details": "AQI=",
    }
    assert ack_from_canonical_json(encoded) == ack


def test_ack_large_accepted_at_is_quoted() -> None:
    encoded = ack_to_canonical_json(envelope_pb2.Ack(accepted_at_unix_ms=2**60))
    assert encoded["accepted_at_unix_ms"] == str(2**60)
    assert ack_from_canonical_json(encoded).accepted_at_unix_ms == 2**60


def test_ack_enum_accepts_integer_and_ignores_unknown_name() -> None:
    assert ack_from_canonical_json({"session_state": 2}).session_state == 2
    assert ack_from_canonical_json({"session_state": "SESSION_STATE_FUTURE"}).session_state == 0


def test_ack_unknown_session_state_integer_decodes_as_unspecified() -> None:
    # macp-sdk-typescript's SessionState is a closed union; both SDKs agree.
    ack = ack_from_canonical_json({"session_state": 99})
    assert ack.session_state == envelope_pb2.SESSION_STATE_UNSPECIFIED
    assert ack_to_canonical_json(ack)["session_state"] == "SESSION_STATE_UNSPECIFIED"


@pytest.mark.parametrize("value", [2**40 + 3, 2**31, 2.5, True, "2", " 2", "1_0", [], {}])
def test_ack_bad_enum_value_is_rejected(value: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        ack_from_canonical_json({"session_state": value})


def test_ack_integral_float_enum_is_accepted() -> None:
    # JSON.parse cannot tell 2.0 from 2, so neither may we.
    assert ack_from_canonical_json({"session_state": 2.0}).session_state == 2


@pytest.mark.parametrize("obj", [[], {"ok": "yes"}, {"accepted_at_unix_ms": 1.5}, {"error": "x"}])
def test_bad_ack_is_rejected(obj: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        ack_from_canonical_json(obj)


def test_error_class() -> None:
    err = CanonicalJsonError("x")
    assert isinstance(err, MacpSdkError) and isinstance(err, ValueError)
    assert err.code == INVALID_ENVELOPE


def test_example_comparison_rejects_a_wrong_default_and_an_added_presence_field() -> None:
    desc = core_pb2.SignalPayload.DESCRIPTOR
    with pytest.raises(AssertionError):
        _assert_matches_example({"signal_type": "x"}, {}, desc)
    with pytest.raises(AssertionError, match="presence"):
        _assert_matches_example({"supersedes": {}}, {}, core_pb2.CommitmentPayload.DESCRIPTOR)


# ── §10.13 wire encoding ─────────────────────────────────────────────


@pytest.mark.parametrize("env", FIXTURE_ENVELOPES)
def test_fixture_json_payload_encodes_to_protobuf_bytes(env: envelope_pb2.Envelope) -> None:
    # Without maps, the §10.13 bytes equal protobuf's own serialization.
    encoded = envelope_to_canonical_json(env)
    if "payload" in encoded:
        assert envelope_from_canonical_json(encoded).payload == env.payload


def test_map_entries_sort_by_utf8_bytes_and_always_carry_key_and_value() -> None:
    obj = envelope_to_canonical_json(_start(0))
    obj["payload"]["extensions"] = {"b": "", "ab": "Ag==", "a": "AQ==", "\u00e9": "", "": ""}
    payload = envelope_from_canonical_json(obj).payload

    def entry(key: bytes, value: bytes) -> bytes:
        body = b"\x0a" + bytes([len(key)]) + key + b"\x12" + bytes([len(value)]) + value
        return b"\x4a" + bytes([len(body)]) + body

    assert payload == (
        entry(b"", b"")
        + entry(b"a", b"\x01")
        + entry(b"ab", b"\x02")
        + entry(b"b", b"")
        + entry("\u00e9".encode(), b"")
    )


def test_negative_zero_is_emitted_as_the_string_minus_zero() -> None:
    signal = core_pb2.SignalPayload(signal_type="s", confidence=-0.0)
    encoded = envelope_to_canonical_json(_envelope("Signal", signal.SerializeToString()))
    assert encoded["payload"]["confidence"] == "-0"
    decoded = envelope_from_canonical_json(encoded)
    assert decoded.payload == signal.SerializeToString()  # -0.0 is written (rule 2)
    encoded["payload"]["confidence"] = -0.0
    assert envelope_from_canonical_json(encoded).payload == decoded.payload


def test_nan_decodes_to_the_canonical_quiet_nan() -> None:
    obj = envelope_to_canonical_json(_envelope("Signal", b""))
    obj["payload"]["confidence"] = "NaN"
    payload = envelope_from_canonical_json(obj).payload
    assert payload.endswith(b"\x00\x00\x00\x00\x00\x00\xf8\x7f")


def test_varint_overflowing_64_bits_is_refused() -> None:
    overflow = b"\x30" + b"\xff" * 9 + b"\x02"  # ttl_ms, 10th byte past bit 63
    with pytest.raises(CanonicalJsonError):
        envelope_to_canonical_json(_envelope("SessionStart", overflow))
    largest = b"\x30" + b"\xff" * 9 + b"\x01"
    assert envelope_to_canonical_json(_envelope("SessionStart", largest))["payload"]["ttl_ms"] == -1


def test_unknown_group_is_skipped_and_a_stray_end_group_refused() -> None:
    known = core_pb2.SessionStartPayload(intent="x").SerializeToString()
    group = b"\x7b\x08\x01\x7c"  # field 15 start-group, varint, end-group
    encoded = envelope_to_canonical_json(_envelope("SessionStart", group + known))
    assert encoded["payload"]["intent"] == "x"
    with pytest.raises(CanonicalJsonError):
        envelope_to_canonical_json(_envelope("SessionStart", b"\x7c"))


# ── payload_encoding ─────────────────────────────────────────────────


def test_payload_encoding_b64_forces_payload_b64_for_core_and_known_types() -> None:
    signal = _envelope("Signal", core_pb2.SignalPayload(signal_type="s").SerializeToString())
    for env in (signal, _proposal()):
        encoded = envelope_to_canonical_json(env, payload_encoding="b64")
        assert "payload" not in encoded
        assert base64.b64decode(encoded["payload_b64"]) == env.payload
        assert envelope_from_canonical_json(encoded) == env


def test_payload_encoding_b64_keeps_bytes_that_do_not_parse() -> None:
    env = _envelope("Commitment", b"\xff\xff")
    with pytest.raises(CanonicalJsonError):
        envelope_to_canonical_json(env)
    encoded = envelope_to_canonical_json(env, payload_encoding="b64")
    assert envelope_from_canonical_json(encoded).payload == b"\xff\xff"


def test_unknown_payload_encoding_is_refused() -> None:
    with pytest.raises(CanonicalJsonError, match="payload_encoding"):
        envelope_to_canonical_json(_proposal(), payload_encoding="json")  # type: ignore[arg-type]


# ── strict scalar types inside payload ───────────────────────────────


def _signal_with(**payload: Any) -> dict[str, Any]:
    obj = envelope_to_canonical_json(_envelope("Signal", b""))
    obj["payload"].update(payload)
    return obj


@pytest.mark.parametrize("value", ["!!!!", "\u00ff\u00ff", "AQ==AQ==", " AQ==", "AQ==\n", 5])
def test_bytes_field_with_invalid_base64_is_refused(value: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(_signal_with(data=value))
    obj = envelope_to_canonical_json(_start(0))
    obj["payload"]["extensions"] = {"k": value}
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_bytes_field_accepts_url_safe_unpadded_base64() -> None:
    payload = envelope_from_canonical_json(_signal_with(data="-_8")).payload
    assert core_pb2.SignalPayload.FromString(payload).data == b"\xfb\xff"


@pytest.mark.parametrize(
    "value", [True, " 1.5", "1_0", "inf", "infinity", "1e400", "\u0663", 1e400, 10**400, [], "nan"]
)
def test_double_with_a_non_json_number_is_refused(value: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(_signal_with(confidence=value))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.5", 1.5), ("-2e-1", -0.2), (3, 3.0), ("Infinity", float("inf"))],
)
def test_double_accepts_numbers_and_numeric_strings(value: Any, expected: float) -> None:
    payload = envelope_from_canonical_json(_signal_with(confidence=value)).payload
    assert core_pb2.SignalPayload.FromString(payload).confidence == expected


@pytest.mark.parametrize("value", ["1_0", "+5", "\u0665", " 5", True, 2.5, 2**32, -1, "-1"])
def test_uint32_with_a_loose_or_out_of_range_value_is_refused(value: Any) -> None:
    obj = envelope_to_canonical_json(
        _envelope(
            "ApprovalRequest",
            quorum_pb2.ApprovalRequestPayload(request_id="r").SerializeToString(),
            mode="macp.mode.quorum.v1",
        )
    )
    obj["payload"]["required_approvals"] = value
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_integral_float_is_accepted_for_integers() -> None:
    obj = envelope_to_canonical_json(_start(0))
    obj["payload"]["ttl_ms"] = 1e3
    payload = envelope_from_canonical_json(obj).payload
    assert core_pb2.SessionStartPayload.FromString(payload).ttl_ms == 1000


@pytest.mark.parametrize("value", [1, True, {}])
def test_string_and_bool_fields_need_their_json_type(value: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(_signal_with(signal_type=value))
    with pytest.raises(CanonicalJsonError):
        ack_from_canonical_json({"ok": 1 if value is True else value})


def test_lone_surrogate_is_refused_in_strings_and_map_keys() -> None:
    with pytest.raises(CanonicalJsonError, match="surrogate"):
        envelope_from_canonical_json(_signal_with(signal_type="\ud800"))
    obj = envelope_to_canonical_json(_start(0))
    obj["payload"]["extensions"] = {"\udc00": ""}
    with pytest.raises(CanonicalJsonError, match="surrogate"):
        envelope_from_canonical_json(obj)
    bad = envelope_to_canonical_json(_proposal())
    bad["sender"] = "\ud800"
    with pytest.raises(CanonicalJsonError, match="surrogate"):
        envelope_from_canonical_json(bad)


@pytest.mark.parametrize(
    "text",
    [
        "\u0662\u0660\u0662\u0665-10-25T09:00:00Z",
        "2025-10-25T09:00:00.\uff11Z",
        "2025-10-25T09:00:00+\u0660\u0661:00",
        "0001-01-01T00:30:00+01:00",
        "9999-12-31T23:30:00-01:00",
    ],
)
def test_timestamp_with_non_ascii_digits_or_outside_the_year_range_is_refused(text: str) -> None:
    obj = envelope_to_canonical_json(_proposal())
    obj["timestamp"] = text
    with pytest.raises(CanonicalJsonError):
        envelope_from_canonical_json(obj)


def test_registry_naming_an_unloaded_type_raises_canonical_json_error() -> None:
    class Broken(ProtoRegistry):
        def get_known_type_name(self, mode: str, message_type: str) -> str | None:
            return "macp.nowhere.v1.Missing"

    with pytest.raises(CanonicalJsonError, match="not a loaded message type"):
        envelope_to_canonical_json(_proposal(), registry=Broken())


@pytest.mark.parametrize("value", [str(2**63), str(-(2**63) - 1)])
def test_quoted_int64_out_of_range_is_refused(value: str) -> None:
    obj = envelope_to_canonical_json(_start(0))
    obj["payload"]["ttl_ms"] = value
    with pytest.raises(CanonicalJsonError, match="out of range"):
        envelope_from_canonical_json(obj)


def _all_types_message() -> Any:
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    T = descriptor_pb2.FieldDescriptorProto
    kinds = [
        ("i32", T.TYPE_INT32), ("i64", T.TYPE_INT64), ("u32", T.TYPE_UINT32),
        ("u64", T.TYPE_UINT64), ("s32", T.TYPE_SINT32), ("s64", T.TYPE_SINT64),
        ("f32", T.TYPE_FIXED32), ("f64", T.TYPE_FIXED64), ("sf32", T.TYPE_SFIXED32),
        ("sf64", T.TYPE_SFIXED64), ("flt", T.TYPE_FLOAT), ("dbl", T.TYPE_DOUBLE),
        ("b", T.TYPE_BOOL), ("s", T.TYPE_STRING), ("raw", T.TYPE_BYTES),
    ]  # fmt: skip
    file = descriptor_pb2.FileDescriptorProto(
        name="canonical_json_all_types.proto", package="cjtest", syntax="proto3"
    )
    msg = file.message_type.add(name="All")
    for index, (name, kind) in enumerate(kinds):
        msg.field.add(name=name, number=index + 1, type=kind, label=T.LABEL_OPTIONAL)
        msg.field.add(name=f"r_{name}", number=index + 20, type=kind, label=T.LABEL_REPEATED)
    msg.field.add(
        name="child", number=40, type=T.TYPE_MESSAGE, type_name=".cjtest.All",
        label=T.LABEL_OPTIONAL,
    )  # fmt: skip
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("cjtest.All"))


def test_wire_encoder_matches_protobuf_for_every_scalar_type() -> None:
    from macp_sdk.canonical_json import _wire

    cls = _all_types_message()
    values = {
        "i32": [-1, 2**31 - 1], "i64": [-(2**63), 2**63 - 1], "u32": [0, 2**32 - 1],
        "u64": [1, 2**64 - 1], "s32": [-(2**31), 5], "s64": [-(2**63), 2**63 - 1],
        "f32": [7, 2**32 - 1], "f64": [9, 2**64 - 1], "sf32": [-3, 2**31 - 1],
        "sf64": [-(2**63), 4], "flt": [1.5, -0.25], "dbl": [-0.0, 1e300],
        "b": [True, False], "s": ["é", ""], "raw": [b"\x00\xff", b""],
    }  # fmt: skip
    full = cls(**{k: v[0] for k, v in values.items()}, **{f"r_{k}": v for k, v in values.items()})
    full.child.CopyFrom(cls(s64=-7, s="nested", r_s32=[-1, 1]))
    for message in (full, cls(), cls(child=cls())):
        assert _wire(message) == message.SerializeToString(deterministic=True)
