from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk.agent.participant import Participant, ParticipantActions
from macp_sdk.agent.runner import from_bootstrap
from macp_sdk.agent.types import IncomingMessage, TerminalResult
from macp_sdk.auth import AuthConfig
from macp_sdk.base_projection import BaseProjection
from macp_sdk.constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)
from macp_sdk.envelope import new_message_id, now_unix_ms, serialize_message
from macp_sdk.errors import MacpSessionError
from macp_sdk.projections import DecisionProjection


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    auth = AuthConfig.for_dev_agent("test-agent")
    client.auth = auth
    return client


def _make_envelope(
    message_type: str,
    payload_message: object,
    *,
    session_id: str = "test-session",
    sender: str = "agent-a",
    mode: str = MODE_DECISION,
) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=mode,
        message_type=message_type,
        message_id=new_message_id(),
        session_id=session_id,
        sender=sender,
        timestamp_unix_ms=now_unix_ms(),
        payload=serialize_message(payload_message),
    )


class TestParticipantCreation:
    def test_basic_creation(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        assert p.participant_id == "agent-a"
        assert p.session_id == "s1"
        assert p.mode == MODE_DECISION
        assert not p.is_stopped
        assert isinstance(p.projection, DecisionProjection)

    def test_unknown_mode_has_no_projection(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode="ext.custom.v1",
            client=client,
        )
        assert p.projection is None

    def test_session_info(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
            participants=["agent-a", "agent-b"],
            policy_version="policy.strict",
        )
        assert p.session.session_id == "s1"
        assert p.session.mode == MODE_DECISION
        assert p.session.participants == ["agent-a", "agent-b"]
        assert p.session.policy_version == "policy.strict"


class TestParticipantHandlerRegistration:
    def test_fluent_on(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        result = p.on("Proposal", lambda msg, ctx: None)
        assert result is p  # fluent API returns self

    def test_fluent_on_phase_change(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        result = p.on_phase_change("Evaluation", lambda phase, ctx: None)
        assert result is p

    def test_fluent_on_terminal(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        result = p.on_terminal(lambda result: None)
        assert result is p

    def test_chained_registration(self):
        client = _make_mock_client()
        p = (
            Participant(
                participant_id="agent-a",
                session_id="s1",
                mode=MODE_DECISION,
                client=client,
            )
            .on("Proposal", lambda msg, ctx: None)
            .on("Vote", lambda msg, ctx: None)
            .on_phase_change("Evaluation", lambda phase, ctx: None)
            .on_terminal(lambda result: None)
        )
        assert isinstance(p, Participant)


class TestParticipantEventProcessing:
    def test_process_event_dispatches_handler(self):
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        received: list[IncomingMessage] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on("Proposal", lambda msg, ctx: received.append(msg))

        envelope = _make_envelope(
            "Proposal",
            decision_pb2.ProposalPayload(proposal_id="p1", option="opt-a"),
        )
        p.process_event(envelope)
        assert len(received) == 1
        assert received[0].message_type == "Proposal"

    def test_commitment_triggers_terminal(self):
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1",
                action="deploy",
                authority_scope="release",
                reason="approved",
            ),
        )
        p.process_event(envelope)
        assert p.is_stopped
        assert len(terminal_results) == 1
        assert terminal_results[0].state == "Committed"

    def test_session_cancel_triggers_terminal(self):
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        envelope = _make_envelope(
            "SessionCancel",
            core_pb2.SessionCancelPayload(reason="timeout"),
        )
        p.process_event(envelope)
        assert p.is_stopped
        assert len(terminal_results) == 1
        assert terminal_results[0].state == "Cancelled"
        assert terminal_results[0].commitment is None

    def test_stop_sets_stopped(self):
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        assert not p.is_stopped
        p.stop()
        assert p.is_stopped

    def test_projection_updated_on_event(self):
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        envelope = _make_envelope(
            "Proposal",
            decision_pb2.ProposalPayload(proposal_id="p1", option="opt-a"),
        )
        p.process_event(envelope)
        assert p.projection is not None
        proj = p.projection
        assert isinstance(proj, DecisionProjection)
        assert "p1" in proj.proposals

    def test_terminal_reject_of_known_proposal_fires_terminal(self):
        """Issue #119, positive case: a terminal Reject for a proposal this
        participant's projection has actually seen fires on_terminal and
        stops the participant -- proves the wiring works, paired with the
        negative case below so that test proves something rather than
        merely that nothing happened.
        """
        from macp.modes.proposal.v1 import proposal_pb2

        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_PROPOSAL,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.process_event(
            _make_envelope(
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                mode=MODE_PROPOSAL,
                sender="alice",
            )
        )
        p.process_event(
            _make_envelope(
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", terminal=True, reason="no deal"),
                mode=MODE_PROPOSAL,
                sender="bob",
            )
        )
        assert p.is_stopped
        assert len(terminal_results) == 1
        assert terminal_results[0].state == "TerminalRejected"

    def test_terminal_reject_of_unknown_proposal_does_not_fire_terminal(self):
        """Issue #119, negative case: a terminal Reject naming a proposal_id
        this participant's projection never saw must not fire on_terminal or
        stop the participant -- the session has not actually terminated.
        """
        from macp.modes.proposal.v1 import proposal_pb2

        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_PROPOSAL,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.process_event(
            _make_envelope(
                "Reject",
                proposal_pb2.RejectPayload(
                    proposal_id="p-unknown", terminal=True, reason="no deal"
                ),
                mode=MODE_PROPOSAL,
                sender="bob",
            )
        )
        assert p.is_stopped is False
        assert terminal_results == []


class _SessionCancelModelingProjection(BaseProjection):
    """A projection that (unlike every mode projection shipped in this SDK
    today) models SessionCancel in its own mode handling, flipping phase
    to "Cancelled" -- used only to exercise criterion 4: when the
    phase-driven path itself drives the Cancelled transition,
    already_terminal happens to be True for that same envelope too, but
    it must be fired_terminal (the phase path) that's responsible, with
    already_terminal's value irrelevant in that case."""

    MODE = MODE_DECISION

    def __init__(self) -> None:
        super().__init__()
        self.phase = "Open"

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        if envelope.message_type == "SessionCancel":
            self._set_phase("Cancelled")


class TestTerminalDispatchIsNotDuplicated:
    """Phase 7: once on_terminal has fired for a session, a later
    SessionCancel must not fire it a second time -- but a SessionCancel
    that is itself the terminal-causing event (because the projection
    doesn't model it, or no projection is attached, or the projection
    does model it) must still fire exactly once. Each test asserts on
    the recorded state sequence, not just the count -- a count-only
    assertion would pass even if the surviving dispatch were the wrong
    one."""

    def test_session_cancel_after_commitment_does_not_fire_terminal_again(self):
        """Criterion 1: the bug this phase fixes."""
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.process_event(
            _make_envelope(
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
                ),
            )
        )
        p.process_event(
            _make_envelope("SessionCancel", core_pb2.SessionCancelPayload(reason="late"))
        )

        assert [r.state for r in terminal_results] == ["Committed"]

    def test_session_cancel_unmodeled_by_projection_still_fires_once(self):
        """Criterion 2, the anti-over-suppression case: a SessionCancel
        that the attached projection does not model at all must still
        fire on_terminal exactly once, unchanged from today."""
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.process_event(
            _make_envelope("SessionCancel", core_pb2.SessionCancelPayload(reason="abort"))
        )

        assert [r.state for r in terminal_results] == ["Cancelled"]

    def test_session_cancel_with_no_projection_attached_still_fires_once(self):
        """Criterion 3: same as criterion 2, but with no projection
        attached at all (an unregistered mode)."""
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode="ext.custom.v1",
            client=client,
        )
        assert p.projection is None
        p.on_terminal(lambda r: terminal_results.append(r))

        p.process_event(
            _make_envelope(
                "SessionCancel",
                core_pb2.SessionCancelPayload(reason="abort"),
                mode="ext.custom.v1",
            )
        )

        assert [r.state for r in terminal_results] == ["Cancelled"]

    def test_session_cancel_modeled_by_projection_fires_once_from_phase_path(self):
        """Criterion 4: a SessionCancel the projection DOES model (flips
        phase to Cancelled) must fire on_terminal exactly once, via the
        phase path -- proving fired_terminal and already_terminal are
        non-overlapping, not both suppressing the one legitimate
        dispatch."""
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p._projection = _SessionCancelModelingProjection()
        terminal_results: list[TerminalResult] = []
        phase_changes: list[str] = []
        p.on_terminal(lambda r: terminal_results.append(r))
        p.on_phase_change("Cancelled", lambda phase, ctx: phase_changes.append(phase))

        p.process_event(
            _make_envelope("SessionCancel", core_pb2.SessionCancelPayload(reason="abort"))
        )

        assert [r.state for r in terminal_results] == ["Cancelled"]
        assert phase_changes == ["Cancelled"]


