from __future__ import annotations

from macp.modes.decision.v1 import decision_pb2
from macp.v1 import core_pb2

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
