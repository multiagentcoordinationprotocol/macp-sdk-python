"""Unit tests for agent transport adapters."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from macp.v1 import envelope_pb2

from macp_sdk.agent.transports import (
    GrpcTransportAdapter,
    HttpTransportAdapter,
    _envelope_to_message,
)
from macp_sdk.constants import MODE_DECISION
from macp_sdk.envelope import new_message_id, now_unix_ms
from macp_sdk.errors import MacpTransportError
from macp_sdk.retry import RetryPolicy


def _make_envelope(
    message_type: str = "Proposal",
    session_id: str = "test-session",
    sender: str = "agent-a",
    payload: bytes | None = None,
) -> envelope_pb2.Envelope:
    if payload is None:
        import json

        payload = json.dumps({"proposal_id": "p1", "option": "opt-a"}).encode()
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=MODE_DECISION,
        message_type=message_type,
        message_id=new_message_id(),
        session_id=session_id,
        sender=sender,
        timestamp_unix_ms=now_unix_ms(),
        payload=payload,
    )


class TestEnvelopeToMessage:
    def test_basic_conversion(self):
        env = _make_envelope()
        msg = _envelope_to_message(env)
        assert msg.message_type == "Proposal"
        assert msg.sender == "agent-a"
        assert msg.raw is env
        assert "proposal_id" in msg.payload
        assert msg.proposal_id == "p1"

    def test_binary_payload(self):
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            mode=MODE_DECISION,
            message_type="Custom",
            message_id=new_message_id(),
            session_id="s1",
            sender="agent-b",
            timestamp_unix_ms=now_unix_ms(),
            payload=b"\x00\x01\x02",
        )
        msg = _envelope_to_message(env)
        assert msg.message_type == "Custom"
        # Binary payloads for unknown message types fall through to the
        # ProtoRegistry UTF-8 decode path which produces a text+base64 dict.
        assert msg.payload.get("encoding") in ("text",) or "_raw_bytes" in msg.payload

    def test_empty_payload(self):
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            mode=MODE_DECISION,
            message_type="Ping",
            message_id=new_message_id(),
            session_id="s1",
            sender="agent-c",
            timestamp_unix_ms=now_unix_ms(),
            payload=b"",
        )
        msg = _envelope_to_message(env)
        assert msg.payload == {}
        assert msg.proposal_id is None


class TestGrpcTransportAdapter:
    def test_yields_messages_for_target_session(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()

        env1 = _make_envelope(session_id="target-session")
        env2 = _make_envelope(session_id="other-session")
        env3 = _make_envelope(session_id="target-session", message_type="Vote")
        mock_stream.responses.return_value = iter([env1, env2, env3])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")
        messages = list(adapter.start())

        assert len(messages) == 2
        assert messages[0].message_type == "Proposal"
        assert messages[1].message_type == "Vote"
        mock_stream.close.assert_called_once()

    def test_subscribe_sent_on_start(self):
        """RFC-MACP-0006-A1: the adapter must subscribe to the target
        session before iterating responses so non-initiator agents get
        SessionStart + Proposal replayed regardless of connection order.
        """
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.responses.return_value = iter([])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")
        list(adapter.start())

        mock_stream.send_subscribe.assert_called_once_with("target-session")

    def test_subscribe_precedes_response_iteration(self):
        """send_subscribe must be invoked before ``responses`` is consumed,
        otherwise the runtime won't replay history onto this stream."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        call_order: list[str] = []

        mock_stream.send_subscribe.side_effect = lambda *a, **kw: call_order.append("subscribe")

        def _responses_factory(*_a, **_kw):
            call_order.append("responses")
            return iter([])

        mock_stream.responses.side_effect = _responses_factory
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "s-order")
        list(adapter.start())

        assert call_order == ["subscribe", "responses"]

    def test_replayed_envelopes_yielded_after_subscribe(self):
        """End-to-end adapter contract: after ``send_subscribe`` the stream
        emits replayed envelopes (SessionStart + Proposal) and the adapter
        passes them through — this is the reason non-initiator agents see
        history they would otherwise miss."""
        mock_client = MagicMock()
        mock_stream = MagicMock()

        replayed = [
            _make_envelope(session_id="late", message_type="SessionStart"),
            _make_envelope(session_id="late", message_type="Proposal"),
        ]
        mock_stream.responses.return_value = iter(replayed)
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "late")
        messages = list(adapter.start())

        mock_stream.send_subscribe.assert_called_once_with("late")
        assert [m.message_type for m in messages] == ["SessionStart", "Proposal"]

    def test_stop_closes_stream(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.responses.return_value = iter([])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "s1")
        list(adapter.start())
        adapter.stop()

        assert adapter._stopped is True

    def test_retries_transient_not_found_from_subscribe(self):
        """#75: a NOT_FOUND from the initial subscribe (the target session's
        SessionStart hasn't reached the runtime yet) is a normal startup
        race, retried with backoff instead of raised immediately."""
        mock_client = MagicMock()
        first_stream = MagicMock()
        second_stream = MagicMock()

        def _not_found_once():
            raise MacpTransportError("Session 'target-session' not found", code="NOT_FOUND")
            yield  # pragma: no cover - makes this a generator function

        first_stream.responses.side_effect = _not_found_once
        second_stream.responses.return_value = iter([_make_envelope(session_id="target-session")])
        mock_client.open_stream.side_effect = [first_stream, second_stream]

        adapter = GrpcTransportAdapter(
            mock_client,
            "target-session",
            subscribe_retry=RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0),
        )
        messages = list(adapter.start())

        assert len(messages) == 1
        first_stream.send_subscribe.assert_called_once_with("target-session")
        second_stream.send_subscribe.assert_called_once_with("target-session")
        first_stream.close.assert_called_once()
        second_stream.close.assert_called_once()

    def test_non_not_found_transport_error_raised_immediately(self):
        """Only NOT_FOUND is treated as a transient startup race; any other
        transport failure must still fail fast, with no retry."""
        mock_client = MagicMock()
        mock_stream = MagicMock()

        def _unavailable():
            raise MacpTransportError("boom", code="UNAVAILABLE")
            yield  # pragma: no cover

        mock_stream.responses.side_effect = _unavailable
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")

        with pytest.raises(MacpTransportError, match="boom"):
            list(adapter.start())

        mock_client.open_stream.assert_called_once()

    def test_raises_after_subscribe_retries_exhausted(self):
        """A session that is permanently missing must still fail once the
        bounded retry window is exhausted, not retry forever."""
        mock_client = MagicMock()
        streams: list[MagicMock] = []

        def _not_found():
            raise MacpTransportError("still missing", code="NOT_FOUND")
            yield  # pragma: no cover

        def _new_stream(*_args, **_kwargs):
            stream = MagicMock()
            stream.responses.side_effect = _not_found
            streams.append(stream)
            return stream

        mock_client.open_stream.side_effect = _new_stream

        adapter = GrpcTransportAdapter(
            mock_client,
            "target-session",
            subscribe_retry=RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0),
        )

        with pytest.raises(MacpTransportError, match="still missing"):
            list(adapter.start())

        assert mock_client.open_stream.call_count == 3  # initial attempt + 2 retries
        for stream in streams:
            stream.close.assert_called_once()

    def test_not_found_after_envelope_seen_is_not_retried(self):
        """NOT_FOUND is only transient at startup — once an envelope has
        been delivered the session demonstrably exists, so a later
        NOT_FOUND (e.g. it expired mid-stream) is raised as-is."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        env = _make_envelope(session_id="target-session")

        def _envelope_then_not_found():
            yield env
            raise MacpTransportError("session expired mid-stream", code="NOT_FOUND")

        mock_stream.responses.side_effect = _envelope_then_not_found
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")

        with pytest.raises(MacpTransportError, match="session expired mid-stream"):
            list(adapter.start())

        mock_client.open_stream.assert_called_once()


class TestHttpTransportAdapter:
    def test_stop_sets_flag(self):
        adapter = HttpTransportAdapter(
            base_url="http://localhost:8080",
            session_id="s1",
            participant_id="agent-a",
            poll_interval_ms=100,
        )
        assert adapter._stopped is False
        adapter.stop()
        assert adapter._stopped is True

    def test_config_values(self):
        adapter = HttpTransportAdapter(
            base_url="http://localhost:8080/",
            session_id="s1",
            participant_id="agent-a",
            poll_interval_ms=2000,
            auth_token="tok-123",
        )
        assert adapter._base_url == "http://localhost:8080"
        assert adapter._session_id == "s1"
        assert adapter._participant_id == "agent-a"
        assert adapter._poll_interval == 2.0
        assert adapter._auth_token == "tok-123"