class TestInitialPhaseIsNotAnnounced:
    """Phase 8: a projection's initial phase is a constructor artifact,
    not an observed transition -- on_phase_change must not fire for it.
    Criterion 4 (both entry points) is covered by parametrizing both the
    zero-dispatch test and the real-transition test over "envelope"
    (_process_envelope, driven by process_event) and "message"
    (_process_message, driven directly -- it never calls apply_envelope
    itself, since an HTTP-polling transport may not carry raw envelopes,
    so the real-transition case advances the projection's phase the way
    such a transport's own upstream decode step would)."""

    @pytest.mark.parametrize("via", ["envelope", "message"])
    def test_decision_initial_phase_dispatches_zero_phase_changes(self, via):
        """Criteria 1 + 4: an event that leaves the phase at its initial
        value (seeded at construction) must not announce it, through
        both _process_envelope and _process_message -- which read
        self._last_phase through the identical comparison, so the seed
        fix must reach both. Before the fix this fired once, for
        "Proposal"."""
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        phase_changes: list[str] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
        )
        p.on_phase_change("*", lambda phase, ctx: phase_changes.append(phase))

        if via == "envelope":
            # An Objection doesn't move DecisionProjection's phase at
            # all, so this is purely a test of the seed -- the phase is
            # still "Proposal", identical to what __init__ seeded
            # _last_phase to.
            p.process_event(
                _make_envelope(
                    "Objection",
                    decision_pb2.ObjectionPayload(
                        proposal_id="p1", reason="concern", severity="low"
                    ),
                )
            )
        else:
            # _process_message never calls apply_envelope itself -- it
            # only compares the projection's *current* phase (left
            # untouched here, still "Proposal") against _last_phase.
            p._process_message(
                IncomingMessage(message_type="Objection", sender="agent-b", payload={})
            )

        assert phase_changes == []

    def test_no_projection_attached_dispatches_zero_phase_changes(self):
        """Criterion 3: a mode with no registered projection class is
        entirely unaffected -- _last_phase stays None and the phase path
        is skipped, exactly as before this phase."""
        client = _make_mock_client()
        phase_changes: list[str] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode="ext.custom.v1",
            client=client,
        )
        assert p.projection is None
        p.on_phase_change("*", lambda phase, ctx: phase_changes.append(phase))

        p.process_event(
            _make_envelope(
                "Signal",
                core_pb2.SignalPayload(signal_type="ping"),
                mode="ext.custom.v1",
            )
        )

        assert phase_changes == []

    @pytest.mark.parametrize("via", ["envelope", "message"])
    def test_real_transition_still_fires_exactly_once(self, via):
        """Criteria 2 + 4: a Quorum participant's real transition
        (Pending -> Voting) must still dispatch exactly once, for
        "Voting" -- proving the seed value is not sticky -- and this
        must hold identically through both _process_envelope and
        _process_message, which read self._last_phase through the
        identical comparison."""
        from macp.modes.quorum.v1 import quorum_pb2

        client = _make_mock_client()
        phase_changes: list[str] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_QUORUM,
            client=client,
        )
        p.on_phase_change("*", lambda phase, ctx: phase_changes.append(phase))
        assert p.projection is not None
        assert p.projection.phase == "Pending"

        if via == "envelope":
            p.process_event(
                _make_envelope(
                    "ApprovalRequest",
                    quorum_pb2.ApprovalRequestPayload(
                        request_id="r1", action="deploy", summary="", required_approvals=2
                    ),
                    mode=MODE_QUORUM,
                )
            )
        else:
            # _process_message never calls apply_envelope itself (an
            # HTTP-polling transport may not carry raw envelopes) -- it
            # only compares the projection's *current* phase against
            # _last_phase. Advancing the projection directly here stands
            # in for whatever upstream decode step such a transport would
            # have already performed.
            p.projection._set_phase("Voting")
            p._process_message(
                IncomingMessage(message_type="ApprovalRequest", sender="agent-b", payload={})
            )

        assert phase_changes == ["Voting"]
        assert p.projection.phase == "Voting"


