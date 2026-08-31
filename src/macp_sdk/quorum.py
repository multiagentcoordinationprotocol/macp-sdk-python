from __future__ import annotations

from dataclasses import dataclass

from macp.modes.quorum.v1 import quorum_pb2
from macp.v1 import envelope_pb2

from .auth import AuthConfig
from .base_projection import ANOMALY_DUPLICATE_BALLOT, BaseProjection
from .base_session import BaseSession
from .constants import MODE_QUORUM
from .envelope import build_envelope, serialize_message

# ---------------------------------------------------------------------------
# Projection records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApprovalRequestRecord:
    request_id: str
    action: str
    summary: str
    required_approvals: int
    requester: str


@dataclass(slots=True)
class BallotRecord:
    request_id: str
    vote: str  # "approve" | "reject" | "abstain"
    reason: str
    sender: str


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class QuorumProjection(BaseProjection):
    """In-process state tracking for Quorum mode sessions.

    Supports multiple concurrent approval requests within a single session.
    Query methods accept a ``request_id`` parameter to target a specific
    request.
    """

    MODE = MODE_QUORUM

    def __init__(self) -> None:
        super().__init__()
        self.phase = "Pending"
        self.requests: dict[str, ApprovalRequestRecord] = {}
        self.ballots: dict[str, dict[str, BallotRecord]] = {}  # request_id -> sender -> ballot

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "ApprovalRequest":
            p = quorum_pb2.ApprovalRequestPayload()
            p.ParseFromString(envelope.payload)
            self.requests[p.request_id] = ApprovalRequestRecord(
                request_id=p.request_id,
                action=p.action,
                summary=p.summary,
                required_approvals=p.required_approvals,
                requester=envelope.sender,
            )
            self.phase = "Voting"
            return

        if mt == "Approve":
            p = quorum_pb2.ApprovePayload()
            p.ParseFromString(envelope.payload)
            self._set_ballot(envelope, p.request_id, "approve", p.reason)
            return

        if mt == "Reject":
            p = quorum_pb2.RejectPayload()
            p.ParseFromString(envelope.payload)
            self._set_ballot(envelope, p.request_id, "reject", p.reason)
            return

        if mt == "Abstain":
            p = quorum_pb2.AbstainPayload()
            p.ParseFromString(envelope.payload)
            self._set_ballot(envelope, p.request_id, "abstain", p.reason)

    def _set_ballot(
        self, envelope: envelope_pb2.Envelope, request_id: str, vote: str, reason: str
    ) -> None:
        # Single funnel for Approve/Reject/Abstain -- this is exactly what
        # enforces RFC-MACP-0011 §5 rule 3's *across-type* cardinality rule
        # ("at most one ballot across Approve, Reject, or Abstain"). First
        # ballot per sender stands; which-of-two-stands is inferred from
        # RFC-MACP-0007 §5.3 parity plus what the only runtime enforces
        # (quorum.rs:164/184/204) -- RFC-0011 itself is silent on that.
        sender = envelope.sender
        sender_map = self.ballots.setdefault(request_id, {})
        existing = sender_map.get(sender)
        if existing is not None:
            # detail states facts only, no cause attribution. message_type
            # here is the DISCARDED message's type, not the first ballot's.
            self._record_anomaly(
                kind=ANOMALY_DUPLICATE_BALLOT,
                message_type=envelope.message_type,
                message_id=envelope.message_id,
                sender=sender,
                subject_id=request_id,
                detail=f"kept first ballot {existing.vote!r}; discarded {vote!r}",
            )
            return
        sender_map[sender] = BallotRecord(
            request_id=request_id,
            vote=vote,
            reason=reason,
            sender=sender,
        )

    def _count_votes(self, request_id: str, vote: str) -> int:
        sender_map = self.ballots.get(request_id)
        if not sender_map:
            return 0
        return sum(1 for b in sender_map.values() if b.vote == vote)

    # -- State query helpers --

    def approval_count(self, request_id: str) -> int:
        return self._count_votes(request_id, "approve")

    def rejection_count(self, request_id: str) -> int:
        return self._count_votes(request_id, "reject")

    def abstention_count(self, request_id: str) -> int:
        return self._count_votes(request_id, "abstain")

    def has_quorum(self, request_id: str) -> bool:
        """True if the approval count has reached the required threshold.

        Canonical name across SDKs (parity with TypeScript ``hasQuorum``).
        """
        req = self.requests.get(request_id)
        if req is None:
            return False
        return self.approval_count(request_id) >= req.required_approvals

    def is_threshold_unreachable(self, request_id: str, total_eligible: int) -> bool:
        """True if remaining possible approvals cannot reach the threshold."""
        req = self.requests.get(request_id)
        if req is None:
            return False
        remaining = total_eligible - len(self.voted_senders(request_id))
        return self.approval_count(request_id) + remaining < req.required_approvals

    def commitment_ready(self, request_id: str) -> bool:
        """True if quorum is reached and commit has not yet been issued.

        Canonical cross-SDK semantics (parity with TypeScript
        ``QuorumProjection.commitmentReady``): ``has_quorum`` alone is
        insufficient — once the session has moved to the ``Committed``
        phase, the commit is no longer "ready" because it has already
        happened. Callers that want the pure threshold-reached check
        should use :meth:`has_quorum`.
        """
        return self.has_quorum(request_id) and self.phase != "Committed"

    def threshold(self, request_id: str) -> int:
        """Return the required approval count, or 0 if no request yet."""
        req = self.requests.get(request_id)
        return req.required_approvals if req else 0

    def voted_senders(self, request_id: str) -> list[str]:
        """Return list of senders who have cast a ballot for this request."""
        sender_map = self.ballots.get(request_id)
        return list(sender_map.keys()) if sender_map else []

    def remaining_votes_needed(self, request_id: str) -> int:
        """Return how many more approvals are needed to reach quorum."""
        req = self.requests.get(request_id)
        if req is None:
            return 0
        return max(0, req.required_approvals - self.approval_count(request_id))


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class QuorumSession(BaseSession):
    """High-level helper for Quorum mode sessions."""

    MODE = MODE_QUORUM

    def _create_projection(self) -> BaseProjection:
        return QuorumProjection()

    @property
    def quorum_projection(self) -> QuorumProjection:
        assert isinstance(self.projection, QuorumProjection)
        return self.projection

    def request_approval(
        self,
        request_id: str,
        action: str,
        *,
        summary: str = "",
        details: bytes = b"",
        required_approvals: int,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = quorum_pb2.ApprovalRequestPayload(
            request_id=request_id,
            action=action,
            summary=summary,
            details=details,
            required_approvals=required_approvals,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="ApprovalRequest",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def approve(
        self,
        request_id: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = quorum_pb2.ApprovePayload(
            request_id=request_id,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Approve",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def reject(
        self,
        request_id: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = quorum_pb2.RejectPayload(
            request_id=request_id,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Reject",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def abstain(
        self,
        request_id: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = quorum_pb2.AbstainPayload(
            request_id=request_id,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Abstain",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)
