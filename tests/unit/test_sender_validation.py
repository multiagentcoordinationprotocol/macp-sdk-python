"""Client-side sender/identity guardrails added in SDK 0.2.0 (PY-3).

When ``AuthConfig.expected_sender`` is set the SDK must raise
``MacpIdentityMismatchError`` before any envelope hits the wire if the
caller passes an explicit ``sender=`` that does not match the auth
identity. This mirrors the runtime rule in RFC-MACP-0004 §4.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from macp.v1 import envelope_pb2

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient
from macp_sdk.decision import DecisionSession
from macp_sdk.errors import MacpIdentityMismatchError
from macp_sdk.proposal import ProposalSession
from macp_sdk.task import TaskSession
from tests.conftest import VALID_SESSION_ID

SESSION_ID = VALID_SESSION_ID


def _make_mock_client(auth: AuthConfig) -> MagicMock:
    client = MagicMock()
    client.auth = auth

    def send_side_effect(envelope, *, auth=None, timeout=None, raise_on_nack=True):
        return envelope_pb2.Ack(
            ok=True, session_id=envelope.session_id, message_id=envelope.message_id
        )

    client.send.side_effect = send_side_effect
    return client


class TestSessionSenderGuardrail:
    def test_mismatch_raises(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        with pytest.raises(MacpIdentityMismatchError) as exc:
            session.vote("p1", "APPROVE", sender="mallory")
        assert exc.value.expected == "alice"
        assert exc.value.actual == "mallory"
        # envelope must never be sent
        client.send.assert_not_called()

    def test_matching_sender_passes(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        session.vote("p1", "APPROVE", sender="alice")
        client.send.assert_called_once()

    def test_fallback_sender_from_auth(self):
        """No explicit sender → envelope.sender falls back to auth.sender_hint."""
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        session.vote("p1", "APPROVE")
        sent_envelope = client.send.call_args[0][0]
        assert sent_envelope.sender == "alice"

    def test_method_level_auth_override_checks_override_identity(self):
        """Per-method auth= override controls which expected_sender is enforced."""
        session_auth = AuthConfig.for_bearer("tok-coord", expected_sender="coordinator")
        alice_auth = AuthConfig.for_bearer("tok-alice", expected_sender="alice")
        client = _make_mock_client(session_auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=session_auth)

        # sender=alice matches the override's expected identity → allowed
        session.vote("p1", "APPROVE", sender="alice", auth=alice_auth)

        # sender=coordinator would match session_auth, but we supplied alice_auth:
        # the override's expected_sender is enforced.
        with pytest.raises(MacpIdentityMismatchError):
            session.vote("p1", "APPROVE", sender="coordinator", auth=alice_auth)

    def test_no_expected_sender_skips_check(self):
        """Dev/test flows without expected_sender keep legacy behaviour."""
        auth = AuthConfig.for_bearer("tok")  # expected_sender=None
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        session.vote("p1", "APPROVE", sender="whoever")
        sent_envelope = client.send.call_args[0][0]
        assert sent_envelope.sender == "whoever"

    def test_commit_respects_guardrail(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        with pytest.raises(MacpIdentityMismatchError):
            session.commit(
                action="approve", authority_scope="release", reason="x", sender="mallory"
            )

    def test_start_respects_guardrail(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = _make_mock_client(auth)
        session = DecisionSession(client, session_id=SESSION_ID, auth=auth)
        with pytest.raises(MacpIdentityMismatchError):
            session.start(
                intent="x",
                participants=["alice", "bob"],
                ttl_ms=60_000,
                sender="mallory",
            )

    def test_task_helper_respects_guardrail(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="worker")
        client = _make_mock_client(auth)
        session = TaskSession(client, session_id=SESSION_ID, auth=auth)
        with pytest.raises(MacpIdentityMismatchError):
            session.accept_task("t1", sender="not-worker")

    def test_proposal_helper_respects_guardrail(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="seller")
        client = _make_mock_client(auth)
        session = ProposalSession(client, session_id=SESSION_ID, auth=auth)
        with pytest.raises(MacpIdentityMismatchError):
            session.propose("p1", "Plan A", sender="buyer")


class TestClientSignalGuardrail:
    """send_signal / send_progress enforce expected_sender too."""

    def test_signal_mismatch_raises(self):
        # build a client with auth but bypass real gRPC by patching the stub
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = MacpClient(target="localhost:0", allow_insecure=True, auth=auth)
        # prevent the real Send from being called
        client.stub = MagicMock()
        with pytest.raises(MacpIdentityMismatchError):
            client.send_signal(signal_type="heartbeat", sender="mallory")
        client.stub.Send.assert_not_called()

    def test_progress_mismatch_raises(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = MacpClient(target="localhost:0", allow_insecure=True, auth=auth)
        client.stub = MagicMock()
        with pytest.raises(MacpIdentityMismatchError):
            client.send_progress(progress_token="tok-1", progress=0.5, total=1.0, sender="mallory")
        client.stub.Send.assert_not_called()