class TestParticipantActions:
    def test_send_envelope(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None)
        actions.send_envelope(MagicMock())
        client.send.assert_called_once()

    def test_get_session(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None)
        actions.get_session()
        client.get_session.assert_called_once_with("s1", auth=None)

    def test_cancel_session(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None)
        actions.cancel_session("timeout")
        client.cancel_session.assert_called_once_with("s1", reason="timeout", auth=None)


class TestParticipantActionsModeGates:
    """Phase 2 item 1: ParticipantActions must build the payload shape
    matching the participant's actual mode, not always decision-mode."""

    def test_propose_decision_mode_builds_decision_payload(self):
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        actions.propose("p1", "opt-a", rationale="because")
        envelope = client.send.call_args.args[0]
        payload = decision_pb2.ProposalPayload()
        payload.ParseFromString(envelope.payload)
        assert payload.proposal_id == "p1"
        assert payload.option == "opt-a"
        assert payload.rationale == "because"

    def test_propose_proposal_mode_builds_proposal_payload(self):
        from macp.modes.proposal.v1 import proposal_pb2

        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_PROPOSAL)
        actions.propose("p1", "My Title", summary="short", tags=["a", "b"])
        envelope = client.send.call_args.args[0]
        payload = proposal_pb2.ProposalPayload()
        payload.ParseFromString(envelope.payload)
        assert payload.proposal_id == "p1"
        assert payload.title == "My Title"
        assert payload.summary == "short"
        assert list(payload.tags) == ["a", "b"]

    def test_propose_other_mode_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_TASK)
        with pytest.raises(MacpSessionError, match="propose"):
            actions.propose("p1", "opt-a")
        client.send.assert_not_called()

    def test_evaluate_non_decision_mode_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_PROPOSAL)
        with pytest.raises(MacpSessionError, match="evaluate"):
            actions.evaluate("p1", "APPROVE", confidence=0.9)
        client.send.assert_not_called()

    def test_vote_non_decision_mode_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_TASK)
        with pytest.raises(MacpSessionError, match="vote"):
            actions.vote("p1", "APPROVE")
        client.send.assert_not_called()

    def test_raise_objection_non_decision_mode_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_PROPOSAL)
        with pytest.raises(MacpSessionError, match="raise_objection"):
            actions.raise_objection("p1", reason="bad")
        client.send.assert_not_called()

    def test_decision_mode_actions_unaffected(self):
        """Regression guard: decision-mode participants keep working
        exactly as before -- this is the only mode these actions were
        ever tested against."""
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        actions.evaluate("p1", "APPROVE", confidence=0.9)
        actions.vote("p1", "APPROVE")
        actions.raise_objection("p1", reason="bad")
        assert client.send.call_count == 3


class TestParticipantActionsValidationParity:
    """Issue #82: ParticipantActions must reject the same malformed input
    the direct mode-session classes (DecisionSession, ProposalSession,
    BaseSession.start) already reject, via the same shared validators in
    macp_sdk.validation."""

    def test_start_session_empty_intent_accepted(self):
        # Deliberate relaxation (RFC-MACP-0001 §7.1, issue #121 item 2): the
        # runtime does not require a non-empty intent, so this must not raise.
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_TASK)
        actions.start_session("", ["a", "b"], 1000)
        client.send.assert_called_once()
        sent_envelope = client.send.call_args[0][0]
        payload = core_pb2.SessionStartPayload()
        payload.ParseFromString(sent_envelope.payload)
        assert payload.intent == ""

    def test_start_session_decision_mode_allows_empty_participants(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        actions.start_session("go", [], 1000)
        client.send.assert_called_once()

    @pytest.mark.parametrize("mode", [MODE_PROPOSAL, MODE_TASK, MODE_HANDOFF, MODE_QUORUM])
    def test_start_session_non_decision_mode_rejects_empty_participants(self, mode):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=mode)
        with pytest.raises(MacpSessionError, match="participants must be non-empty"):
            actions.start_session("go", [], 1000)
        client.send.assert_not_called()

    def test_vote_wrong_mode_raises_mode_error_not_field_error(self):
        """The mode guard must fire before field/enum validation -- a
        non-decision-mode participant calling vote() with malformed input
        should see the 'no equivalent action' message, not a confusing
        proposal_id/vote-value message about a concept that mode doesn't
        have."""
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_TASK)
        with pytest.raises(MacpSessionError, match="no equivalent action") as exc_info:
            actions.vote("", "bogus")
        assert "proposal_id" not in str(exc_info.value)
        assert "invalid vote" not in str(exc_info.value)
        client.send.assert_not_called()

    def test_start_session_validates_against_resolved_defaults(self):
        """mode_version/configuration_version default to None; validation
        must run against the resolved DEFAULT_* strings, not None -- the
        common case where a caller omits both must still succeed."""
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        actions.start_session("go", ["a"], 1000)
        client.send.assert_called_once()

    def test_start_session_invalid_ttl_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="ttl_ms"):
            actions.start_session("go", ["a"], 0)
        client.send.assert_not_called()

    def test_evaluate_empty_proposal_id_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="proposal_id must be non-empty"):
            actions.evaluate("", "APPROVE", confidence=0.9)
        client.send.assert_not_called()

    def test_evaluate_invalid_recommendation_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="invalid recommendation"):
            actions.evaluate("p1", "MAYBE", confidence=0.9)
        client.send.assert_not_called()

    def test_evaluate_invalid_confidence_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match=r"confidence must be in \[0\.0, 1\.0\]"):
            actions.evaluate("p1", "APPROVE", confidence=1.5)
        client.send.assert_not_called()

    def test_vote_invalid_value_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="invalid vote"):
            actions.vote("p1", "MAYBE")
        client.send.assert_not_called()

    def test_vote_empty_proposal_id_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="proposal_id must be non-empty"):
            actions.vote("", "APPROVE")
        client.send.assert_not_called()

    def test_raise_objection_invalid_severity_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="invalid severity"):
            actions.raise_objection("p1", reason="x", severity="urgent")
        client.send.assert_not_called()

    def test_propose_decision_mode_empty_proposal_id_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="proposal_id must be non-empty"):
            actions.propose("", "opt-a")
        client.send.assert_not_called()

    def test_propose_decision_mode_empty_option_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_DECISION)
        with pytest.raises(MacpSessionError, match="option must be non-empty"):
            actions.propose("p1", "")
        client.send.assert_not_called()

    def test_propose_proposal_mode_empty_title_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_PROPOSAL)
        with pytest.raises(MacpSessionError, match="title must be non-empty"):
            actions.propose("p1", "")
        client.send.assert_not_called()

    def test_propose_proposal_mode_empty_proposal_id_raises(self):
        client = _make_mock_client()
        actions = ParticipantActions(client, "s1", None, mode=MODE_PROPOSAL)
        with pytest.raises(MacpSessionError, match="proposal_id must be non-empty"):
            actions.propose("", "My Title")
        client.send.assert_not_called()


