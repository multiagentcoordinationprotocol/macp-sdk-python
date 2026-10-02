from __future__ import annotations

from macp.modes.handoff.v1 import handoff_pb2
from macp.v1 import core_pb2

from macp_sdk.constants import MODE_HANDOFF
from macp_sdk.handoff import HandoffProjection
from tests.conftest import make_envelope


class TestHandoffProjection:
    def _proj(self) -> HandoffProjection:
        return HandoffProjection()

    def test_initial_state(self):
        p = self._proj()
        assert p.phase == "Pending"
        assert p.active_offer() is None
        assert not p.is_committed

    def test_late_handoff_accept_after_commitment_does_not_regress_phase(self):
        """Issue #93 item 5: a mode message arriving after Commitment must
        not move ``phase`` back out of "Committed" -- exercised end-to-end
        here rather than only via the synthetic projection in
        test_base_projection.py.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1", action="commit", authority_scope="session"
                ),
            )
        )
        assert p.phase == "Committed"
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="bob"),
                sender="bob",
            )
        )
        assert p.phase == "Committed"
        assert p.handoffs["h1"].status == "accepted"  # the accept's own effect is not suppressed

    def test_offer(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(
                    handoff_id="h1",
                    target_participant="bob",
                    scope="service-xyz",
                    reason="rotating",
                ),
                sender="alice",
            )
        )
        assert "h1" in p.handoffs
        assert p.handoffs["h1"].status == "offered"
        assert p.active_offer() is not None
        assert p.phase == "OfferPending"

    def test_context(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffContext",
                handoff_pb2.HandoffContextPayload(
                    handoff_id="h1",
                    content_type="application/json",
                    context=b'{"key":"val"}',
                ),
                sender="alice",
            )
        )
        handoff = p.get_handoff("h1")
        assert handoff is not None
        assert handoff.context_content_type == "application/json"
        assert handoff.status == "context_sent"
        assert p.phase == "ContextSharing"

    def test_accept(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="bob"),
                sender="bob",
            )
        )
        assert p.is_accepted("h1")
        assert not p.is_declined("h1")
        assert p.phase == "Accepted"

    def test_decline(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffDecline",
                handoff_pb2.HandoffDeclinePayload(
                    handoff_id="h1", declined_by="bob", reason="not ready"
                ),
                sender="bob",
            )
        )
        assert p.is_declined("h1")
        assert not p.is_accepted("h1")
        assert p.phase == "Declined"

    def test_accept_unknown_handoff_is_noop(self):
        """A HandoffAccept referencing a handoff_id never offered must not
        raise and must not move ``phase`` (RFC-MACP-0010 §5 rule 2). Also
        must NOT record a ProjectionAnomaly (issue #94) -- an unknown
        handoff_id can legitimately mean a projection that joined
        mid-session and never saw the offer, which is not caller misuse.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="ghost", accepted_by="bob"),
                sender="bob",
            )
        )
        assert p.get_handoff("ghost") is None
        assert p.phase == "Pending"
        assert p.anomalies == []

    def test_decline_unknown_handoff_is_noop(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffDecline",
                handoff_pb2.HandoffDeclinePayload(handoff_id="ghost", declined_by="bob"),
                sender="bob",
            )
        )
        assert p.get_handoff("ghost") is None
        assert p.phase == "Pending"
        assert p.anomalies == []

    def test_accept_after_accepted_is_noop(self):
        """A competing HandoffAccept after the handoff is already accepted
        does not change ``accepted_by`` or ``phase`` (RFC-MACP-0010 §5 rule
        4 / §5.1(4) — settle once), and records a ``settled_handoff``
        ProjectionAnomaly (issue #94) -- unlike an unknown handoff_id
        (test_accept_unknown_handoff_is_noop above), this IS caller misuse.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="bob"),
                sender="bob",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="carol"),
                sender="carol",
            )
        )
        handoff = p.get_handoff("h1")
        assert handoff is not None
        assert handoff.accepted_by == "bob"
        assert p.phase == "Accepted"
        assert len(p.anomalies) == 1
        anomaly = p.anomalies[0]
        assert anomaly.kind == "settled_handoff"
        assert anomaly.subject_id == "h1"
        assert anomaly.sender == "carol"

    def test_decline_after_accepted_is_noop(self):
        """A HandoffDecline after acceptance must not flip status back, and
        records a ``settled_handoff`` ProjectionAnomaly (issue #94)."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffAccept",
                handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="bob"),
                sender="bob",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffDecline",
                handoff_pb2.HandoffDeclinePayload(handoff_id="h1", declined_by="bob"),
                sender="bob",
            )
        )
        handoff = p.get_handoff("h1")
        assert handoff is not None
        assert handoff.status == "accepted"
        assert p.phase == "Accepted"
        assert len(p.anomalies) == 1
        anomaly = p.anomalies[0]
        assert anomaly.kind == "settled_handoff"
        assert anomaly.subject_id == "h1"
        assert anomaly.sender == "bob"

    def test_second_decline_is_noop(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffDecline",
                handoff_pb2.HandoffDeclinePayload(handoff_id="h1", declined_by="bob"),
                sender="bob",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffDecline",
                handoff_pb2.HandoffDeclinePayload(handoff_id="h1", declined_by="carol"),
                sender="carol",
            )
        )
        handoff = p.get_handoff("h1")
        assert handoff is not None
        assert handoff.declined_by == "bob"
        assert p.phase == "Declined"
        assert len(p.anomalies) == 1
        assert p.anomalies[0].kind == "settled_handoff"


