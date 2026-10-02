from __future__ import annotations

import dataclasses
import logging
import warnings

import pytest
from macp.modes.quorum.v1 import quorum_pb2

from macp_sdk.base_projection import ANOMALY_DUPLICATE_BALLOT
from macp_sdk.constants import MODE_QUORUM
from macp_sdk.envelope import build_commitment_payload
from macp_sdk.quorum import ApprovalRequestRecord, QuorumProjection
from tests.conftest import make_envelope


class TestQuorumProjection:
    def _proj(self) -> QuorumProjection:
        return QuorumProjection()

    def test_initial_state(self):
        p = self._proj()
        assert p.phase == "Pending"
        assert len(p.requests) == 0
        assert p.approval_count("r1") == 0
        assert not p.has_quorum("r1")

    def test_approval_request(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1",
                    action="deploy",
                    summary="release v2",
                    required_approvals=2,
                ),
                sender="coordinator",
            )
        )
        assert "r1" in p.requests
        assert p.requests["r1"].required_approvals == 2
        assert p.requests["r1"].sender == "coordinator"
        assert p.phase == "Voting"

    def test_approve_and_threshold(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=2
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1", reason="ok"),
                sender="alice",
            )
        )
        assert p.approval_count("r1") == 1
        assert not p.has_quorum("r1")

        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1", reason="lgtm"),
                sender="bob",
            )
        )
        assert p.approval_count("r1") == 2
        assert p.has_quorum("r1")
        assert p.commitment_ready("r1")

    def test_reject_and_abstain(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=3
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Reject",
                quorum_pb2.RejectPayload(request_id="r1", reason="nope"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Abstain",
                quorum_pb2.AbstainPayload(request_id="r1", reason="neutral"),
                sender="bob",
            )
        )
        assert p.rejection_count("r1") == 1
        assert p.abstention_count("r1") == 1
        assert p.approval_count("r1") == 0

    def test_threshold_unreachable(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=3
                ),
                sender="coordinator",
            )
        )
        # 3 participants, all reject
        for sender in ["alice", "bob", "carol"]:
            p.apply_envelope(
                make_envelope(
                    MODE_QUORUM,
                    "Reject",
                    quorum_pb2.RejectPayload(request_id="r1"),
                    sender=sender,
                )
            )
        assert p.is_threshold_unreachable("r1", total_eligible=3)

    def test_late_approval_request_after_commitment_does_not_regress_phase(self):
        """Issue #93 item 5: a mode message arriving after Commitment must
        not move ``phase`` back out of "Committed". Approve/Reject/Abstain
        never touch ``phase`` in this projection (only ApprovalRequest does,
        via ``_set_phase("Voting")``), so the only message that could
        regress it is a second ApprovalRequest -- itself a precondition
        violation (RFC-MACP-0011 §5 rule 1 caps a session at one), which is
        exactly the "unfiltered feed" scenario ``_set_phase`` defends
        against. Exercised end-to-end here rather than only via the
        synthetic projection in test_base_projection.py.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=1
                ),
                sender="coordinator",
            )
        )
        commitment = build_commitment_payload(
            action="approve", authority_scope="quorum", reason="threshold met"
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Commitment",
                commitment,
                sender="coordinator",
            )
        )
        assert p.phase == "Committed"
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r2", action="y", required_approvals=1
                ),
                sender="coordinator",
            )
        )
        assert p.phase == "Committed"
        assert "r2" in p.requests  # the request's own effect is not suppressed

    def test_commitment_ready_false_after_commit(self):
        """Cross-SDK parity (matches TypeScript ``commitmentReady``):
        ``commitment_ready`` must return False once the session is Committed,
        even if the approval threshold is still met. Callers that want the
        raw "threshold reached" check should use :meth:`has_quorum`.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=1
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1"),
                sender="alice",
            )
        )
        assert p.has_quorum("r1")
        assert p.commitment_ready("r1")

        # Commit the session
        commitment = build_commitment_payload(
            action="approve", authority_scope="quorum", reason="threshold met"
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Commitment",
                commitment,
                sender="coordinator",
            )
        )
        assert p.phase == "Committed"
        # Threshold remains reached — but commitment_ready is now false
        assert p.has_quorum("r1")
        assert not p.commitment_ready("r1")

    def test_one_sender_one_ballot(self):
        """First ballot per sender stands; a second is discarded and recorded.

        RFC-MACP-0011 §5 rule 3: "Each eligible participant MAY cast at most one
        ballot across Approve, Reject, or Abstain." RFC-0011 does NOT say which of
        two stands -- first-wins is taken from parity with RFC-MACP-0007 §5.3 ("the
        first accepted Vote stands") and from what the only runtime enforces: the
        three same-sender ballot guards in macp-runtime's
        crates/macp-modes/src/mode/quorum.rs (QuorumMode's Approve/Reject/Abstain
        arms, each rejecting when state.ballots.contains_key(&env.sender),
        confirmed at runtime v0.8.6).

        WHY THE OLD INTENT WAS WRONG: this test previously asserted "latest ballot
        supersedes previous" and expected approval_count == 1. Vote-changing is not
        a behaviour the SDK may invent -- it would need a spec-level Retract or
        Supersede message with defined replacement semantics. Absent that, last-wins
        made the SDK disagree with the runtime on identical accepted history. With
        required_approvals=1 the disagreement is not cosmetic: last-wins REACHES
        quorum here and first-wins does not. Do not "fix" this test by restoring
        the old assertions.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=1
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Reject",
                quorum_pb2.RejectPayload(request_id="r1"),
                sender="alice",
            )
        )
        # Same sender attempts to change their vote -- discarded, first ballot stands.
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1"),
                sender="alice",
            )
        )
        assert p.rejection_count("r1") == 1
        assert p.approval_count("r1") == 0
        assert p.ballots["r1"]["alice"].vote == "reject"
        assert len(p.voted_senders("r1")) == 1
        # The terminal-outcome flip that is the concrete stake: with
        # required_approvals=1, last-wins would have reached quorum here.
        assert p.has_quorum("r1") is False

        assert len(p.anomalies) == 1
        anomaly = p.anomalies[0]
        assert anomaly.kind == ANOMALY_DUPLICATE_BALLOT
        assert anomaly.mode == MODE_QUORUM
        assert anomaly.message_type == "Approve"  # the discarded message
        assert anomaly.subject_id == "r1"
        assert anomaly.sender == "alice"


class TestBallotCardinality:
    """First-wins across Approve/Reject/Abstain (RFC-MACP-0011 §5 rule 3),
    plus the D1/D3 interlock: redelivery of the SAME envelope must never be
    mistaken for a genuine duplicate ballot.

    Opposite ``message_id`` setups, easy to swap, both pass while proving
    nothing if swapped:

    | Test type   | Requires                        | How                                   |
    |-------------|----------------------------------|----------------------------------------|
    | Cardinality | two **distinct** non-empty ids   | two ``make_envelope(...)`` calls --    |
    |             |                                   | the default uuid4 already does this    |
    | Redelivery  | the **same** non-empty id        | reuse the **same envelope object**     |
    """

    def _proj(self) -> QuorumProjection:
        p = QuorumProjection()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="r1", action="x", required_approvals=1
                ),
                sender="coordinator",
            )
        )
        return p

    def test_approve_then_reject(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Reject",
                quorum_pb2.RejectPayload(request_id="r1"),
                sender="alice",
            )
        )
        assert p.ballots["r1"]["alice"].vote == "approve"
        assert len(p.anomalies) == 1
        assert p.anomalies[0].message_type == "Reject"

    def test_reject_then_abstain(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Reject",
                quorum_pb2.RejectPayload(request_id="r1"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Abstain",
                quorum_pb2.AbstainPayload(request_id="r1"),
                sender="alice",
            )
        )
        assert p.ballots["r1"]["alice"].vote == "reject"
        assert len(p.anomalies) == 1
        assert p.anomalies[0].message_type == "Abstain"

    def test_abstain_then_approve(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Abstain",
                quorum_pb2.AbstainPayload(request_id="r1"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1"),
                sender="alice",
            )
        )
        assert p.ballots["r1"]["alice"].vote == "abstain"
        assert len(p.anomalies) == 1
        assert p.anomalies[0].message_type == "Approve"

    def test_redelivered_ballot_records_no_anomaly(self):
        """The single most important test in the plan (quorum half).

        A reconnect replays the full accepted history
        (``agent/transports.py:60``, ``after_sequence=0``). Without Phase 1's
        message_id dedup gate, re-applying the SAME accepted ballot on every
        reconnect would falsely fire a duplicate_ballot anomaly. This test
        fails outright if that dedup regresses -- do not weaken it.
        """
        p = self._proj()
        envelope = make_envelope(
            MODE_QUORUM,
            "Approve",
            quorum_pb2.ApprovePayload(request_id="r1"),
            sender="alice",
        )
        p.apply_envelope(envelope)
        p.apply_envelope(envelope)  # same object -> same message_id -> redelivery
        assert p.approval_count("r1") == 1
        assert len(p.anomalies) == 0

    def test_anomaly_shape_and_warning_log(self, caplog):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="r1", reason="lgtm"),
                sender="alice",
            )
        )
        with caplog.at_level(logging.WARNING, logger="macp_sdk"):
            p.apply_envelope(
                make_envelope(
                    MODE_QUORUM,
                    "Reject",
                    quorum_pb2.RejectPayload(request_id="r1", reason="changed my mind"),
                    sender="alice",
                )
            )
        assert len(caplog.records) == 1
        anomaly = p.anomalies[0]
        assert anomaly.kind == ANOMALY_DUPLICATE_BALLOT
        assert anomaly.mode == MODE_QUORUM
        assert anomaly.message_type == "Reject"
        assert anomaly.sender == "alice"
        assert anomaly.subject_id == "r1"
        assert anomaly.message_id
        assert "approve" in anomaly.detail and "reject" in anomaly.detail


class TestBallotForUnknownRequest:
    """Issue #121 Phase 4: a ballot naming a request_id this projection never
    saw an ApprovalRequest for is ignored entirely -- no ballot record, no
    anomaly. See quorum.py's _set_ballot guard and its rationale.
    """

    def _proj(self) -> QuorumProjection:
        return QuorumProjection()

    @pytest.mark.parametrize(
        ("message_type", "payload"),
        [
            ("Approve", quorum_pb2.ApprovePayload(request_id="req-unknown", reason="ok")),
            ("Reject", quorum_pb2.RejectPayload(request_id="req-unknown", reason="no")),
            ("Abstain", quorum_pb2.AbstainPayload(request_id="req-unknown", reason="")),
        ],
    )
    def test_ballot_for_unseen_request_is_ignored(self, message_type, payload):
        p = self._proj()
        p.apply_envelope(make_envelope(MODE_QUORUM, message_type, payload, sender="alice"))
        assert p.ballots == {}
        assert p.approval_count("req-unknown") == 0
        assert p.voted_senders("req-unknown") == []
        assert p.has_anomalies is False
        assert p.phase == "Pending"

    def test_approve_after_real_request_still_recorded(self):
        """The guard must not break the happy path."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="req-1", action="deploy", required_approvals=2
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="req-1", reason="ok"),
                sender="alice",
            )
        )
        assert p.approval_count("req-1") == 1

    def test_unknown_id_ballot_ignored_even_when_a_different_request_exists(self):
        """Proves the guard keys off the specific request_id, not off
        "any request exists" -- a known req-1 must not make req-unknown's
        ballots stick too."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(
                    request_id="req-1", action="deploy", required_approvals=2
                ),
                sender="coordinator",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "Approve",
                quorum_pb2.ApprovePayload(request_id="req-unknown", reason="ok"),
                sender="alice",
            )
        )
        assert "req-unknown" not in p.ballots
        assert p.approval_count("req-unknown") == 0
        assert p.has_anomalies is False


class TestRequesterDeprecatedAlias:
    """Issue #120/#121 Phase 14: ``ApprovalRequestRecord.requester`` ->
    ``.sender``, kept as a read-only INSTANCE property alias -- same
    mechanism as ``ProposalRecord.proposer`` (proposal.py), applied here to
    the second remaining ``requester`` field.
    """

    def _record(self, **overrides):
        fields = {
            "request_id": "r1",
            "action": "deploy",
            "summary": "release v2",
            "required_approvals": 2,
            "sender": "coordinator",
        }
        fields.update(overrides)
        return ApprovalRequestRecord(**fields)

    def test_sender_is_a_real_dataclass_field_requester_is_not(self):
        names = {f.name for f in dataclasses.fields(ApprovalRequestRecord)}
        assert "sender" in names
        assert "requester" not in names

    def test_requester_returns_sender_value_and_warns_every_access(self):
        record = self._record(sender="coordinator")
        for _ in range(2):  # not just once -- prove it's not a warn-once cache
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                value = record.requester
            assert value == "coordinator" == record.sender
            deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 1
            msg = str(deprecation_warnings[0].message)
            assert "ApprovalRequestRecord.requester" in msg
            assert "ApprovalRequestRecord.sender" in msg

    def test_requester_has_no_setter(self):
        record = self._record()
        with pytest.raises(AttributeError):
            record.requester = "mallory"

    def test_constructor_rejects_requester_kwarg(self):
        with pytest.raises(TypeError):
            ApprovalRequestRecord(
                request_id="r1",
                action="deploy",
                summary="release v2",
                required_approvals=2,
                requester="coordinator",
            )