class _BlockingFakeTransport:
    """A TransportAdapter whose start() blocks until cancel()/stop() is
    called -- stands in for a real gRPC stream parked on an idle session
    with no message pending, to test Participant.stop() without a real
    gRPC server."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._event = threading.Event()
        self.cancel_called = False
        self.stop_called = False

    def start(self):
        self._event.wait(timeout=5)
        return
        yield  # pragma: no cover - makes this a generator function

    def cancel(self) -> None:
        self.cancel_called = True
        self._event.set()

    def stop(self) -> None:
        self.stop_called = True
        self._event.set()


class TestParticipantStopUnblocksTransport:
    """Phase 2 item 2: stop() must unblock a run() loop parked inside a
    blocked transport read, from another thread."""

    def test_stop_unblocks_injected_transport_via_cancel(self):
        fake = _BlockingFakeTransport()
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
            transport=fake,
        )
        thread = threading.Thread(target=p.run, daemon=True)
        thread.start()
        # No race here: fake.start() blocks on a threading.Event that
        # cancel() sets -- if stop() runs before run() reaches the wait(),
        # the event is already set and wait() returns immediately instead
        # of blocking, so this doesn't need to synchronize on run()
        # actually having started first.
        time.sleep(0.05)
        p.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert fake.cancel_called is True

    def test_stop_unblocks_auto_constructed_transport_via_self_transport(self, monkeypatch):
        """The bug this item fixes: run() previously stored the
        auto-constructed transport only in a local variable, never on
        self._transport, so stop() had nothing to cancel when no
        transport was injected at construction (the common case)."""
        fake = _BlockingFakeTransport()
        monkeypatch.setattr(
            "macp_sdk.agent.participant.GrpcTransportAdapter",
            lambda *a, **kw: fake,
        )
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        thread = threading.Thread(target=p.run, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert p._transport is fake, (
            "run() must assign the auto-constructed transport to self._transport"
        )

        p.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert fake.cancel_called is True

    def test_stop_before_run_is_safe_and_run_becomes_a_no_op(self):
        """stop() can be called from a signal handler or another thread
        before run() ever starts -- must not crash, and a subsequent
        run() must not block."""
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
        )
        p.stop()  # no transport exists yet -- must not raise
        assert p.is_stopped

        thread = threading.Thread(target=p.run, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_stop_is_idempotent(self):
        fake = _BlockingFakeTransport()
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
            transport=fake,
        )
        p.stop()
        p.stop()  # must not raise
        assert p.is_stopped

    def test_custom_transport_without_cancel_falls_back_to_stopped_flag(self):
        """A TransportAdapter without a cancel() method degrades to
        today's cooperative-flag behavior instead of raising."""

        class _NoCancelTransport:
            def __init__(self) -> None:
                self.stop_called = False

            def start(self):
                return
                yield  # pragma: no cover

            def stop(self) -> None:
                self.stop_called = True

        transport = _NoCancelTransport()
        client = _make_mock_client()
        p = Participant(
            participant_id="agent-a",
            session_id="s1",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        p.stop()  # must not raise even though transport has no cancel()
        assert p.is_stopped

    def test_run_processes_yielded_envelope_and_returns_on_terminal(self):
        """Regression guard for run()'s stop()/self._transport changes
        above: the normal path (a transport that actually yields a
        message) must still dispatch it and exit cleanly once the
        projection reaches a terminal phase -- run() was never previously
        exercised by any test."""

        class _OneMessageTransport:
            def __init__(self, envelope: envelope_pb2.Envelope) -> None:
                self._envelope = envelope
                self.stop_called = False

            def start(self):
                yield IncomingMessage(
                    message_type=self._envelope.message_type,
                    sender=self._envelope.sender,
                    payload={},
                    raw=self._envelope,
                )

            def stop(self) -> None:
                self.stop_called = True

        client = _make_mock_client()
        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _OneMessageTransport(envelope)
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.run()  # must return on its own -- Commitment drives a terminal phase

        assert p.is_stopped
        assert len(terminal_results) == 1
        assert transport.stop_called is True


class _CountingTransport:
    """Yields the given envelopes one at a time and records exactly how
    many the for-loop pulled from start() -- the reusable fixture for
    proving run() does not wait for (or consume) envelope n+1 once
    self._stopped flips while envelope n was being dispatched."""

    def __init__(self, envelopes: list[envelope_pb2.Envelope]) -> None:
        self._envelopes = envelopes
        self.pulled = 0
        self.stop_called = False

    def start(self):
        for env in self._envelopes:
            self.pulled += 1
            yield IncomingMessage(
                message_type=env.message_type,
                sender=env.sender,
                payload={},
                raw=env,
            )

    def stop(self) -> None:
        self.stop_called = True


class TestRunLoopStopPromptness:
    """Phase 6 criteria 1-2: run()'s loop must re-check self._stopped
    right after dispatch, within the same iteration that set it -- not
    only at the top of the next one. Both tests prove it by giving the
    transport a second envelope and asserting it is never pulled."""

    def test_handler_stop_mid_dispatch_stops_before_next_envelope(self):
        """Criterion 1: a handler calling stop() while processing
        envelope n causes run() to return without waiting for envelope
        n+1."""
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        env1 = _make_envelope(
            "Proposal", decision_pb2.ProposalPayload(proposal_id="p1", option="x"), sender="s1"
        )
        env2 = _make_envelope(
            "Proposal", decision_pb2.ProposalPayload(proposal_id="p2", option="y"), sender="s2"
        )
        transport = _CountingTransport([env1, env2])
        seen: list[str] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )

        def handler(msg: IncomingMessage, ctx: object) -> None:
            seen.append(msg.sender)
            p.stop()

        p.on("Proposal", handler)

        p.run()

        assert seen == ["s1"]
        assert transport.pulled == 1, (
            "run() must not pull a second envelope once the handler for the first one called stop()"
        )

    def test_terminal_phase_stops_before_next_envelope(self):
        """Criterion 2: reaching a terminal phase on envelope n causes
        run() to return within the same iteration, without pulling
        envelope n+1."""
        env1 = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        env2 = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c2", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _CountingTransport([env1, env2])
        client = _make_mock_client()
        terminal_results: list[TerminalResult] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        p.on_terminal(lambda r: terminal_results.append(r))

        p.run()

        assert len(terminal_results) == 1
        assert transport.pulled == 1, (
            "run() must not pull a second envelope once the terminal "
            "dispatch for the first one set self._stopped"
        )

    def test_non_stopping_envelope_does_not_break_the_loop(self):
        """Regression guard for the fix itself: the new post-dispatch
        `if self._stopped: break` must only fire when self._stopped is
        actually True. A transport with two envelopes, neither of which
        stops the participant, must still have both pulled and
        dispatched -- proving the new check isn't an unconditional
        break in disguise."""
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        env1 = _make_envelope(
            "Proposal", decision_pb2.ProposalPayload(proposal_id="p1", option="x"), sender="s1"
        )
        env2 = _make_envelope(
            "Proposal", decision_pb2.ProposalPayload(proposal_id="p2", option="y"), sender="s2"
        )
        transport = _CountingTransport([env1, env2])
        seen: list[str] = []
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        p.on("Proposal", lambda msg, ctx: seen.append(msg.sender))

        p.run()

        assert seen == ["s1", "s2"]
        assert transport.pulled == 2, (
            "run() must keep pulling envelopes while self._stopped stays False"
        )
        assert not p.is_stopped