class TestReplayIdempotence:
    """Replay/resubscribe must reach the same final state as a live feed —
    the settle-once fix is a pure function of transcript order, so
    redelivering the same envelopes must not double-apply it.
    """

    def _proj(self) -> HandoffProjection:
        return HandoffProjection()

    def test_replayed_offer_after_accept_is_deterministic(self):
        """Redelivering the original HandoffOffer after the handoff has
        since been accepted must stay a no-op (dedup by message_id) —
        without dedup, HandoffOffer unconditionally overwrites the record
        (``self.handoffs[p.handoff_id] = HandoffRecord(..., status="offered")``),
        which would incorrectly regress an already-accepted handoff back to
        "offered". This is what makes the redelivery genuinely exercise the
        dedup guard rather than passing vacuously (the settle-once guard
        alone wouldn't catch a replayed *offer*, only a replayed accept/decline).
        """
        p = self._proj()
        offer = make_envelope(
            MODE_HANDOFF,
            "HandoffOffer",
            handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
            sender="alice",
        )
        accept = make_envelope(
            MODE_HANDOFF,
            "HandoffAccept",
            handoff_pb2.HandoffAcceptPayload(handoff_id="h1", accepted_by="bob"),
            sender="bob",
        )
        p.apply_envelope(offer)
        p.apply_envelope(accept)
        assert p.phase == "Accepted"
        # Redeliver the original offer (same envelope object — same
        # message_id, already recorded). Without dedup this would reset
        # status back to "offered" and phase back to "OfferPending".
        p.apply_envelope(offer)
        handoff = p.get_handoff("h1")
        assert handoff is not None
        assert handoff.status == "accepted"
        assert handoff.accepted_by == "bob"
        assert p.phase == "Accepted"
        assert len(p.transcript) == 2


class TestHandoffContextForUnknownHandoff:
    """Issue #121 Phase 4: a HandoffContext naming an unknown handoff_id must
    not advance the phase from "OfferPending" to "ContextSharing". Same
    guard shape as issue #119's fix to proposal.py's Reject branch.
    """

    def _proj(self) -> HandoffProjection:
        return HandoffProjection()

    def test_context_for_unseen_handoff_does_not_advance_phase(self):
        # Non-regression sanity check, NOT the guard-detecting case: with no
        # prior HandoffOffer, phase starts at "Pending" (test_initial_state)
        # and the pre-fix code's "if self.phase == OfferPending" was already
        # False here regardless of the guard, so this alone cannot catch a
        # regression. See test_context_for_unseen_handoff_after_real_offer_
        # does_not_advance_phase below for the case that actually exercises
        # the fix (phase already OfferPending from a real offer).
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffContext",
                handoff_pb2.HandoffContextPayload(
                    handoff_id="h-unknown", content_type="text/plain"
                ),
                sender="bob",
            )
        )
        assert p.phase == "Pending"
        assert p.handoffs == {}

    def test_context_for_unseen_handoff_after_real_offer_does_not_advance_phase(self):
        """The actual regression test: once a real HandoffOffer has already
        moved phase to "OfferPending", a HandoffContext for a DIFFERENT,
        unknown handoff_id must not flip it to "ContextSharing". Pre-fix,
        the phase check ran unconditionally on `self.phase`, oblivious to
        which handoff_id the context named, so this failed before the fix
        (phase went to "ContextSharing") and passes after it.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h-1", target_participant="bob"),
            )
        )
        assert p.phase == "OfferPending"
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffContext",
                handoff_pb2.HandoffContextPayload(handoff_id="h-other", content_type="text/plain"),
                sender="bob",
            )
        )
        assert p.phase == "OfferPending"
        assert "h-other" not in p.handoffs

    def test_context_for_real_handoff_still_advances_phase(self):
        """The guard must not break the happy path."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h-1", target_participant="bob"),
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_HANDOFF,
                "HandoffContext",
                handoff_pb2.HandoffContextPayload(handoff_id="h-1", content_type="text/plain"),
                sender="alice",
            )
        )
        assert p.phase == "ContextSharing"
        handoff = p.get_handoff("h-1")
        assert handoff is not None
        assert handoff.status == "context_sent"
        assert handoff.context_content_type == "text/plain"
