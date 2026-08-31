from __future__ import annotations

import logging

from macp.modes.decision.v1 import decision_pb2
from macp.v1 import core_pb2

from macp_sdk.base_projection import ANOMALY_DUPLICATE_VOTE
from macp_sdk.constants import MODE_DECISION
from macp_sdk.projections import DecisionProjection
from tests.conftest import make_envelope


class TestDecisionProjection:
    def _proj(self) -> DecisionProjection:
        return DecisionProjection()

    def test_initial_state(self):
        p = self._proj()
        assert p.phase == "Proposal"
        assert not p.is_committed
        assert p.vote_totals() == {}
        assert p.majority_winner() is None

    def test_proposal_does_not_advance_phase(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Proposal",
            decision_pb2.ProposalPayload(proposal_id="p1", option="opt-a", rationale="good"),
        )
        p.apply_envelope(env)
        assert "p1" in p.proposals
        assert p.phase == "Proposal"
        assert len(p.transcript) == 1

    def test_evaluation_advances_phase_to_evaluation(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Evaluation",
            decision_pb2.EvaluationPayload(
                proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="ok"
            ),
            sender="alice",
        )
        p.apply_envelope(env)
        assert p.phase == "Evaluation"

    def test_evaluation_recorded(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Evaluation",
            decision_pb2.EvaluationPayload(
                proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="ok"
            ),
            sender="alice",
        )
        p.apply_envelope(env)
        assert len(p.evaluations) == 1
        assert p.evaluations[0].recommendation == "APPROVE"

    def test_objection_recorded(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Objection",
            decision_pb2.ObjectionPayload(proposal_id="p1", reason="risk", severity="critical"),
        )
        p.apply_envelope(env)
        assert len(p.objections) == 1
        assert p.has_blocking_objection("p1")

    def test_non_blocking_objection(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Objection",
            decision_pb2.ObjectionPayload(proposal_id="p1", reason="minor", severity="low"),
        )
        p.apply_envelope(env)
        assert not p.has_blocking_objection("p1")

    def test_vote_and_totals(self):
        p = self._proj()
        for sender, vote_val in [("alice", "approve"), ("bob", "approve"), ("carol", "reject")]:
            env = make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote=vote_val, reason=""),
                sender=sender,
            )
            p.apply_envelope(env)
        assert p.phase == "Voting"
        assert p.vote_totals() == {"p1": 2}
        assert p.majority_winner() == "p1"

    def test_commitment(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Commitment",
            core_pb2.CommitmentPayload(
                commitment_id="c1",
                action="deploy",
                authority_scope="release",
                reason="approved",
            ),
        )
        p.apply_envelope(env)
        assert p.is_committed
        assert p.phase == "Committed"
        assert p.commitment is not None
        assert p.commitment.action == "deploy"

    def test_ignores_wrong_mode(self):
        p = self._proj()
        env = make_envelope(
            "macp.mode.task.v1",
            "TaskRequest",
            decision_pb2.ProposalPayload(proposal_id="p1", option="x"),
        )
        p.apply_envelope(env)
        assert len(p.transcript) == 0

    def test_abstain_excluded_from_majority(self):
        """ABSTAIN votes are excluded from the ratio denominator."""
        p = self._proj()
        for sender, vote_val in [
            ("alice", "APPROVE"),
            ("bob", "REJECT"),
            ("carol", "ABSTAIN"),
            ("dave", "ABSTAIN"),
        ]:
            env = make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote=vote_val),
                sender=sender,
            )
            p.apply_envelope(env)
        # 1 APPROVE out of 2 non-abstain votes = 50%, not > 50% so no majority
        assert p.majority_winner() is None

    def test_abstain_only_returns_none(self):
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Vote",
            decision_pb2.VotePayload(proposal_id="p1", vote="ABSTAIN"),
            sender="alice",
        )
        p.apply_envelope(env)
        assert p.majority_winner() is None

    def test_critical_only_veto(self):
        """Only critical severity triggers a veto, not high."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Objection",
                decision_pb2.ObjectionPayload(proposal_id="p1", reason="risk", severity="high"),
            )
        )
        assert not p.has_blocking_objection("p1")

        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Objection",
                decision_pb2.ObjectionPayload(
                    proposal_id="p1", reason="fatal", severity="critical"
                ),
            )
        )
        assert p.has_blocking_objection("p1")

    def test_has_blocking_objection_no_proposal_filter(self):
        """has_blocking_objection(None) checks across all proposals."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Objection",
                decision_pb2.ObjectionPayload(proposal_id="p2", reason="bad", severity="critical"),
            )
        )
        assert p.has_blocking_objection() is True

    def test_review_evaluations(self):
        p = self._proj()
        for rec in ["APPROVE", "REVIEW", "REJECT", "REVIEW"]:
            p.apply_envelope(
                make_envelope(
                    MODE_DECISION,
                    "Evaluation",
                    decision_pb2.EvaluationPayload(
                        proposal_id="p1", recommendation=rec, confidence=0.8
                    ),
                    sender=f"agent-{rec}",
                )
            )
        assert len(p.review_evaluations()) == 2
        assert len(p.qualifying_evaluations()) == 2

    def test_is_positive_outcome_not_committed(self):
        p = self._proj()
        assert p.is_positive_outcome is None

    def test_is_positive_outcome_committed(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1",
                    action="deploy",
                    authority_scope="release",
                    reason="done",
                    outcome_positive=True,
                ),
            )
        )
        assert p.is_positive_outcome is True


