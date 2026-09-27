from __future__ import annotations

from macp.modes.decision.v1 import decision_pb2
from macp.v1 import envelope_pb2

from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession
from .constants import MODE_DECISION
from .envelope import build_envelope, serialize_message
from .projections import DecisionProjection
from .validation import (
    validate_confidence,
    validate_recommendation,
    validate_required_field,
    validate_severity,
    validate_vote,
)


class DecisionSession(BaseSession):
    """High-level helper for Decision mode sessions.

    Inherits ``start``, ``commit``, ``cancel``, ``metadata``, and ``open_stream``
    from :class:`BaseSession`.  Adds decision-specific actions: ``propose``,
    ``evaluate``, ``raise_objection``, and ``vote``.

    Note: The initiator must be included in the ``participants`` list passed
    to ``start()`` in order to propose.  The runtime no longer grants
    implicit proposal authority to the initiator.
    """

    MODE = MODE_DECISION

    def _create_projection(self) -> BaseProjection:
        return DecisionProjection()

    # Narrow the type for callers that want decision-specific queries.
    @property
    def decision_projection(self) -> DecisionProjection:
        assert isinstance(self.projection, DecisionProjection)
        return self.projection

    def propose(
        self,
        proposal_id: str,
        option: str,
        *,
        rationale: str = "",
        supporting_data: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        validate_required_field("option", option)
        payload = decision_pb2.ProposalPayload(
            proposal_id=proposal_id,
            option=option,
            rationale=rationale,
            supporting_data=supporting_data,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Proposal",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def evaluate(
        self,
        proposal_id: str,
        recommendation: str,
        *,
        confidence: float,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        normalized_rec = validate_recommendation(recommendation)
        validate_confidence(confidence)
        payload = decision_pb2.EvaluationPayload(
            proposal_id=proposal_id,
            recommendation=normalized_rec,
            confidence=confidence,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Evaluation",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def raise_objection(
        self,
        proposal_id: str,
        *,
        reason: str,
        severity: str = "medium",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        normalized_sev = validate_severity(severity)
        payload = decision_pb2.ObjectionPayload(
            proposal_id=proposal_id,
            reason=reason,
            severity=normalized_sev,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Objection",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def vote(
        self,
        proposal_id: str,
        vote: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        normalized_vote = validate_vote(vote)
        payload = decision_pb2.VotePayload(
            proposal_id=proposal_id,
            vote=normalized_vote,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Vote",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)
