"""Test send_signal() and send_progress() convenience methods on MacpClient."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from macp.v1 import core_pb2

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient
from macp_sdk.envelope import (
    build_envelope,
    build_progress_payload,
    build_signal_payload,
    serialize_message,
)
from macp_sdk.errors import MacpSessionError


def _sent_envelope(mock_client: MagicMock):
    args, _ = mock_client.send.call_args
    return args[0]


def _send_signal(
    mock_client: MagicMock,
    *,
    signal_type: str,
    data: bytes = b"",
    confidence: float = 0.0,
    correlation_session_id: str = "",
    sender: str = "",
    auth: AuthConfig | None = None,
):
    """Reproduce the send_signal logic against the mock."""
    auth_cfg = auth or mock_client.auth
    payload = build_signal_payload(
        signal_type=signal_type,
        data=data,
        confidence=confidence,
        correlation_session_id=correlation_session_id,
    )
    envelope = build_envelope(
        mode="",
        message_type="Signal",
        session_id="",
        payload=serialize_message(payload),
        sender=sender or auth_cfg.sender,
    )
    return mock_client.send(envelope, auth=auth_cfg, timeout=None)


def _send_progress(
    mock_client: MagicMock,
    *,
    session_id: str,
    mode: str,
    progress_token: str,
    progress: float,
    total: float,
    message: str = "",
    target_message_id: str = "",
    sender: str = "",
    auth: AuthConfig | None = None,
):
    """Reproduce the send_progress logic against the mock."""
    auth_cfg = auth or mock_client.auth
    payload = build_progress_payload(
        progress_token=progress_token,
        progress=progress,
        total=total,
        message=message,
        target_message_id=target_message_id,
    )
    envelope = build_envelope(
        mode=mode,
        message_type="Progress",
        session_id=session_id,
        payload=serialize_message(payload),
        sender=sender or auth_cfg.sender,
    )
    return mock_client.send(envelope, auth=auth_cfg, timeout=None)


class TestSendSignal:
    def test_basic_signal(self, mock_client):
        _send_signal(mock_client, signal_type="observation.latency", confidence=0.9)
        env = _sent_envelope(mock_client)
        assert env.mode == ""
        assert env.session_id == ""
        assert env.message_type == "Signal"

    def test_signal_with_correlation(self, mock_client):
        _send_signal(
            mock_client,
            signal_type="alert.cpu",
            data=b"\x01",
            confidence=0.8,
            correlation_session_id="sess-42",
        )
        env = _sent_envelope(mock_client)
        assert env.message_type == "Signal"

    def test_signal_payload_roundtrip(self, mock_client):
        _send_signal(mock_client, signal_type="test.ping", confidence=1.0)
        env = _sent_envelope(mock_client)
        payload = core_pb2.SignalPayload()
        payload.ParseFromString(env.payload)
        assert payload.signal_type == "test.ping"
        assert payload.confidence == 1.0


class TestSendProgress:
    def test_basic_progress(self, mock_client):
        _send_progress(
            mock_client,
            session_id="s1",
            mode="macp.mode.task.v1",
            progress_token="tok-1",
            progress=3.0,
            total=10.0,
        )
        env = _sent_envelope(mock_client)
        assert env.session_id == "s1"
        assert env.mode == "macp.mode.task.v1"
        assert env.message_type == "Progress"

    def test_progress_payload_roundtrip(self, mock_client):
        _send_progress(
            mock_client,
            session_id="s1",
            mode="macp.mode.task.v1",
            progress_token="tok-2",
            progress=5.0,
            total=8.0,
            message="halfway",
            target_message_id="msg-xyz",
        )
        env = _sent_envelope(mock_client)
        payload = core_pb2.ProgressPayload()
        payload.ParseFromString(env.payload)
        assert payload.progress_token == "tok-2"
        assert payload.progress == 5.0
        assert payload.total == 8.0
        assert payload.message == "halfway"
        assert payload.target_message_id == "msg-xyz"


class TestSendProgressScopePairing:
    """RFC-MACP-0001 §6 makes ``Progress`` tri-state: ambient (``session_id``
    and ``mode`` both empty) or session-scoped (both non-empty), never mixed.
    macp-runtime rejects the mixed shape with ``INVALID_ENVELOPE`` as of
    PR #137; ``send_progress`` fails fast with a message naming the mismatch.

    These exercise the real ``MacpClient.send_progress`` (the helpers above
    only reproduce its envelope-building logic), so they lock the guard in
    place rather than a copy of it.
    """

    SID = "550e8400-e29b-41d4-a716-446655440000"

    @staticmethod
    def _client() -> MacpClient:
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = MacpClient(target="localhost:0", allow_insecure=True, auth=auth)
        client.stub = MagicMock()
        return client

    def test_session_id_without_mode_raises_without_sending(self):
        client = self._client()
        with pytest.raises(MacpSessionError, match="but mode is empty"):
            client.send_progress(session_id=self.SID, progress_token="t1", progress=1.0, total=4.0)
        client.stub.Send.assert_not_called()

    def test_mode_without_session_id_raises_without_sending(self):
        client = self._client()
        with pytest.raises(MacpSessionError, match="but session_id is empty"):
            client.send_progress(
                mode="macp.mode.task.v1", progress_token="t1", progress=1.0, total=4.0
            )
        client.stub.Send.assert_not_called()

    def test_ambient_form_is_accepted(self):
        client = self._client()
        client.send = MagicMock()  # type: ignore[method-assign]
        client.send_progress(progress_token="t1", progress=1.0, total=4.0)
        env = client.send.call_args.args[0]
        assert env.message_type == "Progress"
        assert env.session_id == ""
        assert env.mode == ""

    def test_session_scoped_form_is_accepted(self):
        client = self._client()
        client.send = MagicMock()  # type: ignore[method-assign]
        client.send_progress(
            session_id=self.SID,
            mode="macp.mode.task.v1",
            progress_token="t1",
            progress=1.0,
            total=4.0,
        )
        env = client.send.call_args.args[0]
        assert env.message_type == "Progress"
        assert env.session_id == self.SID
        assert env.mode == "macp.mode.task.v1"
