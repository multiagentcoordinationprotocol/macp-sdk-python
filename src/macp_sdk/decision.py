from __future__ import annotations

from macp.modes.decision.v1 import decision_pb2
from macp.v1 import envelope_pb2

from .async_session import AsyncBaseSession
from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession, _SessionCore
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


class _DecisionActions(_SessionCore):
    """Envelope builders shared by :class:`DecisionSession` and :class:`AsyncDecisionSession`.

    Each ``_<action>_envelope`` method validates its arguments and returns the
    envelope to send, so the sync and async helpers cannot drift apart.
    """

    MODE = MODE_DECISION

    def _create_projection(self) -> BaseProjection:
        return DecisionProjection()

    def _propose_envelope(
        self,
        proposal_id: str,
        option: str,
        *,
        rationale: str = "",
        supporting_data: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        validate_required_field("proposal_id", proposal_id)
        validate_required_field("option", option)
        payload = decision_pb2.ProposalPayload(
            proposal_id=proposal_id,
            option=option,
            rationale=rationale,
            supporting_data=supporting_data,
        )
        return build_envelope(
            mode=self.MODE,
            message_type="Proposal",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )

    def _evaluate_envelope(
        self,
        proposal_id: str,
        recommendation: str,
        *,
        confidence: float,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        validate_required_field("proposal_id", proposal_id)
        normalized_rec = validate_recommendation(recommendation)
        validate_confidence(confidence)
        payload = decision_pb2.EvaluationPayload(
            proposal_id=proposal_id,
            recommendation=normalized_rec,
            confidence=confidence,
            reason=reason,
        )
        return build_envelope(
            mode=self.MODE,
            message_type="Evaluation",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )

    def _raise_objection_envelope(
        self,
        proposal_id: str,
        *,
        reason: str,
        severity: str = "medium",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        validate_required_field("proposal_id", proposal_id)
        normalized_sev = validate_severity(severity)
        payload = decision_pb2.ObjectionPayload(
            proposal_id=proposal_id,
            reason=reason,
            severity=normalized_sev,
        )
        return build_envelope(
            mode=self.MODE,
            message_type="Objection",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )

    def _vote_envelope(
        self,
        proposal_id: str,
        vote: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        validate_required_field("proposal_id", proposal_id)
        normalized_vote = validate_vote(vote)
        payload = decision_pb2.VotePayload(
            proposal_id=proposal_id,
            vote=normalized_vote,
            reason=reason,
        )
        return build_envelope(
            mode=self.MODE,
            message_type="Vote",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )


class DecisionSession(BaseSession, _DecisionActions):
    """High-level helper for Decision mode sessions.

    Inherits ``start``, ``commit``, ``cancel``, ``metadata``, and ``open_stream``
    from :class:`BaseSession`.  Adds decision-specific actions: ``propose``,
    ``evaluate``, ``raise_objection``, and ``vote``.

    Note: The initiator must be included in the ``participants`` list passed
    to ``start()`` in order to propose.  The runtime no longer grants
    implicit proposal authority to the initiator.
    """

    MODE = MODE_DECISION

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
        return self._send_and_track(
            self._propose_envelope(
                proposal_id,
                option,
                rationale=rationale,
                supporting_data=supporting_data,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )

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
        return self._send_and_track(
            self._evaluate_envelope(
                proposal_id,
                recommendation,
                confidence=confidence,
                reason=reason,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )

    def raise_objection(
        self,
        proposal_id: str,
        *,
        reason: str,
        severity: str = "medium",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return self._send_and_track(
            self._raise_objection_envelope(
                proposal_id, reason=reason, severity=severity, sender=sender, auth=auth
            ),
            auth=auth,
        )

    def vote(
        self,
        proposal_id: str,
        vote: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return self._send_and_track(
            self._vote_envelope(proposal_id, vote, reason=reason, sender=sender, auth=auth),
            auth=auth,
        )


class AsyncDecisionSession(AsyncBaseSession, _DecisionActions):
    """Asyncio twin of :class:`DecisionSession`, driven by an
    :class:`~macp_sdk.async_client.AsyncMacpClient`.

    Same methods, arguments and validation as :class:`DecisionSession`; every
    method that talks to the runtime is a coroutine.
    """

    MODE = MODE_DECISION

    # Narrow the type for callers that want decision-specific queries.
    @property
    def decision_projection(self) -> DecisionProjection:
        assert isinstance(self.projection, DecisionProjection)
        return self.projection

    async def propose(
        self,
        proposal_id: str,
        option: str,
        *,
        rationale: str = "",
        supporting_data: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return await self._send_and_track(
            self._propose_envelope(
                proposal_id,
                option,
                rationale=rationale,
                supporting_data=supporting_data,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )

    async def evaluate(
        self,
        proposal_id: str,
        recommendation: str,
        *,
        confidence: float,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return await self._send_and_track(
            self._evaluate_envelope(
                proposal_id,
                recommendation,
                confidence=confidence,
                reason=reason,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )

    async def raise_objection(
        self,
        proposal_id: str,
        *,
        reason: str,
        severity: str = "medium",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return await self._send_and_track(
            self._raise_objection_envelope(
                proposal_id, reason=reason, severity=severity, sender=sender, auth=auth
            ),
            auth=auth,
        )

    async def vote(
        self,
        proposal_id: str,
        vote: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        return await self._send_and_track(
            self._vote_envelope(proposal_id, vote, reason=reason, sender=sender, auth=auth),
            auth=auth,
        )