class _OneMessageTerminalTransport:
    """Yields one terminal-triggering message then ends normally."""

    def __init__(self, envelope: envelope_pb2.Envelope) -> None:
        self._envelope = envelope
        self.stop_called = False

    def start(self):
        yield IncomingMessage(
            message_type=self._envelope.message_type,
            sender=self._envelope.sender,
            payload={},
            raw=self._envelope,
        )

    def stop(self) -> None:
        self.stop_called = True


class _EmptyTransport:
    """Yields nothing -- stands in for a stream that simply ended (e.g.
    the server closed it) with the session still live, not stopped."""

    def __init__(self) -> None:
        self.stop_called = False

    def start(self):
        return
        yield  # pragma: no cover

    def stop(self) -> None:
        self.stop_called = True


class _RaisingStopTransport:
    """stop() raises -- proves run()'s finally must not let that
    exception propagate out of run(), and must not skip the
    conditional cancel-callback close that follows it."""

    def __init__(self, envelope: envelope_pb2.Envelope) -> None:
        self._envelope = envelope

    def start(self):
        yield IncomingMessage(
            message_type=self._envelope.message_type,
            sender=self._envelope.sender,
            payload={},
            raw=self._envelope,
        )

    def stop(self) -> None:
        raise RuntimeError("transport.stop() boom")


class _RaisingHandlerTransport:
    """Yields one message whose registered handler raises -- proves
    run()'s new try/except around transport.stop() does not swallow or
    alter the handler's exception, and that transport.stop() still runs
    during teardown."""

    def __init__(self, envelope: envelope_pb2.Envelope) -> None:
        self._envelope = envelope
        self.stop_called = False

    def start(self):
        yield IncomingMessage(
            message_type=self._envelope.message_type,
            sender=self._envelope.sender,
            payload={},
            raw=self._envelope,
        )

    def stop(self) -> None:
        self.stop_called = True


class TestRunTeardown:
    """Phase 6 criteria 3, 4, 5, 6: run()'s finally block must (a) close
    the bound cancel-callback server exactly once, if and only if this
    run() ended stopped, (b) leave stop() safe and non-reclosing
    afterwards, and (c) never let a transport.stop() exception escape
    and mask the loop's real outcome or skip that conditional close."""

    def test_run_closes_cancel_callback_server_when_session_reaches_terminal(self):
        """Criterion 3."""
        client = _make_mock_client()
        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _OneMessageTerminalTransport(envelope)
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        server = MagicMock()
        p.attach_cancel_callback_server(server)

        p.run()

        assert p.is_stopped
        server.close.assert_called_once()
        assert p._cancel_callback_server is None

    def test_stop_after_stopped_run_does_not_raise_or_reclose(self):
        """Criterion 4: calling stop() after a run() that already ended
        stopped (and already closed the server via the finally block)
        must not raise and must not call close() a second time."""
        client = _make_mock_client()
        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _OneMessageTerminalTransport(envelope)
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        server = MagicMock()
        p.attach_cancel_callback_server(server)

        p.run()
        assert p.is_stopped
        server.close.assert_called_once()

        p.stop()  # must not raise, must not reclose

        assert p.is_stopped
        server.close.assert_called_once()

    def test_run_leaves_cancel_callback_server_bound_when_not_stopped(self):
        """run() returning because the transport's stream simply ended
        (no stop() call, no terminal phase) must NOT close a bound
        cancel-callback server -- the participant is still runnable and
        a later run() should keep using the same cancel endpoint."""
        client = _make_mock_client()
        transport = _EmptyTransport()
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        server = MagicMock()
        p.attach_cancel_callback_server(server)

        p.run()

        assert not p.is_stopped
        server.close.assert_not_called()
        assert p._cancel_callback_server is server

    def test_handler_exception_propagates_unaltered_and_transport_still_stopped(self):
        """Criterion 5: a handler that raises propagates out of run()
        with its original type and message, and transport.stop() was
        still called. The callback server is not closed on this path --
        self._stopped is False."""
        from macp.modes.decision.v1 import decision_pb2

        client = _make_mock_client()
        envelope = _make_envelope(
            "Proposal", decision_pb2.ProposalPayload(proposal_id="p1", option="x")
        )
        transport = _RaisingHandlerTransport(envelope)
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        server = MagicMock()
        p.attach_cancel_callback_server(server)

        def handler(msg: IncomingMessage, ctx: object) -> None:
            raise ValueError("handler boom")

        p.on("Proposal", handler)

        with pytest.raises(ValueError, match="handler boom"):
            p.run()

        assert transport.stop_called is True
        assert not p.is_stopped
        server.close.assert_not_called()

    def test_run_swallows_transport_stop_exception_on_happy_exit(self):
        """Criterion 6 (part 1): a transport whose stop() raises during
        teardown does not change what run() raises -- nothing, in the
        happy (terminal) case."""
        client = _make_mock_client()
        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _RaisingStopTransport(envelope)
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )

        p.run()  # must not raise despite transport.stop() raising

        assert p.is_stopped

    def test_run_closes_cancel_callback_server_even_when_transport_stop_raises(self):
        """Criterion 6 (part 2): the conditional cancel-callback close
        must still run after a transport.stop() exception -- the
        try/except around stop() must not skip the code that follows it
        in the same finally block."""
        client = _make_mock_client()
        envelope = _make_envelope(
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
            ),
        )
        transport = _RaisingStopTransport(envelope)
        p = Participant(
            participant_id="agent-a",
            session_id="test-session",
            mode=MODE_DECISION,
            client=client,
            transport=transport,
        )
        server = MagicMock()
        p.attach_cancel_callback_server(server)

        p.run()

        assert p.is_stopped
        server.close.assert_called_once()


