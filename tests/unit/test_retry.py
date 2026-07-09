from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from macp.v1 import envelope_pb2

from macp_sdk.errors import AckFailure, MacpAckError, MacpRetryError, MacpTransportError
from macp_sdk.retry import RetryPolicy, retry_send


def _make_envelope() -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode="macp.mode.decision.v1",
        message_type="Vote",
        message_id="m1",
        session_id="s1",
        sender="alice",
    )


class TestRetryPolicy:
    def test_defaults(self):
        p = RetryPolicy()
        assert p.max_retries == 3
        assert "RATE_LIMITED" in p.retryable_codes

    def test_retryable_codes_exact_cross_sdk_parity(self):
        """The retryable set must match exactly between Python and TypeScript
        (typescript-sdk/src/retry.ts ``DEFAULT_RETRY_POLICY.retryableCodes``).
        FORBIDDEN, UNAUTHENTICATED, and POLICY_DENIED must NOT be retryable —
        a well-meaning contributor adding one of these on one side would
        desync the SDKs.
        """
        p = RetryPolicy()
        assert p.retryable_codes == frozenset({"RATE_LIMITED", "INTERNAL_ERROR"})


class TestRetrySend:
    @patch("macp_sdk.retry.time.sleep")
    def test_succeeds_first_try(self, mock_sleep):
        client = MagicMock()
        ack = envelope_pb2.Ack(ok=True)
        client.send.return_value = ack

        result = retry_send(client, _make_envelope())
        assert result is ack
        mock_sleep.assert_not_called()

    @patch("macp_sdk.retry.time.sleep")
    def test_retries_on_transport_error(self, mock_sleep):
        client = MagicMock()
        ack = envelope_pb2.Ack(ok=True)
        client.send.side_effect = [MacpTransportError("boom"), ack]

        result = retry_send(client, _make_envelope())
        assert result is ack
        assert mock_sleep.call_count == 1

    @patch("macp_sdk.retry.time.sleep")
    def test_retries_on_retryable_nack(self, mock_sleep):
        client = MagicMock()
        ack = envelope_pb2.Ack(ok=True)
        failure = AckFailure(code="RATE_LIMITED", message="slow down")
        client.send.side_effect = [MacpAckError(failure), ack]

        result = retry_send(client, _make_envelope())
        assert result is ack

    @patch("macp_sdk.retry.time.sleep")
    def test_does_not_retry_non_retryable_nack(self, mock_sleep):
        client = MagicMock()
        failure = AckFailure(code="FORBIDDEN", message="denied")
        client.send.side_effect = MacpAckError(failure)

        with pytest.raises(MacpAckError):
            retry_send(client, _make_envelope())
        mock_sleep.assert_not_called()

    @patch("macp_sdk.retry.time.sleep")
    def test_exhausts_retries(self, mock_sleep):
        client = MagicMock()
        client.send.side_effect = MacpTransportError("boom")

        policy = RetryPolicy(max_retries=2)
        with pytest.raises(MacpRetryError, match="retries exhausted"):
            retry_send(client, _make_envelope(), policy=policy)
        assert client.send.call_count == 3  # 1 initial + 2 retries

    @patch("macp_sdk.retry.time.sleep")
    def test_exponential_backoff_schedule(self, mock_sleep):
        """The delays follow backoff_base * 2**attempt, with no jitter — the
        deterministic schedule is cross-SDK parity behaviour (see retry.ts)."""
        client = MagicMock()
        client.send.side_effect = MacpTransportError("boom")

        with pytest.raises(MacpRetryError):
            retry_send(client, _make_envelope(), policy=RetryPolicy(max_retries=3))

        assert [c.args[0] for c in mock_sleep.call_args_list] == [0.1, 0.2, 0.4]

    @patch("macp_sdk.retry.time.sleep")
    def test_backoff_clamped_at_max(self, mock_sleep):
        client = MagicMock()
        client.send.side_effect = MacpTransportError("boom")

        policy = RetryPolicy(max_retries=3, backoff_base=1.0, backoff_max=2.0)
        with pytest.raises(MacpRetryError):
            retry_send(client, _make_envelope(), policy=policy)

        assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 2.0, 2.0]

    @patch("macp_sdk.retry.time.sleep")
    def test_no_sleep_after_final_attempt(self, mock_sleep):
        client = MagicMock()
        client.send.side_effect = MacpTransportError("boom")

        policy = RetryPolicy(max_retries=2)
        with pytest.raises(MacpRetryError):
            retry_send(client, _make_envelope(), policy=policy)

        # Sleeps happen between attempts only, never after the last failure.
        assert mock_sleep.call_count == policy.max_retries

    @patch("macp_sdk.retry.time.sleep")
    def test_retry_emits_debug_log(self, mock_sleep, caplog):
        import logging

        client = MagicMock()
        ack = envelope_pb2.Ack(ok=True)
        client.send.side_effect = [MacpTransportError("boom"), ack]

        with caplog.at_level(logging.DEBUG, logger="macp_sdk"):
            retry_send(client, _make_envelope())

        assert any("retry attempt=1" in r.getMessage() for r in caplog.records)
