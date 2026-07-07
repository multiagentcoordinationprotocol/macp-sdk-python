from __future__ import annotations

from dataclasses import dataclass

from macp.modes.handoff.v1 import handoff_pb2
from macp.v1 import envelope_pb2

from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession
from .constants import MODE_HANDOFF
from .envelope import build_envelope, serialize_message

# ---------------------------------------------------------------------------
# Projection records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HandoffRecord:
    handoff_id: str
    target_participant: str
    scope: str
    reason: str
    sender: str
    status: str  # "offered" | "context_sent" | "accepted" | "declined"
    context_content_type: str | None
    accepted_by: str | None
    declined_by: str | None
    # True when the acceptance was an implicit accept synthesized by the
    # runtime (RFC-MACP-0010 §5.1) rather than an explicit client HandoffAccept.
    # Runtime v0.5.0 defines but does not yet emit these; the SDK surfaces the
    # field so histories that contain them replay correctly. Client-submitted
    # accepts are always ``implicit=False`` (the runtime rejects a forged True).
    implicit: bool = False


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class HandoffProjection(BaseProjection):
    """In-process state tracking for Handoff mode sessions."""

    MODE = MODE_HANDOFF

    def __init__(self) -> None:
        super().__init__()
        self.phase = "Pending"
        self.handoffs: dict[str, HandoffRecord] = {}

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "HandoffOffer":
            p = handoff_pb2.HandoffOfferPayload()
            p.ParseFromString(envelope.payload)
            self.handoffs[p.handoff_id] = HandoffRecord(
                handoff_id=p.handoff_id,
                target_participant=p.target_participant,
                scope=p.scope,
                reason=p.reason,
                sender=envelope.sender,
                status="offered",
                context_content_type=None,
                accepted_by=None,
                declined_by=None,
            )
            self.phase = "OfferPending"
            return

        if mt == "HandoffContext":
            p = handoff_pb2.HandoffContextPayload()
            p.ParseFromString(envelope.payload)
            handoff = self.handoffs.get(p.handoff_id)
            if handoff is not None:
                if handoff.status == "offered":
                    handoff.status = "context_sent"
                handoff.context_content_type = p.content_type
            if self.phase == "OfferPending":
                self.phase = "ContextSharing"
            return

        if mt == "HandoffAccept":
            p = handoff_pb2.HandoffAcceptPayload()
            p.ParseFromString(envelope.payload)
            handoff = self.handoffs.get(p.handoff_id)
            if handoff is not None:
                handoff.status = "accepted"
                handoff.accepted_by = p.accepted_by
                # macp-proto >= 0.1.6: capture whether this was a runtime
                # implicit accept (sender = target, message_id
                # ``implicit-accept:<handoff_id>``). Absent field decodes to
                # False. If ``handoff`` is None the offer was never observed
                # (e.g. mid-session subscribe) — we still advance the phase.
                handoff.implicit = getattr(p, "implicit", False)
            self.phase = "Accepted"
            return

        if mt == "HandoffDecline":
            p = handoff_pb2.HandoffDeclinePayload()
            p.ParseFromString(envelope.payload)
            handoff = self.handoffs.get(p.handoff_id)
            if handoff is not None:
                handoff.status = "declined"
                handoff.declined_by = p.declined_by
            self.phase = "Declined"

    # -- State query helpers --

    def has_accepted_offer(self, handoff_id: str | None = None) -> bool:
        """True if any offer (or a specific one) has been accepted."""
        if handoff_id is not None:
            handoff = self.handoffs.get(handoff_id)
            return handoff is not None and handoff.status == "accepted"
        return any(h.status == "accepted" for h in self.handoffs.values())

    def active_offer(self) -> HandoffRecord | None:
        """Return the most recent pending offer, or None."""
        for handoff in reversed(list(self.handoffs.values())):
            if handoff.status in ("offered", "context_sent"):
                return handoff
        return None

    def is_accepted(self, handoff_id: str) -> bool:
        handoff = self.handoffs.get(handoff_id)
        return handoff is not None and handoff.status == "accepted"

    def is_declined(self, handoff_id: str) -> bool:
        handoff = self.handoffs.get(handoff_id)
        return handoff is not None and handoff.status == "declined"

    def is_implicitly_accepted(self, handoff_id: str) -> bool:
        """True if *handoff_id* was accepted by a runtime implicit accept.

        Distinguishes a timeout-driven implicit accept (RFC-MACP-0010 §5.1)
        from an explicit client ``HandoffAccept``. Runtime v0.5.0 does not yet
        emit implicit accepts, so this returns False for all live sessions
        today; it exists so histories/replays that carry them surface the
        distinction.
        """
        handoff = self.handoffs.get(handoff_id)
        return handoff is not None and handoff.status == "accepted" and handoff.implicit

    def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
        """Return the handoff record for *handoff_id*, or None."""
        return self.handoffs.get(handoff_id)

    def pending_handoffs(self) -> list[HandoffRecord]:
        """Return handoffs that are still pending (offered or context_sent)."""
        return [h for h in self.handoffs.values() if h.status in ("offered", "context_sent")]


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class HandoffSession(BaseSession):
    """High-level helper for Handoff mode sessions."""

    MODE = MODE_HANDOFF

    def _create_projection(self) -> BaseProjection:
        return HandoffProjection()

    @property
    def handoff_projection(self) -> HandoffProjection:
        assert isinstance(self.projection, HandoffProjection)
        return self.projection

    def offer(
        self,
        handoff_id: str,
        target_participant: str,
        *,
        scope: str = "",
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = handoff_pb2.HandoffOfferPayload(
            handoff_id=handoff_id,
            target_participant=target_participant,
            scope=scope,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="HandoffOffer",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def add_context(
        self,
        handoff_id: str,
        *,
        content_type: str = "application/octet-stream",
        context: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = handoff_pb2.HandoffContextPayload(
            handoff_id=handoff_id,
            content_type=content_type,
            context=context,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="HandoffContext",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def accept_handoff(
        self,
        handoff_id: str,
        *,
        accepted_by: str = "",
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        # Deliberately no ``implicit`` parameter: RFC-MACP-0010 §5.1 reserves
        # ``implicit=true`` for runtime-synthesized accepts and the runtime
        # rejects a client-submitted True. Client accepts always leave the
        # field at its proto3 default (False). See the regression test in
        # tests/unit/test_handoff.py.
        payload = handoff_pb2.HandoffAcceptPayload(
            handoff_id=handoff_id,
            accepted_by=accepted_by or self._sender_for(sender, auth=auth),
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="HandoffAccept",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def decline(
        self,
        handoff_id: str,
        *,
        declined_by: str = "",
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        payload = handoff_pb2.HandoffDeclinePayload(
            handoff_id=handoff_id,
            declined_by=declined_by or self._sender_for(sender, auth=auth),
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="HandoffDecline",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)
