"""Test that session helpers build and send correct envelopes via the mock client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from macp_sdk.auth import AuthConfig
from macp_sdk.constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)
from macp_sdk.decision import DecisionSession
from macp_sdk.errors import MacpSessionError
from macp_sdk.handoff import HandoffSession
from macp_sdk.proposal import ProposalSession
from macp_sdk.quorum import QuorumSession
from macp_sdk.task import TaskSession


def _sent_envelope(mock_client: MagicMock):
    """Extract the last envelope passed to mock_client.send()."""
    args, _ = mock_client.send.call_args
    return args[0]


def _auth(agent_id: str) -> AuthConfig:
    """Build a per-sender dev auth matching the explicit sender in each test.

    Since SDK 0.2.0 (PY-3), ``AuthConfig.for_dev_agent`` sets ``expected_sender``
    to the agent_id by default, so a session using ``test-agent`` auth cannot
    send envelopes with ``sender='alice'``. The correct pattern is to supply
    matching auth per participant — same shape the integration tests use.
    """
    return AuthConfig.for_dev_agent(agent_id)


class TestDecisionSession:
    def test_start(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.start(intent="pick", participants=["a", "b"], ttl_ms=60000)
        env = _sent_envelope(mock_client)
        assert env.mode == MODE_DECISION
        assert env.message_type == "SessionStart"
        assert env.session_id == "00000000-0000-4000-8000-000000000001"

    def test_propose(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.propose("p1", "option-a")
        env = _sent_envelope(mock_client)
        assert env.message_type == "Proposal"

    def test_vote(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.vote("p1", "APPROVE", sender="alice", auth=_auth("alice"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Vote"
        assert env.sender == "alice"

    def test_vote_invalid_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="invalid vote"):
            s.vote("p1", "MAYBE", sender="alice", auth=_auth("alice"))

    def test_evaluate_invalid_recommendation_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="invalid recommendation"):
            s.evaluate("p1", "SUGGEST", confidence=0.5, sender="alice", auth=_auth("alice"))

    def test_evaluate_invalid_confidence_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="confidence"):
            s.evaluate("p1", "APPROVE", confidence=1.5, sender="alice", auth=_auth("alice"))

    def test_objection_invalid_severity_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="invalid severity"):
            s.raise_objection("p1", reason="bad", severity="extreme")

    def test_max_participants_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="Maximum 1000"):
            s.start(intent="test", participants=[f"a{i}" for i in range(1001)], ttl_ms=60000)

    def test_commit(self, mock_client):
        s = DecisionSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.commit(action="deploy", authority_scope="release", reason="approved")
        env = _sent_envelope(mock_client)
        assert env.message_type == "Commitment"


class TestProposalSession:
    def test_propose(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.propose("p1", "Plan A", summary="first offer")
        env = _sent_envelope(mock_client)
        assert env.mode == MODE_PROPOSAL
        assert env.message_type == "Proposal"

    def test_counter_propose(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.counter_propose("p2", "p1", "Plan B")
        env = _sent_envelope(mock_client)
        assert env.message_type == "CounterProposal"

    def test_accept(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.accept("p1", sender="bob", auth=_auth("bob"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Accept"

    def test_reject(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.reject("p1", terminal=True, sender="bob", auth=_auth("bob"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Reject"

    def test_withdraw(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.withdraw("p1", sender="alice", auth=_auth("alice"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Withdraw"

    def test_withdraw_empty_proposal_id_raises(self, mock_client):
        s = ProposalSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with pytest.raises(MacpSessionError, match="proposal_id must be non-empty"):
            s.withdraw("", sender="alice", auth=_auth("alice"))


class TestTaskSession:
    def test_request_task(self, mock_client):
        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.request_task("t1", "Analyze", instructions="run pipeline")
        env = _sent_envelope(mock_client)
        assert env.mode == MODE_TASK
        assert env.message_type == "TaskRequest"

    def test_accept_task(self, mock_client):
        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.accept_task("t1", sender="worker", auth=_auth("worker"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "TaskAccept"

    def test_update_task(self, mock_client):
        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.update_task("t1", status="running", progress=0.5, sender="worker", auth=_auth("worker"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "TaskUpdate"

    def test_complete_task(self, mock_client):
        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.complete_task(
            "t1", output=b"result", summary="done", sender="worker", auth=_auth("worker")
        )
        env = _sent_envelope(mock_client)
        assert env.message_type == "TaskComplete"

    def test_fail_task(self, mock_client):
        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.fail_task("t1", error_code="ERR", reason="broke", sender="worker", auth=_auth("worker"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "TaskFail"

    def test_deprecated_aliases_still_work(self, mock_client):
        """The old un-suffixed names are kept as deprecated aliases for one
        release. They forward to the canonical method and emit
        ``DeprecationWarning``.
        """
        import warnings

        s = TaskSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s.request("t1", "Analyze", instructions="run pipeline")
            s.update("t1", progress=0.5, sender="worker", auth=_auth("worker"))
            s.complete("t1", sender="worker", auth=_auth("worker"))
            s.fail("t1", sender="worker", auth=_auth("worker"))
        categories = [w.category for w in caught]
        assert categories.count(DeprecationWarning) == 4


class TestHandoffSession:
    def test_offer(self, mock_client):
        s = HandoffSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.offer("h1", "bob", scope="service-xyz")
        env = _sent_envelope(mock_client)
        assert env.mode == MODE_HANDOFF
        assert env.message_type == "HandoffOffer"

    def test_add_context(self, mock_client):
        s = HandoffSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.add_context("h1", context=b"data")
        env = _sent_envelope(mock_client)
        assert env.message_type == "HandoffContext"

    def test_accept_handoff(self, mock_client):
        s = HandoffSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.accept_handoff("h1", sender="bob", auth=_auth("bob"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "HandoffAccept"

    def test_decline(self, mock_client):
        s = HandoffSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.decline("h1", reason="not ready", sender="bob", auth=_auth("bob"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "HandoffDecline"


class TestQuorumSession:
    def test_request_approval(self, mock_client):
        s = QuorumSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.request_approval("r1", "deploy", required_approvals=2)
        env = _sent_envelope(mock_client)
        assert env.mode == MODE_QUORUM
        assert env.message_type == "ApprovalRequest"

    def test_approve(self, mock_client):
        s = QuorumSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.approve("r1", sender="alice", auth=_auth("alice"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Approve"

    def test_reject(self, mock_client):
        s = QuorumSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.reject("r1", sender="bob", auth=_auth("bob"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Reject"

    def test_abstain(self, mock_client):
        s = QuorumSession(mock_client, session_id="00000000-0000-4000-8000-000000000001")
        s.abstain("r1", sender="carol", auth=_auth("carol"))
        env = _sent_envelope(mock_client)
        assert env.message_type == "Abstain"


class TestSessionLifecycleHelpers:
    """``cancel`` / ``suspend`` / ``resume`` delegate to the client RPCs.

    Defined on ``BaseSession`` so every mode helper inherits them — parity
    with macp-sdk-typescript's session-level ``suspend()`` / ``resume()``.
    """

    SID = "00000000-0000-4000-8000-000000000001"

    @pytest.mark.parametrize(
        "factory",
        [DecisionSession, ProposalSession, TaskSession, HandoffSession, QuorumSession],
    )
    def test_suspend_delegates_to_client(self, mock_client, factory):
        session_auth = _auth("alice")
        s = factory(mock_client, session_id=self.SID, auth=session_auth)
        s.suspend(reason="maintenance")
        mock_client.suspend_session.assert_called_once_with(
            self.SID, reason="maintenance", auth=session_auth
        )

    @pytest.mark.parametrize(
        "factory",
        [DecisionSession, ProposalSession, TaskSession, HandoffSession, QuorumSession],
    )
    def test_resume_delegates_to_client(self, mock_client, factory):
        session_auth = _auth("alice")
        s = factory(mock_client, session_id=self.SID, auth=session_auth)
        s.resume(reason="back online")
        mock_client.resume_session.assert_called_once_with(
            self.SID, reason="back online", auth=session_auth
        )

    def test_call_auth_overrides_session_auth(self, mock_client):
        s = DecisionSession(mock_client, session_id=self.SID, auth=_auth("alice"))
        override = _auth("bob")
        s.suspend(auth=override)
        mock_client.suspend_session.assert_called_once_with(self.SID, reason="", auth=override)

    def test_no_session_auth_passes_none(self, mock_client):
        # With no session auth, the helper passes auth=None and the client
        # falls back to its own configured auth.
        s = DecisionSession(mock_client, session_id=self.SID)
        s.resume()
        mock_client.resume_session.assert_called_once_with(self.SID, reason="", auth=None)