class TestFromBootstrap:
    def test_basic_bootstrap(self):
        bootstrap = {
            "participant_id": "agent-x",
            "session_id": "sess-123",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "agent-x"},
            "participants": ["agent-x", "agent-y"],
            "policy_version": "policy.strict",
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p.participant_id == "agent-x"
            assert p.session_id == "sess-123"
            assert p.mode == "macp.mode.decision.v1"
            assert p.session.participants == ["agent-x", "agent-y"]
            assert p.session.policy_version == "policy.strict"
        finally:
            os.unlink(path)

    def test_bootstrap_with_bearer_token(self):
        bootstrap = {
            "participant_id": "agent-z",
            "session_id": "sess-456",
            "mode": "macp.mode.task.v1",
            "runtime_url": "localhost:50052",
            "auth": {"bearer_token": "secret-token"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p.participant_id == "agent-z"
            assert p.mode == "macp.mode.task.v1"
        finally:
            os.unlink(path)

    def test_bootstrap_env_var(self):
        bootstrap = {
            "participant_id": "agent-env",
            "session_id": "sess-env",
            "mode": "macp.mode.quorum.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "agent-env"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            os.environ["MACP_BOOTSTRAP_FILE"] = path
            p = from_bootstrap()
            assert p.participant_id == "agent-env"
            assert p.session_id == "sess-env"
        finally:
            os.unlink(path)
            del os.environ["MACP_BOOTSTRAP_FILE"]

    def test_no_path_raises(self):
        # Ensure env var is not set
        old = os.environ.pop("MACP_BOOTSTRAP_FILE", None)
        try:
            try:
                from_bootstrap()
                raise AssertionError("Should have raised")
            except ValueError as e:
                assert "MACP_BOOTSTRAP_FILE" in str(e)
        finally:
            if old is not None:
                os.environ["MACP_BOOTSTRAP_FILE"] = old

    def test_bootstrap_rejects_insecure_without_opt_in(self):
        """secure=false without allow_insecure=true must fail (RFC-0006 §3)."""
        import pytest

        from macp_sdk.errors import MacpSdkError

        bootstrap = {
            "participant_id": "agent-x",
            "session_id": "sess-x",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "agent-x"},
            "secure": False,
            # allow_insecure intentionally omitted
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            with pytest.raises(MacpSdkError, match="allow_insecure=True"):
                from_bootstrap(path)
        finally:
            os.unlink(path)

    def test_bootstrap_missing_runtime_address_raises(self):
        """Phase 2 item 5: a bootstrap with neither ``runtime_url`` nor
        ``runtime_address`` must raise before constructing a MacpClient,
        rather than silently defaulting to localhost:50051."""
        import pytest

        bootstrap = {
            "participant_id": "agent-x",
            "session_id": "sess-x",
            "mode": "macp.mode.decision.v1",
            "auth": {"agent_id": "agent-x"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match=r"runtime_url.*runtime_address"):
                from_bootstrap(path)
        finally:
            os.unlink(path)

    def test_bootstrap_explicit_localhost_still_valid(self):
        """An explicit runtime_url of localhost:50051 is not the implicit
        default being rejected -- it's a valid, explicitly-set value."""
        bootstrap = {
            "participant_id": "agent-x",
            "session_id": "sess-x",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "agent-x"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._client.target == "localhost:50051"
        finally:
            os.unlink(path)

    def test_bootstrap_propagates_expected_sender(self):
        """auth.expected_sender (or participant_id fallback) wires through to AuthConfig."""
        bootstrap = {
            "participant_id": "alice",
            "session_id": "sess-1",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"bearer_token": "tok-alice"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._auth is not None
            assert p._auth.expected_sender == "alice"
            assert p._auth.bearer_token == "tok-alice"
        finally:
            os.unlink(path)

    def test_bootstrap_flat_auth_token(self):
        """Flat ``auth_token`` field (new examples-service format)."""
        bootstrap = {
            "participant_id": "agent-flat",
            "session_id": "sess-flat",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth_token": "tok-flat-123",
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._auth is not None
            assert p._auth.bearer_token == "tok-flat-123"
            assert p._auth.expected_sender == "agent-flat"
        finally:
            os.unlink(path)

    def test_bootstrap_flat_agent_id(self):
        """Flat ``agent_id`` field for dev auth."""
        bootstrap = {
            "participant_id": "dev-1",
            "session_id": "sess-dev",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "agent_id": "dev-1",
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._auth is not None
            assert p._auth.bearer_token == "dev-1"
            assert p._auth.sender == "dev-1"
            assert p._auth.expected_sender == "dev-1"
        finally:
            os.unlink(path)

    def test_bootstrap_runtime_address_alias(self):
        """``runtime_address`` is accepted as an alias for ``runtime_url``."""
        bootstrap = {
            "participant_id": "agent-addr",
            "session_id": "sess-addr",
            "mode": "macp.mode.decision.v1",
            "runtime_address": "runtime.local:50052",
            "auth": {"agent_id": "agent-addr"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._client.target == "runtime.local:50052"
        finally:
            os.unlink(path)

    def test_bootstrap_with_initiator_config(self):
        """Bootstrap with ``initiator`` block populates InitiatorConfig."""
        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-init",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "participants": ["coord", "alice"],
            "secure": False,
            "allow_insecure": True,
            "initiator": {
                "session_start": {
                    "intent": "pick a plan",
                    "participants": ["coord", "alice", "bob"],
                    "ttl_ms": 120000,
                },
                "kickoff": {
                    "message_type": "Proposal",
                    "payload": {
                        "proposal_id": "p1",
                        "option": "deploy-v3",
                        "rationale": "tests green",
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            cfg = p._initiator_config
            assert cfg is not None
            assert cfg.intent == "pick a plan"
            assert cfg.participants == ["coord", "alice", "bob"]
            assert cfg.ttl_ms == 120000
            assert cfg.kickoff_message_type == "Proposal"
            assert cfg.kickoff_payload["proposal_id"] == "p1"
        finally:
            os.unlink(path)

    def test_bootstrap_initiator_extensions_decoded_from_base64(self):
        """SDK-PY-1: ``initiator.session_start.extensions`` (a proto
        ``map<string, bytes>`` encoded as base64 per proto-JSON canonical
        form) must be decoded back into ``dict[str, bytes]`` on the
        resulting ``InitiatorConfig``."""
        import base64

        aitp_bytes = b"\x01\x02\x03aitp"
        ctxm_bytes = b"ctxm-provenance"
        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-ext",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
            "initiator": {
                "session_start": {
                    "intent": "with-ext",
                    "participants": ["coord"],
                    "ttl_ms": 60000,
                    "extensions": {
                        "aitp.v1": base64.b64encode(aitp_bytes).decode("ascii"),
                        "ctxm.v1": base64.b64encode(ctxm_bytes).decode("ascii"),
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            cfg = p._initiator_config
            assert cfg is not None
            assert cfg.extensions == {
                "aitp.v1": aitp_bytes,
                "ctxm.v1": ctxm_bytes,
            }
        finally:
            os.unlink(path)

    def test_bootstrap_initiator_extensions_absent_defaults_empty(self):
        """A bootstrap without an ``extensions`` key must yield an empty
        dict so ``_emit_initiator_envelopes()`` does not send a nil map."""
        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-noext",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
            "initiator": {
                "session_start": {
                    "intent": "no-ext",
                    "participants": ["coord"],
                    "ttl_ms": 60000,
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            cfg = p._initiator_config
            assert cfg is not None
            assert cfg.extensions == {}
        finally:
            os.unlink(path)

    def test_emit_initiator_envelopes_passes_extensions(self):
        """SDK-PY-1: ``_emit_initiator_envelopes`` must forward
        ``InitiatorConfig.extensions`` to ``ParticipantActions.start_session``
        so the bytes survive onto the SessionStart envelope."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord", "alice"],
            ttl_ms=30000,
            context_id="ctx-123",
            extensions={"aitp.v1": b"\xde\xad"},
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-emit",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord", "alice"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        p._actions.start_session.assert_called_once()
        kwargs = p._actions.start_session.call_args.kwargs
        assert kwargs["extensions"] == {"aitp.v1": b"\xde\xad"}
        assert kwargs["context_id"] == "ctx-123"

    def test_emit_initiator_envelopes_empty_extensions_sent_as_none(self):
        """An empty ``extensions`` dict should be normalised to ``None``
        so the builder emits an empty proto map (default), not a
        sentinel value the runtime would have to parse specially."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord"],
            ttl_ms=30000,
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-empty-ext",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        assert p._actions.start_session.call_args.kwargs["extensions"] is None

    def test_emit_initiator_envelopes_passes_roots(self):
        """Phase 2 item 6: InitiatorConfig.roots must forward through to
        ParticipantActions.start_session -- the mocked-actions unit test."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord"],
            ttl_ms=30000,
            roots=[{"uri": "file:///x", "name": "x"}],
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-roots",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        assert p._actions.start_session.call_args.kwargs["roots"] == [
            {"uri": "file:///x", "name": "x"}
        ]

    def test_emit_initiator_envelopes_roots_reach_the_wire(self):
        """Phase 2 item 6 acceptance criterion: an initiator bootstrap with
        session_start.roots must produce a SessionStart envelope whose
        decoded payload's roots field is non-empty -- exercises the real
        ParticipantActions.start_session/build_session_start_payload path,
        not a mocked one."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord"],
            ttl_ms=30000,
            roots=[{"uri": "file:///x", "name": "x"}],
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-roots-wire",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )

        p._emit_initiator_envelopes()

        envelope = client.send.call_args_list[0].args[0]
        payload = core_pb2.SessionStartPayload()
        payload.ParseFromString(envelope.payload)
        assert len(payload.roots) == 1
        assert payload.roots[0].uri == "file:///x"
        assert payload.roots[0].name == "x"

    def test_emit_initiator_envelopes_no_roots_sent_as_none(self):
        """No roots configured must not send an empty-but-present roots
        list -- build_session_start_payload gets None, matching the
        extensions=None convention for "not configured"."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(intent="i", participants=["coord"], ttl_ms=30000)
        p = Participant(
            participant_id="coord",
            session_id="sess-no-roots",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        assert p._actions.start_session.call_args.kwargs["roots"] is None

    def test_emit_initiator_envelopes_kickoff_decision_mode(self):
        """Phase 2 item 1: the initiator kickoff call site must build the
        decision-shaped propose() call for a decision-mode participant."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord"],
            ttl_ms=30000,
            kickoff_message_type="Proposal",
            kickoff_payload={
                "proposal_id": "p1",
                "option": "deploy-v1",
                "rationale": "tests green",
            },
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-kickoff-decision",
            mode=MODE_DECISION,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        p._actions.propose.assert_called_once_with("p1", "deploy-v1", rationale="tests green")

    def test_emit_initiator_envelopes_kickoff_proposal_mode(self):
        """Phase 2 item 1: the same kickoff call site must build the
        proposal-shaped propose() call for a proposal-mode participant --
        this is the "must be updated in the same change" dependency the
        plan flags: propose() becoming mode-aware would otherwise silently
        break a proposal-mode initiator's kickoff."""
        from macp_sdk.agent.runner import InitiatorConfig

        client = _make_mock_client()
        cfg = InitiatorConfig(
            intent="i",
            participants=["coord"],
            ttl_ms=30000,
            kickoff_message_type="Proposal",
            kickoff_payload={
                "proposal_id": "p1",
                "title": "Ship v2",
                "summary": "canary rollout",
                "tags": ["release"],
            },
        )
        p = Participant(
            participant_id="coord",
            session_id="sess-kickoff-proposal",
            mode=MODE_PROPOSAL,
            client=client,
            auth=client.auth,
            participants=["coord"],
            initiator_config=cfg,
        )
        p._actions = MagicMock(spec=ParticipantActions)

        p._emit_initiator_envelopes()

        p._actions.propose.assert_called_once_with(
            "p1", "Ship v2", summary="canary rollout", details=b"", tags=["release"]
        )

    def test_bootstrap_cancel_callback_binds_to_participant_stop(self):
        """When the bootstrap JSON carries a ``cancel_callback`` block
        (RFC-0001 §7.2 Option A), ``from_bootstrap`` must spin up the
        HTTP server and wire it to ``participant.stop()`` so a POST
        from the control-plane tears the event loop down cleanly.
        Before 0.2.4 every agent had to hand-roll this."""
        import json as _json
        import urllib.request

        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-cc",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
            "cancel_callback": {
                "host": "127.0.0.1",
                "port": 0,  # let the OS pick — we read the real port off the server
                "path": "/cancel",
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            server = p._cancel_callback_server
            assert server is not None, "cancel_callback server not attached"

            # POST and verify the participant stops.
            host, port = server.address
            req = urllib.request.Request(
                f"http://{host}:{port}/cancel",
                data=b'{"runId":"r","reason":"test"}',
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=2.0)
            assert resp.status == 202
            assert p.is_stopped, "participant.stop() was not called"
            # After stop the server is closed and detached.
            assert p._cancel_callback_server is None
        finally:
            os.unlink(path)

    def test_bootstrap_without_cancel_callback_leaves_server_none(self):
        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-no-cc",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._cancel_callback_server is None
        finally:
            os.unlink(path)

    def test_bootstrap_without_initiator_has_no_config(self):
        """Non-initiator bootstrap has ``initiator_config=None``."""
        bootstrap = {
            "participant_id": "alice",
            "session_id": "sess-non",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "alice"},
            "secure": False,
            "allow_insecure": True,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            assert p._initiator_config is None
        finally:
            os.unlink(path)


class TestCancelCallbackSurvivesSequentialRun:
    """Phase 6 criteria 10-11: a cancel-callback server bound via
    from_bootstrap must survive a run() that ends without stopping
    (criterion 10), and must not be needed again once a run() does end
    stopped (criterion 11) -- proven end to end against the real HTTP
    server, reusing from_bootstrap's existing port:0 scaffolding from
    test_bootstrap_cancel_callback_binds_to_participant_stop above."""

    def test_cancel_endpoint_survives_two_run_calls_when_first_does_not_stop(self):
        """Criterion 10. The liveness probe between the two run() calls
        must be side-effect-free -- a POST to the real /cancel path
        would call stop() and destroy the state under test -- so probe
        with a POST to a non-matching path and assert 404, which proves
        the socket is still bound and served without cancelling
        anything."""
        import urllib.error
        import urllib.request

        class _EmptyRunTransport:
            """Yields nothing on each call -- stands in for a stream
            that ran out with the session still live, not stopped."""

            def __init__(self) -> None:
                self.starts = 0

            def start(self):
                self.starts += 1
                return
                yield  # pragma: no cover

            def stop(self) -> None:
                pass

        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-cc-seq",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
            "cancel_callback": {"host": "127.0.0.1", "port": 0, "path": "/cancel"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            transport = _EmptyRunTransport()
            p._transport = transport
            server = p._cancel_callback_server
            assert server is not None
            host, port = server.address

            def probe_alive() -> None:
                req = urllib.request.Request(
                    f"http://{host}:{port}/not-the-cancel-path",
                    data=b"{}",
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=2.0)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404
                else:
                    pytest.fail("expected 404 from a non-matching path")

            p.run()
            assert not p.is_stopped
            assert p._cancel_callback_server is server
            probe_alive()

            p.run()
            assert transport.starts == 2
            assert not p.is_stopped
            assert p._cancel_callback_server is server
            probe_alive()

            req = urllib.request.Request(
                f"http://{host}:{port}/cancel",
                data=b'{"runId":"r","reason":"done"}',
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=2.0)
            assert resp.status == 202
            assert p.is_stopped
            assert p._cancel_callback_server is None
        finally:
            os.unlink(path)

    def test_second_run_is_a_noop_after_first_run_ends_stopped(self):
        """Criterion 11, the complementary half: once a run() ends
        stopped (terminal envelope), a second run() returns immediately
        via :579's early return -- transport.start() must be entered
        exactly once across both calls, and the already-released
        cancel-callback server must not be touched again."""

        class _OneTerminalMessageTransport:
            def __init__(self, envelope: envelope_pb2.Envelope) -> None:
                self._envelope = envelope
                self.starts = 0

            def start(self):
                self.starts += 1
                yield IncomingMessage(
                    message_type=self._envelope.message_type,
                    sender=self._envelope.sender,
                    payload={},
                    raw=self._envelope,
                )

            def stop(self) -> None:
                pass

        bootstrap = {
            "participant_id": "coord",
            "session_id": "sess-cc-seq-2",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth": {"agent_id": "coord"},
            "secure": False,
            "allow_insecure": True,
            "cancel_callback": {"host": "127.0.0.1", "port": 0, "path": "/cancel"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bootstrap, f)
            path = f.name
        try:
            p = from_bootstrap(path)
            envelope = _make_envelope(
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1", action="deploy", authority_scope="release", reason="ok"
                ),
                session_id="sess-cc-seq-2",
            )
            transport = _OneTerminalMessageTransport(envelope)
            p._transport = transport

            p.run()
            assert p.is_stopped
            assert transport.starts == 1
            assert p._cancel_callback_server is None

            p.run()  # :579 early return -- must not touch the transport again
            assert transport.starts == 1
        finally:
            os.unlink(path)
