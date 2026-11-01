"""The A2A binding codec (RFC-MACP-0014, Wave 6 S6.2).

Runs every vendored vector in ``tests/vectors/a2a/`` (see its ``SOURCE.md``),
replays the spec's A2A example transcript end to end, and covers the edges the
vectors do not pin. Interop with the official A2A types is in
``test_a2a_interop.py``.
"""

from __future__ import annotations

import base64
import json
import math
import typing
import uuid
from pathlib import Path
from typing import Any

import pytest
from macp.v1 import core_pb2, envelope_pb2

import macp_sdk
from macp_sdk import a2a
from macp_sdk.canonical_json import (
    _wire,
    ack_from_canonical_json,
    ack_to_canonical_json,
    envelope_from_canonical_json,
    envelope_to_canonical_json,
)
from macp_sdk.errors import MacpA2aError, MacpSdkError

ROOT = Path(__file__).parents[1]
VECTOR_DIR = ROOT / "vectors" / "a2a"
EXT = a2a.MACP_A2A_EXTENSION_URI
SESSION = "6f1c2a9e-4b7d-4c1e-9a53-2d8f0b7e5a10"


def _same(a: Any, b: Any) -> bool:
    """Parsed-JSON equality, as the spec's checker compares: key order is
    irrelevant and 2 equals 2.0, but a bool is never a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _run(op: str, inp: dict[str, Any], params: dict[str, Any]) -> Any:
    """One vector op, with Envelopes and Acks in canonical JSON on both sides."""
    limit = {k: v for k, v in params.items() if k == "max_payload_bytes"}
    if op == "envelope_to_a2a_part":
        return a2a.envelope_to_a2a_part(envelope_from_canonical_json(inp["envelope"]), **params)
    if op == "envelope_from_a2a_part":
        return envelope_to_canonical_json(a2a.envelope_from_a2a_part(inp["part"], **limit))
    if op == "envelope_to_a2a_message":
        return a2a.envelope_to_a2a_message(envelope_from_canonical_json(inp["envelope"]), **params)
    if op == "envelope_from_a2a_message":
        return envelope_to_canonical_json(a2a.envelope_from_a2a_message(inp["message"], **limit))
    if op == "ack_to_a2a_message":
        return a2a.ack_to_a2a_message(ack_from_canonical_json(inp["ack"]), **params)
    if op == "ack_from_a2a_message":
        return ack_to_canonical_json(a2a.ack_from_a2a_message(inp["message"]))
    if op == "envelope_to_a2a_artifact":
        env = envelope_from_canonical_json(inp["envelope"])
        return a2a.envelope_to_a2a_artifact(env, inp["sequence"])
    if op == "envelope_from_a2a_artifact":
        env, sequence = a2a.envelope_from_a2a_artifact(inp["artifact"], **limit)
        return {"envelope": envelope_to_canonical_json(env), "sequence": sequence}
    if op == "session_state_to_a2a":
        task_state, metadata = a2a.session_state_to_a2a(inp["session_state"])
        return {"task_state": task_state, "metadata": metadata}
    if op == "session_state_from_a2a":
        return a2a.session_state_from_a2a(inp["task_or_event"])
    if op == "macp_agent_extension":
        return a2a.macp_agent_extension(**params)
    if op == "error_info_from_a2a":
        return a2a.error_info_from_a2a(inp["error_or_details"])
    raise AssertionError(f"unknown op {op!r}")  # pragma: no cover


def _load_vectors() -> list[dict[str, Any]]:
    paths = sorted(p for p in VECTOR_DIR.glob("a2a_*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


VECTORS = _load_vectors()
POSITIVE = [pytest.param(v, id=v["id"]) for v in VECTORS if "expected" in v]
REJECT = [pytest.param(v, id=v["id"]) for v in VECTORS if "expected_error" in v]


# ── vectors ───────────────────────────────────────────────────────────


class TestVectors:
    def test_pack_is_complete(self) -> None:
        # The pack's README: 101 vectors, 66 positive and 35 reject, over every op.
        assert (len(VECTORS), len(POSITIVE), len(REJECT)) == (101, 66, 35)
        ops = {v["op"] for v in VECTORS}
        assert ops == {
            "envelope_to_a2a_part",
            "envelope_from_a2a_part",
            "envelope_to_a2a_message",
            "envelope_from_a2a_message",
            "ack_to_a2a_message",
            "ack_from_a2a_message",
            "envelope_to_a2a_artifact",
            "envelope_from_a2a_artifact",
            "session_state_to_a2a",
            "session_state_from_a2a",
            "macp_agent_extension",
            "error_info_from_a2a",
        }
        assert ops <= set(a2a.__all__)

    @pytest.mark.parametrize("vector", POSITIVE)
    def test_positive(self, vector: dict[str, Any]) -> None:
        got = _run(vector["op"], vector["input"], vector.get("params", {}))
        assert _same(got, vector["expected"]), json.dumps(got, indent=1)

    @pytest.mark.parametrize("vector", REJECT)
    def test_reject(self, vector: dict[str, Any]) -> None:
        with pytest.raises(MacpA2aError) as info:
            _run(vector["op"], vector["input"], vector.get("params", {}))
        want = vector["expected_error"]
        assert (info.value.layer, info.value.code) == (want["layer"], want["code"])

    def test_same_never_equates_a_bool_and_a_number(self) -> None:
        assert _same({"a": [1, 2.0]}, {"a": [1.0, 2]})
        assert not _same(True, 1)
        assert not _same({"a": 1}, {"a": "1"})
        assert not _same({"a": 1}, {"b": 1})


# ── the spec's A2A example, end to end ───────────────────────────────


def _transcript() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (ROOT / "fixtures" / "a2a_binding_session.json").read_text(encoding="utf-8")
    )
    return doc


class TestTranscriptReplay:
    def test_every_step_decodes(self) -> None:
        doc = _transcript()
        sent: list[envelope_pb2.Envelope] = []
        acks: list[envelope_pb2.Ack] = []
        observed: list[tuple[str, int]] = []
        states: list[str] = []
        errors: list[a2a.A2aErrorInfo | None] = []
        for step in doc["exchange"]:
            body = step.get("request", {}).get("body", {})
            if body.get("method") == "SendMessage":
                msg = body["params"]["message"]
                env = a2a.envelope_from_a2a_message(msg)
                sent.append(env)
                # Re-encoding gives back the Message that was sent. The example
                # omits payload defaults; the codec writes every field (§10.10).
                again = a2a.envelope_to_a2a_message(
                    env, message_id=msg["messageId"], context_id=msg.get("contextId")
                )
                want = json.loads(json.dumps(msg))
                want["parts"][0]["data"] = envelope_to_canonical_json(
                    envelope_from_canonical_json(msg["parts"][0]["data"])
                )
                assert _same(dict(again), want)
            response = step.get("response", {}).get("body", {})
            result = response.get("result", {})
            if "message" in result:
                acks.append(a2a.ack_from_a2a_message(result["message"]))
            if "error" in response:
                errors.append(a2a.error_info_from_a2a(response["error"]))
            if "artifacts" in result:  # GetTask
                states.append(a2a.session_state_from_a2a(result))
                observed += [
                    (env.message_type, seq)
                    for env, seq in map(a2a.envelope_from_a2a_artifact, result["artifacts"])
                ]
            for event in step.get("stream", []):
                if "artifactUpdate" in event:
                    env, seq = a2a.envelope_from_a2a_artifact(event["artifactUpdate"]["artifact"])
                    observed.append((env.message_type, seq))
                else:
                    states.append(a2a.session_state_from_a2a(event["statusUpdate"]))

        assert [e.message_type for e in sent] == [
            "SessionStart",
            "Proposal",
            "Vote",
            "Vote",
            "Commitment",
            "Vote",
            "Signal",
        ]
        assert all(e.session_id == SESSION for e in sent[:-1])
        assert [(a.ok, a.duplicate) for a in acks] == [(True, False)] * 3 + [
            (True, True),
            (True, False),
        ]
        assert acks[-1].session_state == envelope_pb2.SESSION_STATE_RESOLVED
        # The stream (subscribed after step 2), then GetTask's full record.
        assert observed == [
            ("Proposal", 2),
            ("Vote", 3),
            ("Commitment", 4),
            ("SessionStart", 1),
            ("Proposal", 2),
            ("Vote", 3),
            ("Commitment", 4),
        ]
        assert states == ["SESSION_STATE_RESOLVED", "SESSION_STATE_RESOLVED"]
        assert [(e["reason"], e["duplicate"]) for e in errors if e is not None] == [
            ("SESSION_NOT_OPEN", False),
            ("INVALID_REQUEST", False),
        ]

    def test_agent_card_extension_matches(self) -> None:
        declared = _transcript()["exchange"][0]["response"]["body"]["capabilities"]["extensions"][0]
        params = declared["params"]
        built = a2a.macp_agent_extension(
            macp_versions=params["macpVersions"],
            modes=params["modes"],
            required=declared["required"],
            manifest_url=params["manifestUrl"],
        )
        # The codec never writes `description`, which is the server's own text.
        assert _same(dict(built), {k: v for k, v in declared.items() if k != "description"})

    def test_ack_messages_re_encode(self) -> None:
        for step in _transcript()["exchange"]:
            msg = step.get("response", {}).get("body", {}).get("result", {}).get("message")
            if msg is None:
                continue
            again = a2a.ack_to_a2a_message(
                a2a.ack_from_a2a_message(msg),
                message_id=msg["messageId"],
                task_id=msg.get("taskId", ""),
                context_id=msg.get("contextId"),
            )
            # The example omits defaults; the codec writes every field (§10.10).
            want = json.loads(json.dumps(msg))
            want["parts"][0]["data"] = ack_to_canonical_json(
                ack_from_canonical_json(msg["parts"][0]["data"])
            )
            assert _same(dict(again), want)


# ── envelopes: edges the vectors do not pin ──────────────────────────


def _env(**overrides: Any) -> envelope_pb2.Envelope:
    fields: dict[str, Any] = {
        "macp_version": "1.0",
        "mode": "ext.multi_round.v1",
        "message_type": "Contribute",
        "message_id": "m-1",
        "session_id": SESSION,
        "sender": "agent://alice",
        "timestamp_unix_ms": 1_792_918_800_000,
        "payload": b"abcd",
    }
    fields.update(overrides)
    return envelope_pb2.Envelope(**fields)


def _json_part(**data: Any) -> dict[str, Any]:
    base = envelope_to_canonical_json(_env())
    base.update(data)
    return {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_JSON, "data": base}


def _raw_part(buf: bytes) -> dict[str, Any]:
    return {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_PROTO, "raw": base64.b64encode(buf).decode()}


def _code(exc: pytest.ExceptionInfo[MacpA2aError]) -> tuple[str, str]:
    return exc.value.layer, exc.value.code


class TestEnvelopeParts:
    @pytest.mark.parametrize("encoding", ["json", "proto"])
    def test_round_trip(self, encoding: str) -> None:
        env = _env()
        part = a2a.envelope_to_a2a_part(env, encoding=encoding)  # type: ignore[arg-type]
        assert a2a.envelope_from_a2a_part(part) == env

    def test_proto_part_is_field_number_order_with_defaults_omitted(self) -> None:
        env = _env(sender="", timestamp_unix_ms=0)
        part = a2a.envelope_to_a2a_part(env, encoding="proto")
        assert base64.b64decode(part["raw"]) == env.SerializeToString(deterministic=True)

    def test_unknown_encoding_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="encoding"):
            a2a.envelope_to_a2a_part(_env(), encoding="yaml")  # type: ignore[arg-type]

    def test_json_part_of_unparsable_standard_payload_is_invalid_envelope(self) -> None:
        env = _env(mode="macp.mode.decision.v1", message_type="Vote", payload=b"\xff")
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_to_a2a_part(env)
        assert _code(info) == ("macp", "INVALID_ENVELOPE")
        # The proto arm carries the bytes as they are.
        assert base64.b64decode(a2a.envelope_to_a2a_part(env, encoding="proto")["raw"])

    def test_negative_zero_survives_a_json_part(self) -> None:
        # No vector yet (spec #246): the envelope schema types the field `number`.
        signal = core_pb2.SignalPayload(signal_type="s", confidence=-0.0)
        env = _env(mode="", message_type="Signal", session_id="", payload=_wire(signal))
        part = a2a.envelope_to_a2a_part(env)
        assert part["data"]["payload"]["confidence"] == "-0"
        back = core_pb2.SignalPayload.FromString(a2a.envelope_from_a2a_part(part).payload)
        assert back.confidence == 0 and math.copysign(1.0, back.confidence) < 0

    @pytest.mark.parametrize("part", [None, [], "x", {"mediaType": "text/plain", "text": "hi"}])
    def test_not_an_envelope_part(self, part: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(part)
        assert _code(info) == ("codec", "NOT_A_MACP_MESSAGE")

    @pytest.mark.parametrize(
        "part",
        [
            {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_JSON},
            {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_JSON, "data": [1]},
            {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_PROTO},
            {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_PROTO, "raw": 7},
            {"mediaType": a2a.MEDIA_TYPE_ENVELOPE_PROTO, "raw": "a"},
        ],
        ids=["json-no-data", "json-data-list", "proto-no-raw", "proto-raw-int", "proto-raw-bad"],
    )
    def test_malformed_arm_is_invalid_envelope(self, part: dict[str, Any]) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(part)
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    @pytest.mark.parametrize(
        "buf",
        [
            b"\x0a\x03\xff\xfe\xfd",  # macp_version is not UTF-8
            b"\x0a\x05ab",  # truncated length-delimited field
            b"\x42\x02\x08\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01",  # 11-byte varint
            b"\x38\x80\x80\x80\x80\x80\x80\x80\x80\x80\x02",  # timestamp overflows 64 bits
        ],
        ids=["bad-utf8", "truncated", "long-varint", "varint-overflow"],
    )
    def test_raw_that_does_not_decode_is_invalid_envelope(self, buf: bytes) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(_raw_part(buf))
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    @pytest.mark.parametrize("unix_ms", [-62_135_596_800_001, 253_402_300_800_000])
    def test_raw_timestamp_outside_years_1_to_9999(self, unix_ms: int) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(_raw_part(_wire(_env(timestamp_unix_ms=unix_ms))))
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    def test_raw_keeps_unknown_fields_out_of_the_way(self) -> None:
        buf = _wire(_env()) + b"\xa0\x06\x01"  # field 100, varint 1
        assert a2a.envelope_from_a2a_part(_raw_part(buf)).message_id == "m-1"


class TestPayloadBound:
    def test_default_is_one_mebibyte(self) -> None:
        at = _env(payload=b"x" * 1_048_576)
        assert len(a2a.envelope_from_a2a_part(a2a.envelope_to_a2a_part(at)).payload) == 1_048_576
        over = _env(payload=b"x" * 1_048_577)
        for encoding in ("json", "proto"):
            part = a2a.envelope_to_a2a_part(over, encoding=encoding)  # type: ignore[arg-type]
            with pytest.raises(MacpA2aError) as info:
                a2a.envelope_from_a2a_part(part)
            assert _code(info) == ("macp", "PAYLOAD_TOO_LARGE")

    def test_json_payload_is_measured_after_conversion(self) -> None:
        signal = core_pb2.SignalPayload(signal_type="risk", confidence=0.5)
        size = len(_wire(signal))
        env = _env(mode="", message_type="Signal", session_id="", payload=_wire(signal))
        part = a2a.envelope_to_a2a_part(env)
        assert "payload" in part["data"]
        assert a2a.envelope_from_a2a_part(part, max_payload_bytes=size).payload == _wire(signal)
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(part, max_payload_bytes=size - 1)
        assert _code(info) == ("macp", "PAYLOAD_TOO_LARGE")

    def test_oversized_payload_b64_is_not_decoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: Any) -> bytes:  # pragma: no cover - must not run
            raise AssertionError("decoded an oversized payload")

        monkeypatch.setattr(a2a, "_decode_base64", boom)
        part = _json_part(payload_b64="QUJD" * 1000)
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(part, max_payload_bytes=2999)
        assert _code(info) == ("macp", "PAYLOAD_TOO_LARGE")

    @pytest.mark.parametrize(
        "text", ["QUJD=QUJD", "QU+_QUJD", "QUJDQ"], ids=["padding", "mixed", "length"]
    )
    def test_bad_base64_beats_the_bound(self, text: str) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(_json_part(payload_b64=text), max_payload_bytes=1)
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    def test_other_fields_beat_the_bound(self) -> None:
        part = _json_part(payload_b64="QUJD" * 10, timestamp="yesterday")
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_part(part, max_payload_bytes=1)
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    def test_zero_bound_allows_an_empty_payload(self) -> None:
        part = _json_part(payload_b64="")
        assert a2a.envelope_from_a2a_part(part, max_payload_bytes=0).payload == b""

    @pytest.mark.parametrize("limit", [-1, 1.5, "10", True, None])
    def test_bad_bound_is_a_value_error(self, limit: Any) -> None:
        with pytest.raises(ValueError, match="max_payload_bytes"):
            a2a.envelope_from_a2a_part(_json_part(), max_payload_bytes=limit)

    def test_bound_applies_to_messages_and_artifacts(self) -> None:
        env = _env()
        msg = a2a.envelope_to_a2a_message(env)
        art = a2a.envelope_to_a2a_artifact(env, 1)
        with pytest.raises(MacpA2aError, match="PAYLOAD_TOO_LARGE"):
            a2a.envelope_from_a2a_message(msg, max_payload_bytes=3)
        with pytest.raises(MacpA2aError, match="PAYLOAD_TOO_LARGE"):
            a2a.envelope_from_a2a_artifact(art, max_payload_bytes=3)


class TestMessages:
    def test_task_id_rules(self) -> None:
        later = a2a.envelope_to_a2a_message(_env())
        assert later["taskId"] == SESSION
        for message_type in ("SessionStart", "Signal"):
            env = _env(message_type=message_type, payload=b"")
            assert "taskId" not in a2a.envelope_to_a2a_message(env, encoding="proto")

    def test_missing_task_id_on_a_later_envelope(self) -> None:
        msg = dict(a2a.envelope_to_a2a_message(_env()))
        del msg["taskId"]
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_message(msg)
        assert _code(info) == ("macp", "INVALID_ENVELOPE")

    def test_task_id_on_session_start_is_left_to_the_server(self) -> None:
        env = _env(message_type="SessionStart", payload=b"")
        msg = dict(a2a.envelope_to_a2a_message(env, encoding="proto"))
        msg["taskId"] = "someone-else"
        assert a2a.envelope_from_a2a_message(msg) == env

    @pytest.mark.parametrize("msg", [None, "x", {}, {"parts": "x"}, {"parts": []}])
    def test_not_a_macp_message(self, msg: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_message(msg)
        assert _code(info) == ("codec", "NOT_A_MACP_MESSAGE")

    def test_message_id_and_context_id(self) -> None:
        msg = a2a.envelope_to_a2a_message(_env(), message_id="retry-1", context_id="ctx")
        assert (msg["messageId"], msg["contextId"]) == ("retry-1", "ctx")
        assert msg["extensions"] == [EXT] and msg["role"] == "ROLE_USER"
        assert "contextId" not in a2a.envelope_to_a2a_message(_env(), context_id="")


def _ack(**fields: Any) -> envelope_pb2.Ack:
    return envelope_pb2.Ack(message_id="m-1", session_id=SESSION, **fields)


class TestAcks:
    def test_round_trip(self) -> None:
        ok = _ack(ok=True, accepted_at_unix_ms=2**60, session_state=1)
        assert a2a.ack_from_a2a_message(a2a.ack_to_a2a_message(ok)) == ok
        err = envelope_pb2.MACPError(code="POLICY_DENIED", message="no", details=b"{}")
        nack = _ack(ok=False, error=err)
        assert a2a.ack_from_a2a_message(a2a.ack_to_a2a_message(nack)) == nack

    def test_message_id_defaults_to_a_new_uuid(self) -> None:
        first = a2a.ack_to_a2a_message(_ack(ok=True))["messageId"]
        second = a2a.ack_to_a2a_message(_ack(ok=True))["messageId"]
        assert first != second and uuid.UUID(first)

    def test_shape(self) -> None:
        msg = a2a.ack_to_a2a_message(_ack(ok=True), message_id="a", task_id="t", context_id="c")
        assert msg["role"] == "ROLE_AGENT" and msg["extensions"] == [EXT]
        assert (msg["taskId"], msg["contextId"]) == ("t", "c")
        assert msg["parts"][0]["metadata"] == {EXT + "/kind": "ack"}
        bare = a2a.ack_to_a2a_message(_ack(ok=True), message_id="a")
        assert "taskId" not in bare and "contextId" not in bare

    @pytest.mark.parametrize(
        "ack",
        [_ack(ok=False), _ack(ok=True, error=envelope_pb2.MACPError(code="X"))],
        ids=["nack-without-error", "ok-with-error"],
    )
    def test_encoder_refuses_an_inconsistent_ack(self, ack: envelope_pb2.Ack) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.ack_to_a2a_message(ack)
        assert _code(info) == ("codec", "INVALID_ACK")

    def test_two_ack_parts(self) -> None:
        msg = a2a.ack_to_a2a_message(_ack(ok=True), message_id="a")
        msg["parts"] = msg["parts"] * 2
        with pytest.raises(MacpA2aError) as info:
            a2a.ack_from_a2a_message(msg)
        assert _code(info) == ("codec", "INVALID_ACK")

    @pytest.mark.parametrize("data", [None, [], "ok", {"ok": "yes"}])
    def test_ack_data_that_does_not_decode(self, data: Any) -> None:
        part = {"mediaType": "application/json", "data": data, "metadata": {EXT + "/kind": "ack"}}
        with pytest.raises(MacpA2aError) as info:
            a2a.ack_from_a2a_message({"parts": [part]})
        assert _code(info) == ("codec", "INVALID_ACK")

    @pytest.mark.parametrize(
        "msg",
        [None, {}, {"parts": "x"}, {"parts": [1, {"metadata": "x"}, {"metadata": {}}]}],
    )
    def test_no_ack_part(self, msg: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.ack_from_a2a_message(msg)
        assert _code(info) == ("codec", "NOT_A_MACP_MESSAGE")


class TestArtifacts:
    def test_round_trip(self) -> None:
        env = _env()
        art = a2a.envelope_to_a2a_artifact(env, 7)
        assert art["artifactId"] == "macp:m-1" and art["name"] == "Contribute"
        assert a2a.envelope_from_a2a_artifact(art) == (env, 7)

    @pytest.mark.parametrize(
        "sequence", [0, -1, 1.5, math.inf, math.nan, True, "1", None, 2**53], ids=repr
    )
    def test_bad_sequence(self, sequence: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_to_a2a_artifact(_env(), sequence)
        assert _code(info) == ("codec", "INVALID_SEQUENCE")
        art = dict(a2a.envelope_to_a2a_artifact(_env(), 1))
        art["metadata"] = {EXT + "/sequence": sequence}
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_artifact(art)
        assert _code(info) == ("codec", "INVALID_SEQUENCE")

    def test_largest_sequence(self) -> None:
        art = a2a.envelope_to_a2a_artifact(_env(), 2**53 - 1)
        assert a2a.envelope_from_a2a_artifact(art)[1] == 2**53 - 1

    @pytest.mark.parametrize("meta", [None, "x", {}])
    def test_missing_metadata(self, meta: Any) -> None:
        art = dict(a2a.envelope_to_a2a_artifact(_env(), 1))
        art["metadata"] = meta
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_artifact(art)
        assert _code(info) == ("codec", "INVALID_SEQUENCE")

    def test_not_an_artifact(self) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.envelope_from_a2a_artifact([])  # type: ignore[arg-type]
        assert _code(info) == ("codec", "NOT_A_MACP_MESSAGE")


class TestSessionState:
    @pytest.mark.parametrize(
        ("state", "task_state", "bare"),
        [
            (envelope_pb2.SESSION_STATE_OPEN, "TASK_STATE_WORKING", "OPEN"),
            (envelope_pb2.SESSION_STATE_RESOLVED, "TASK_STATE_COMPLETED", "RESOLVED"),
            (envelope_pb2.SESSION_STATE_EXPIRED, "TASK_STATE_FAILED", "EXPIRED"),
            (envelope_pb2.SESSION_STATE_SUSPENDED, "TASK_STATE_WORKING", "SUSPENDED"),
            (envelope_pb2.SESSION_STATE_CANCELLED, "TASK_STATE_CANCELED", "CANCELLED"),
        ],
    )
    def test_numbers_and_round_trip(self, state: int, task_state: str, bare: str) -> None:
        got_task_state, metadata = a2a.session_state_to_a2a(state)
        assert (got_task_state, metadata) == (task_state, {EXT + "/sessionState": bare})
        name = envelope_pb2.SessionState.Name(state)  # type: ignore[arg-type]
        assert a2a.session_state_from_a2a({"id": SESSION, "metadata": metadata}) == name

    @pytest.mark.parametrize("state", [0, 99, -1, True, "OPEN", "", None, 1.0], ids=repr)
    def test_no_mapping(self, state: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.session_state_to_a2a(state)
        assert _code(info) == ("codec", "INVALID_SESSION_STATE")

    @pytest.mark.parametrize(
        "obj",
        [None, [], {}, {"metadata": "x"}, {"metadata": {EXT + "/sessionState": 1}}],
    )
    def test_unreadable(self, obj: Any) -> None:
        with pytest.raises(MacpA2aError) as info:
            a2a.session_state_from_a2a(obj)
        assert _code(info) == ("codec", "INVALID_SESSION_STATE")

    def test_never_falls_back_to_task_state(self) -> None:
        task = {"id": SESSION, "status": {"state": "TASK_STATE_COMPLETED"}}
        with pytest.raises(MacpA2aError):
            a2a.session_state_from_a2a(task)


class TestAgentExtensionAndErrors:
    def test_extension_defaults(self) -> None:
        ext = a2a.macp_agent_extension(macp_versions=("1.0",), modes=iter(["m"]))
        assert ext == {
            "uri": EXT,
            "required": False,
            "params": {
                "macpVersions": ["1.0"],
                "modes": ["m"],
                "transport": "macp.transport.a2a.v1",
            },
        }

    _INFO: typing.ClassVar[dict[str, str]] = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "domain": "multiagentcoordinationprotocol.io",
        "reason": "SESSION_NOT_OPEN",
    }

    @pytest.mark.parametrize(
        "src",
        [None, 7, "x", {}, {"data": "x"}, {"data": [1, "x"]}, [{**_INFO, "domain": "other"}]],
    )
    def test_no_macp_error_info(self, src: Any) -> None:
        assert a2a.error_info_from_a2a(src) is None

    def test_odd_fields_are_tolerated(self) -> None:
        got = a2a.error_info_from_a2a([{**self._INFO, "reason": 5, "metadata": "x"}])
        assert got == {
            "reason": "",
            "domain": "multiagentcoordinationprotocol.io",
            "metadata": {},
            "duplicate": False,
        }

    def test_first_macp_entry_wins(self) -> None:
        second = {**self._INFO, "reason": "DUPLICATE", "metadata": {"duplicate": "true"}}
        got = a2a.error_info_from_a2a({"data": [self._INFO, second]})
        assert got is not None and got["reason"] == "SESSION_NOT_OPEN"
        assert a2a.error_info_from_a2a([second]) == {
            "reason": "DUPLICATE",
            "domain": "multiagentcoordinationprotocol.io",
            "metadata": {"duplicate": "true"},
            "duplicate": True,
        }


class TestSurface:
    def test_error_type(self) -> None:
        err = MacpA2aError("codec", "INVALID_ACK", "bad")
        assert isinstance(err, MacpSdkError) and isinstance(err, ValueError)
        assert (err.layer, err.code, str(err)) == ("codec", "INVALID_ACK", "codec/INVALID_ACK: bad")

    def test_constants(self) -> None:
        assert a2a.MACP_A2A_EXTENSION_URI == (
            "https://multiagentcoordinationprotocol.io/a2a/extensions/macp/v1"
        )
        assert a2a.MEDIA_TYPE_ENVELOPE_JSON == "application/macp-envelope+json"
        assert a2a.MEDIA_TYPE_ENVELOPE_PROTO == "application/macp-envelope+proto"
        assert _transcript()["a2a_version"] == a2a.A2A_VERSION
        assert _transcript()["extension_uri"] == a2a.MACP_A2A_EXTENSION_URI

    def test_exported_from_the_package(self) -> None:
        assert set(a2a.__all__) <= set(macp_sdk.__all__)
        assert "MacpA2aError" in macp_sdk.__all__
        for name in a2a.__all__:
            assert getattr(macp_sdk, name) is getattr(a2a, name)