class TestReplayIdempotence:
    """Regression coverage for issue #43 Phase 2 — replay inflation.

    Separate bug from vote/ballot cardinality: BaseProjection.apply_envelope's
    message_id dedup guard (Phase 1) also fixes seven previously-unguarded
    ``.append(`` sites across Decision/Proposal/Task, including this file's
    ``evaluations`` (projections.py:75) and ``objections`` (projections.py:90).

    Real-world trigger: src/macp_sdk/agent/transports.py:60 subscribes with
    after_sequence defaulting to 0, so every (re)subscribe replays the full
    accepted history, and Participant.run() (participant.py:483) has no
    re-entry guard — a supervisor restarting run() re-feeds the whole history
    into the same projection object.

    | Test type   | Requires                         | How                                   |
    |-------------|-----------------------------------|----------------------------------------|
    | Redelivery  | the SAME non-empty message_id     | reuse the same envelope object, or an  |
    |             |                                    | explicit shared message_id=            |
    | Distinctness| two DIFFERENT non-empty ids       | two make_envelope(...) calls (default) |

    Distinctness is not exercised by this class — that coverage lives in
    tests/unit/test_base_projection.py::TestIdempotentApply::
    test_distinct_message_ids_both_applied.

    Every test below is a redelivery test, so every one reuses the same
    envelope object — a test that calls make_envelope twice gets two
    different uuid4 message_ids, dedup never engages, and the test would
    pass while proving nothing.
    """

    def _proj(self) -> DecisionProjection:
        return DecisionProjection()

    def test_redelivered_evaluation_is_noop(self):
        # Trigger: agent/transports.py:60 (after_sequence=0 full replay) +
        # participant.py:483 (run() has no re-entry guard).
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Evaluation",
            decision_pb2.EvaluationPayload(
                proposal_id="p1", recommendation="REVIEW", confidence=0.7, reason="needs review"
            ),
            sender="alice",
        )
        # A second, non-REVIEW evaluation so qualifying_evaluations() (which
        # filters recommendation != "REVIEW") has a genuine positive case to
        # pin — asserting it stays at 0 with an all-REVIEW feed can never go
        # red, since qualifying_evaluations() would return [] regardless of
        # whether dedup ran at all.
        qualifying_env = make_envelope(
            MODE_DECISION,
            "Evaluation",
            decision_pb2.EvaluationPayload(
                proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="looks good"
            ),
            sender="bob",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        p.apply_envelope(qualifying_env)
        p.apply_envelope(qualifying_env)
        assert len(p.evaluations) == 2
        assert len(p.review_evaluations()) == 1
        assert len(p.qualifying_evaluations()) == 1
        assert len(p.transcript) == 2

    def test_redelivered_objection_is_noop_and_predicate_unaffected(self):
        # Trigger: agent/transports.py:60 + participant.py:483.
        p = self._proj()
        env = make_envelope(
            MODE_DECISION,
            "Objection",
            decision_pb2.ObjectionPayload(proposal_id="p1", reason="risk", severity="critical"),
            sender="alice",
        )
        p.apply_envelope(env)
        # has_blocking_objection is any(...) over self.objections — it was
        # never affected by duplication, before or after the redelivery.
        assert p.has_blocking_objection("p1") is True
        p.apply_envelope(env)
        assert len(p.objections) == 1
        assert p.has_blocking_objection("p1") is True

    def test_full_history_replay_is_stable(self):
        """The run()-restart scenario: re-apply the same accepted feed."""
        # Trigger: agent/transports.py:60 + participant.py:483.
        p = self._proj()
        envelopes = [
            make_envelope(
                MODE_DECISION,
                "Proposal",
                decision_pb2.ProposalPayload(proposal_id="p1", option="opt-a", rationale="good"),
                sender="alice",
            ),
            make_envelope(
                MODE_DECISION,
                "Evaluation",
                decision_pb2.EvaluationPayload(
                    proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="ok"
                ),
                sender="bob",
            ),
            make_envelope(
                MODE_DECISION,
                "Objection",
                decision_pb2.ObjectionPayload(proposal_id="p1", reason="risk", severity="low"),
                sender="carol",
            ),
        ]
        for env in envelopes:
            p.apply_envelope(env)
        transcript_len = len(p.transcript)
        evaluations_len = len(p.evaluations)
        objections_len = len(p.objections)

        # Re-apply the SAME envelope objects, in order — simulating a
        # reconnect that replays the whole accepted history again.
        for env in envelopes:
            p.apply_envelope(env)

        assert len(p.transcript) == transcript_len
        assert len(p.evaluations) == evaluations_len
        assert len(p.objections) == objections_len


class TestVoteCardinality:
    """First-wins vote cardinality (issue #43 Phase 4), plus the D1/D3
    interlock: redelivery of the SAME envelope must never be mistaken for a
    genuine duplicate vote.

    Opposite ``message_id`` setups, easy to swap, both pass while proving
    nothing if swapped:

    | Test type   | Requires                        | How                                   |
    |-------------|-----------------------------------|----------------------------------------|
    | Cardinality | two **distinct** non-empty ids   | two ``make_envelope(...)`` calls --    |
    |             |                                   | the default uuid4 already does this    |
    | Redelivery  | the **same** non-empty id        | reuse the **same envelope object**     |
    """

    def _proj(self) -> DecisionProjection:
        return DecisionProjection()

    def test_one_sender_one_vote_per_proposal(self):
        """First Vote per sender/proposal stands; a second is discarded.

        RFC-MACP-0007 §5.3: "A participant MUST cast at most one Vote per
        proposal_id. A runtime MUST reject a second Vote from the same
        sender for the same proposal_id; the first accepted Vote stands."
        The only runtime enforces this outright
        (macp-runtime crates/macp-modes/src/mode/decision.rs:217:
        ``if proposal_votes.contains_key(&env.sender) { return
        Err(MacpError::InvalidPayload); }``), so last-wins is unreachable
        via a conforming runtime and this projection now agrees with it.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="reject"),
                sender="alice",
            )
        )
        assert p.votes["p1"]["alice"].vote == "approve"
        assert p.vote_totals() == {"p1": 1}
        assert len(p.anomalies) == 1

        anomaly = p.anomalies[0]
        assert anomaly.kind == ANOMALY_DUPLICATE_VOTE
        assert anomaly.mode == MODE_DECISION
        assert anomaly.message_type == "Vote"
        assert anomaly.sender == "alice"
        assert anomaly.subject_id == "p1"
        assert anomaly.message_id

        # The discarded envelope is recoverable via transcript -- the reason
        # ProjectionAnomaly deliberately doesn't carry the full Envelope.
        assert any(e.message_id == anomaly.message_id for e in p.transcript)

    def test_duplicate_vote_does_not_change_majority_winner(self):
        """majority_winner() under a duplicate-vote feed returns the
        first-wins answer, not one inflated by the discarded second vote."""
        p = self._proj()
        for sender, vote_val in [("alice", "approve"), ("bob", "approve"), ("carol", "reject")]:
            p.apply_envelope(
                make_envelope(
                    MODE_DECISION,
                    "Vote",
                    decision_pb2.VotePayload(proposal_id="p1", vote=vote_val),
                    sender=sender,
                )
            )
        # carol attempts to change reject -> approve; discarded.
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
                sender="carol",
            )
        )
        assert p.vote_totals() == {"p1": 2}
        assert p.majority_winner() == "p1"
        assert len(p.anomalies) == 1

    def test_redelivered_vote_records_no_anomaly(self):
        """The single most important test in the plan.

        A reconnect replays the full accepted history
        (``agent/transports.py:60``, ``after_sequence=0``). Without Phase 1's
        message_id dedup gate, every honest agent would light up with false
        duplicate_vote anomalies on every reconnect -- that false positive is
        the entire reason the dedup gate exists. This test fails outright if
        Phase 1's dedup regresses; do not weaken it.
        """
        p = self._proj()
        envelope = make_envelope(
            MODE_DECISION,
            "Vote",
            decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
            sender="alice",
        )
        p.apply_envelope(envelope)
        p.apply_envelope(envelope)  # same object -> same message_id -> redelivery
        assert p.vote_totals() == {"p1": 1}
        assert len(p.anomalies) == 0

    def test_anomaly_shape_and_warning_log(self, caplog):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
                sender="alice",
            )
        )
        with caplog.at_level(logging.WARNING, logger="macp_sdk"):
            p.apply_envelope(
                make_envelope(
                    MODE_DECISION,
                    "Vote",
                    decision_pb2.VotePayload(proposal_id="p1", vote="reject"),
                    sender="alice",
                )
            )
        assert len(caplog.records) == 1
        anomaly = p.anomalies[0]
        assert "approve" in anomaly.detail and "reject" in anomaly.detail

    def test_replay_of_transcript_reproduces_votes_and_anomalies(self):
        """Proves docs/determinism.md:53-63's replay-equivalence claim holds
        under both dedup (D1) and first-wins (D3), for ``votes``, ``phase``,
        AND ``anomalies``.

        The real invariant this guards: anomalies must be reconstructible
        from ``transcript`` alone. If an anomaly is ever recorded for an
        envelope that was NOT appended to ``transcript``, replay produces a
        fresh projection with fewer anomalies than the original, and this
        assertion catches it -- no other test in the suite does.

        (An earlier version of this docstring claimed the test also catches
        the dedup guard moving to AFTER the transcript append. Mutation
        testing disproved that: under that mutation the transcript carries
        the redelivered envelope twice, so replaying it re-triggers dedup on
        the second copy and ``anomalies`` reproduces anyway -- this test
        passes, and ten other transcript-length tests catch that mutation
        regardless. Do not re-derive that rationale.)

        Mixed transcript: one conforming vote, one redelivery of that same
        envelope (same message_id -- must NOT produce an anomaly), and one
        genuine duplicate from the same sender (distinct message_id -- MUST
        produce an anomaly).
        """
        p = self._proj()
        first_vote = make_envelope(
            MODE_DECISION,
            "Vote",
            decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
            sender="alice",
        )
        duplicate_vote = make_envelope(
            MODE_DECISION,
            "Vote",
            decision_pb2.VotePayload(proposal_id="p1", vote="reject"),
            sender="alice",
        )
        p.apply_envelope(first_vote)
        p.apply_envelope(first_vote)  # redelivery -- same message_id, no anomaly
        p.apply_envelope(duplicate_vote)  # genuine duplicate -- distinct message_id

        assert p.votes["p1"]["alice"].vote == "approve"
        assert len(p.anomalies) == 1

        replay = DecisionProjection()
        for env in p.transcript:
            replay.apply_envelope(env)

        assert replay.votes == p.votes
        assert replay.phase == p.phase
        assert replay.anomalies == p.anomalies
