from __future__ import annotations

from macp_sdk.envelope import (
    build_commitment_payload,
    build_commitment_ref,
    build_envelope,
    build_progress_payload,
    build_root,
    build_session_start_payload,
    build_signal_payload,
    new_commitment_id,
    new_message_id,
    new_session_id,
    serialize_message,
)


class TestIdGenerators:
    def test_session_id_is_uuid(self):
        sid = new_session_id()
        assert len(sid) == 36 and sid.count("-") == 4

    def test_message_id_unique(self):
        assert new_message_id() != new_message_id()

    def test_commitment_id_unique(self):
        assert new_commitment_id() != new_commitment_id()


class TestBuildRoot:
    def test_build(self):
        root = build_root("file:///tmp", "tmp")
        assert root.uri == "file:///tmp"
        assert root.name == "tmp"


class TestBuildSessionStartPayload:
    def test_basic(self):
        payload = build_session_start_payload(
            intent="test",
            participants=["a", "b"],
            ttl_ms=60000,
        )
        assert payload.intent == "test"
        assert list(payload.participants) == ["a", "b"]
        assert payload.ttl_ms == 60000
        assert payload.mode_version == "1.0.0"


class TestBuildCommitmentPayload:
    def test_basic(self):
        payload = build_commitment_payload(
            action="deploy",
            authority_scope="release",
            reason="tests passed",
        )
        assert payload.action == "deploy"
        assert payload.commitment_id  # auto-generated

    def test_supersedes_absent_by_default(self):
        payload = build_commitment_payload(action="deploy", authority_scope="release", reason="r")
        assert not payload.HasField("supersedes")

    def test_supersedes_threads_commitment_ref(self):
        ref = build_commitment_ref(session_id="s-prev", commitment_hash="abc123")
        payload = build_commitment_payload(
            action="deploy",
            authority_scope="release",
            reason="revising the prior call",
            supersedes=ref,
        )
        assert payload.HasField("supersedes")
        assert payload.supersedes.session_id == "s-prev"
        assert payload.supersedes.commitment_hash == "abc123"


class TestBuildEnvelope:
    def test_basic(self):
        payload = build_session_start_payload(intent="x", participants=["a"], ttl_ms=1000)
        env = build_envelope(
            mode="macp.mode.decision.v1",
            message_type="SessionStart",
            session_id="sid-1",
            payload=serialize_message(payload),
            sender="alice",
        )
        assert env.mode == "macp.mode.decision.v1"
        assert env.message_type == "SessionStart"
        assert env.session_id == "sid-1"
        assert env.sender == "alice"
        assert env.message_id  # auto-generated
        assert env.timestamp_unix_ms > 0


class TestBuildSignalPayload:
    def test_basic(self):
        payload = build_signal_payload(signal_type="observation.latency", confidence=0.95)
        assert payload.signal_type == "observation.latency"
        assert payload.confidence == 0.95
        assert payload.data == b""
        assert payload.correlation_session_id == ""

    def test_with_data_and_correlation(self):
        payload = build_signal_payload(
            signal_type="alert.cpu",
            data=b"\x01\x02\x03",
            confidence=0.8,
            correlation_session_id="session-123",
        )
        assert payload.data == b"\x01\x02\x03"
        assert payload.correlation_session_id == "session-123"


class TestBuildProgressPayload:
    def test_basic(self):
        payload = build_progress_payload(
            progress_token="tok-1",
            progress=5.0,
            total=10.0,
        )
        assert payload.progress_token == "tok-1"
        assert payload.progress == 5.0
        assert payload.total == 10.0
        assert payload.message == ""

    def test_with_message_and_target(self):
        payload = build_progress_payload(
            progress_token="tok-2",
            progress=3.0,
            total=8.0,
            message="processing batch",
            target_message_id="msg-abc",
        )
        assert payload.message == "processing batch"
        assert payload.target_message_id == "msg-abc"


class TestSerializeMessage:
    def test_protobuf(self):
        payload = build_root("file:///x")
        data = serialize_message(payload)
        assert isinstance(data, bytes)

    def test_non_protobuf_raises(self):
        import pytest

        with pytest.raises(TypeError):
            serialize_message("not a protobuf")  # type: ignore[arg-type]
