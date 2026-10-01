from __future__ import annotations

import importlib
import warnings
from typing import Any
from unittest.mock import MagicMock

from macp.modes.decision.v1 import decision_pb2

from macp_sdk.agent.strategies import (
    CommitmentResult,
    EvaluationResult,
    VoteResult,
    commitment_handler,
    evaluation_handler,
    function_committer,
    function_evaluator,
    function_voter,
    majority_committer,
    majority_voter,
    voting_handler,
)
from macp_sdk.agent.types import (
    HandlerContext,
    IncomingMessage,
    SessionInfo,
)
from macp_sdk.constants import MODE_DECISION
from macp_sdk.errors import MacpSdkError, MacpSessionError
from macp_sdk.projections import DecisionEvaluationRecord, DecisionProjection
from tests.conftest import make_envelope


def _make_message(
    message_type: str = "Proposal",
    payload: dict[str, Any] | None = None,
    proposal_id: str = "prop-1",
) -> IncomingMessage:
    return IncomingMessage(
        message_type=message_type,
        sender="agent-a",
        payload=payload or {"option": "deploy"},
        proposal_id=proposal_id,
    )


def _make_context(projection: Any = None) -> HandlerContext:
    logs: list[str] = []

    def log_fn(fmt: str, *args: Any) -> None:
        logs.append(fmt % args if args else fmt)

    actions = MagicMock()

    ctx = HandlerContext(
        participant="test-participant",
        projection=projection,
        actions=actions,
        session=SessionInfo(session_id="s1", mode="macp.mode.decision.v1"),
        log_fn=log_fn,
    )
    ctx._test_logs = logs  # type: ignore[attr-defined]
    return ctx


class TestEvaluationStrategy:
    def test_function_evaluator(self):
        def eval_fn(proposal: dict[str, Any], context: SessionInfo) -> EvaluationResult:
            return EvaluationResult(
                recommendation="APPROVE",
                confidence=0.95,
                reason="looks good",
            )

        strategy = function_evaluator(eval_fn)
        result = strategy.evaluate({"option": "deploy"}, SessionInfo("s1", "m1"))
        assert result.recommendation == "APPROVE"
        assert result.confidence == 0.95
        assert result.reason == "looks good"

    def test_evaluation_handler(self):
        strategy = function_evaluator(lambda p, c: EvaluationResult("REJECT", 0.3, "risky"))
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        handler(_make_message(), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 1
        assert "REJECT" in logs[0]
        assert "0.30" in logs[0]
        ctx.actions.evaluate.assert_called_once_with(
            "prop-1",
            "REJECT",
            confidence=0.3,
            reason="risky",
        )

    def test_evaluation_handler_ignores_wrong_message_type(self):
        """Phase 2 item 3: evaluation_handler must not fire on an
        Evaluation or Vote message -- only Proposal."""
        strategy = function_evaluator(lambda p, c: EvaluationResult("APPROVE", 0.9, "fine"))
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Evaluation"), ctx)
        handler(_make_message(message_type="Vote"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 0
        ctx.actions.evaluate.assert_not_called()

    def test_evaluation_result_frozen(self):
        r = EvaluationResult("APPROVE", 0.9, "ok")
        try:
            r.recommendation = "REJECT"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass


class TestVotingStrategy:
    def test_function_voter(self):
        strategy = function_voter(
            should_vote_fn=lambda p: True,
            decide_fn=lambda p: VoteResult("approve", "good proposal"),
        )
        assert strategy.should_vote(None) is True
        decision = strategy.decide_vote(None)
        assert decision.vote == "approve"
        assert decision.reason == "good proposal"

    def test_voting_handler_votes_when_ready(self):
        strategy = function_voter(
            should_vote_fn=lambda p: True,
            decide_fn=lambda p: VoteResult("approve", "all clear"),
        )
        handler = voting_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Evaluation"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 1
        assert "approve" in logs[0]
        ctx.actions.vote.assert_called_once_with(
            "prop-1",
            "approve",
            reason="all clear",
        )

    def test_voting_handler_skips_when_not_ready(self):
        strategy = function_voter(
            should_vote_fn=lambda p: False,
            decide_fn=lambda p: VoteResult("approve", ""),
        )
        handler = voting_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Evaluation"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 0
        ctx.actions.vote.assert_not_called()

    def test_voting_handler_ignores_wrong_message_type(self):
        """Phase 2 item 3: voting_handler must not fire on a Proposal
        message, even one that would otherwise pass should_vote()."""
        strategy = function_voter(
            should_vote_fn=lambda p: True,
            decide_fn=lambda p: VoteResult("approve", "all clear"),
        )
        handler = voting_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Proposal"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 0
        ctx.actions.vote.assert_not_called()

    def test_vote_result_frozen(self):
        d = VoteResult("approve", "ok")
        try:
            d.vote = "reject"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass


class TestCommitmentStrategy:
    def test_function_committer(self):
        strategy = function_committer(
            should_commit_fn=lambda p: True,
            decide_fn=lambda p: CommitmentResult("deploy", "release", "quorum met"),
        )
        assert strategy.should_commit(None) is True
        decision = strategy.decide_commitment(None)
        assert decision.action == "deploy"
        assert decision.authority_scope == "release"
        assert decision.reason == "quorum met"

    def test_commitment_handler_commits_when_ready(self):
        strategy = function_committer(
            should_commit_fn=lambda p: True,
            decide_fn=lambda p: CommitmentResult("approve", "full", "done"),
        )
        handler = commitment_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Vote"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 1
        assert "approve" in logs[0]
        assert "full" in logs[0]
        ctx.actions.commit.assert_called_once_with(
            "approve",
            "full",
            reason="done",
            outcome_positive=True,
        )

    def test_commitment_handler_skips_when_not_ready(self):
        strategy = function_committer(
            should_commit_fn=lambda p: False,
            decide_fn=lambda p: CommitmentResult("x", "y", "z"),
        )
        handler = commitment_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Vote"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 0
        ctx.actions.commit.assert_not_called()

    def test_commitment_handler_ignores_wrong_message_type(self):
        """Phase 2 item 3: commitment_handler must not fire on a Proposal
        message, even one that would otherwise pass should_commit()."""
        strategy = function_committer(
            should_commit_fn=lambda p: True,
            decide_fn=lambda p: CommitmentResult("approve", "full", "done"),
        )
        handler = commitment_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Proposal"), ctx)
        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 0
        ctx.actions.commit.assert_not_called()

    def test_commitment_handler_infers_outcome_positive_when_unset(self):
        """Phase 2 item 4: a CommitmentResult that leaves
        outcome_positive unset must get it inferred from the action name,
        not silently default to True."""
        strategy = function_committer(
            should_commit_fn=lambda p: True,
            decide_fn=lambda p: CommitmentResult("task_rejected", "full", "no quorum"),
        )
        handler = commitment_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Vote"), ctx)
        ctx.actions.commit.assert_called_once_with(
            "task_rejected",
            "full",
            reason="no quorum",
            outcome_positive=False,
        )

    def test_commitment_handler_respects_explicit_outcome_positive(self):
        """An explicit outcome_positive=False always wins over inference,
        even for an action name that would infer True."""
        strategy = function_committer(
            should_commit_fn=lambda p: True,
            decide_fn=lambda p: CommitmentResult(
                "approve", "full", "manual override", outcome_positive=False
            ),
        )
        handler = commitment_handler(strategy)
        ctx = _make_context()
        handler(_make_message(message_type="Vote"), ctx)
        ctx.actions.commit.assert_called_once_with(
            "approve",
            "full",
            reason="manual override",
            outcome_positive=False,
        )

    def test_commitment_result_frozen(self):
        d = CommitmentResult("a", "b", "c")
        try:
            d.action = "x"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass


class TestStrategyComposition:
    """Test that strategies can be composed together on a single participant handler chain."""

    def test_evaluation_then_voting(self):
        eval_strategy = function_evaluator(lambda p, c: EvaluationResult("APPROVE", 0.9, "fine"))
        vote_strategy = function_voter(
            should_vote_fn=lambda p: True,
            decide_fn=lambda p: VoteResult("approve", "evaluation passed"),
        )
        eval_h = evaluation_handler(eval_strategy)
        vote_h = voting_handler(vote_strategy)

        ctx = _make_context()
        # Two distinctly-typed messages: evaluation_handler only fires on
        # Proposal, voting_handler only fires on Evaluation, so no single
        # message satisfies both gates (Phase 2 item 3).
        proposal_msg = _make_message(message_type="Proposal")
        evaluation_msg = _make_message(message_type="Evaluation")
        eval_h(proposal_msg, ctx)
        vote_h(evaluation_msg, ctx)

        logs = ctx._test_logs  # type: ignore[attr-defined]
        assert len(logs) == 2
        assert "APPROVE" in logs[0]
        assert "approve" in logs[1]


class TestMajorityVoter:
    def _mock_projection(
        self,
        totals: dict[str, int],
        winner: str | None = None,
        evaluations: list[Any] | None = None,
    ):
        proj = MagicMock()
        proj.vote_totals.return_value = totals
        proj.majority_winner.return_value = winner
        # Explicit, not a bare MagicMock() attribute: an un-configured
        # MagicMock attribute auto-vivifies as a (truthy) MagicMock, which
        # would make should_vote's `bool(projection.evaluations)` check
        # meaningless in these tests.
        proj.evaluations = evaluations if evaluations is not None else []
        return proj

    def test_should_vote_true_once_an_evaluation_exists(self):
        """Issue #93 item 4: should_vote gates on Evaluations, not on votes
        already cast -- even with zero votes so far."""
        strategy = majority_voter()
        proj = self._mock_projection({}, evaluations=[MagicMock()])
        assert strategy.should_vote(proj) is True

    def test_should_vote_false_with_no_evaluations(self):
        strategy = majority_voter()
        proj = self._mock_projection({})
        assert strategy.should_vote(proj) is False

    def test_should_vote_ignores_existing_vote_totals(self):
        """Votes alone (no Evaluation recorded) must not trigger should_vote
        -- the old vote_totals()-based gate is gone entirely."""
        strategy = majority_voter()
        proj = self._mock_projection({"approve": 3, "reject": 1}, "approve")
        assert strategy.should_vote(proj) is False

    def test_should_vote_none_projection(self):
        strategy = majority_voter()
        assert strategy.should_vote(None) is False

    @staticmethod
    def _evaluation(proposal_id: str, recommendation: str, sender: str = "alice"):
        return DecisionEvaluationRecord(
            proposal_id=proposal_id,
            recommendation=recommendation,
            confidence=0.9,
            reason="",
            sender=sender,
        )

    def test_decide_vote_with_winner(self):
        """decide_vote reads evaluations, not votes (#97 follow-up to #93
        item 4) -- 3/3 APPROVE evaluations for 'deploy-v2' meets the default
        0.5 threshold."""
        strategy = majority_voter()
        proj = self._mock_projection(
            {},
            evaluations=[
                self._evaluation("deploy-v2", "APPROVE", "a"),
                self._evaluation("deploy-v2", "APPROVE", "b"),
                self._evaluation("deploy-v2", "APPROVE", "c"),
            ],
        )
        decision = strategy.decide_vote(proj)
        assert decision.vote == "APPROVE"
        assert "deploy-v2" in decision.reason

    def test_decide_vote_no_winner(self):
        """Below-threshold evaluations ABSTAIN rather than APPROVE."""
        strategy = majority_voter()
        proj = self._mock_projection(
            {},
            evaluations=[
                self._evaluation("deploy-v2", "APPROVE", "a"),
                self._evaluation("deploy-v2", "REJECT", "b"),
                self._evaluation("deploy-v2", "REJECT", "c"),
            ],
        )
        decision = strategy.decide_vote(proj)
        assert decision.vote == "ABSTAIN"

    def test_decide_vote_no_evaluations(self):
        strategy = majority_voter()
        proj = self._mock_projection({}, evaluations=[])
        decision = strategy.decide_vote(proj)
        assert decision.vote == "ABSTAIN"
        assert "no evaluations" in decision.reason

    def test_decide_vote_uses_most_recently_evaluated_proposal(self):
        """An earlier proposal's (rejected) evaluations must not leak into
        the decision for the proposal actually being evaluated now."""
        strategy = majority_voter()
        proj = self._mock_projection(
            {},
            evaluations=[
                self._evaluation("p1", "REJECT", "a"),
                self._evaluation("p1", "REJECT", "b"),
                self._evaluation("p2", "APPROVE", "a"),
            ],
        )
        decision = strategy.decide_vote(proj)
        assert decision.vote == "APPROVE"
        assert "p2" in decision.reason
        assert "p1" not in decision.reason

    def test_decide_vote_excludes_review_recommendation(self):
        """A REVIEW evaluation is informational only -- it must not count
        toward either the numerator or denominator of the approval ratio."""
        strategy = majority_voter()
        proj = self._mock_projection(
            {},
            evaluations=[
                self._evaluation("p1", "APPROVE", "a"),
                self._evaluation("p1", "REVIEW", "b"),
            ],
        )
        decision = strategy.decide_vote(proj)
        # 1/1 qualifying (REVIEW excluded) approves -> meets 0.5 threshold.
        assert decision.vote == "APPROVE"

    def test_decide_vote_all_review_has_no_qualifying_evaluations(self):
        """The no-qualifying-evaluations path (every evaluation for the
        proposal is REVIEW) is distinct from the no-evaluations-at-all
        path -- both ABSTAIN, but for different stated reasons."""
        strategy = majority_voter()
        proj = self._mock_projection(
            {},
            evaluations=[
                self._evaluation("p1", "REVIEW", "a"),
                self._evaluation("p1", "REVIEW", "b"),
            ],
        )
        decision = strategy.decide_vote(proj)
        assert decision.vote == "ABSTAIN"
        assert "no qualifying evaluations" in decision.reason

    def test_custom_threshold(self):
        """positive_threshold gates decide_vote's evaluation ratio (#97
        follow-up): the old test of this name gated should_vote on a ratio
        of already-cast votes, which was itself the deadlock this strategy
        no longer has -- rewritten against the fixed, evaluations-based
        design."""
        strategy = majority_voter(positive_threshold=0.9)

        proj = self._mock_projection(
            {},
            evaluations=[self._evaluation("p1", "APPROVE", f"p{i}") for i in range(6)]
            + [self._evaluation("p1", "REJECT", f"r{i}") for i in range(4)],
        )
        # 6/10 = 0.6, below 0.9 threshold.
        assert strategy.decide_vote(proj).vote == "ABSTAIN"

        proj2 = self._mock_projection(
            {},
            evaluations=[self._evaluation("p1", "APPROVE", f"p{i}") for i in range(10)]
            + [self._evaluation("p1", "REJECT", "r0")],
        )
        # 10/11 = 0.91, above 0.9 threshold.
        assert strategy.decide_vote(proj2).vote == "APPROVE"

    def test_all_majority_voter_session_reaches_a_first_vote(self):
        """Regression for issue #93 item 4: a Decision session in which
        every participant runs majority_voter must be able to cast a first
        vote at all -- before this fix, vote_totals() started empty, so
        should_vote() was false for everyone, forever (a deadlock).

        Exercises a real DecisionProjection (not a MagicMock), driven the
        way voting_handler actually drives it: should_vote() is checked
        right after an Evaluation lands, with zero votes cast yet.
        """
        p = DecisionProjection()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Proposal",
                decision_pb2.ProposalPayload(proposal_id="p1", option="deploy"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Evaluation",
                decision_pb2.EvaluationPayload(
                    proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="ok"
                ),
                sender="alice",
            )
        )
        assert p.vote_totals() == {}  # no vote cast yet -- the old gate's deadlock condition

        strategy = majority_voter()
        assert strategy.should_vote(p) is True
        # #97 follow-up: reaching should_vote()==True is necessary but not
        # sufficient -- the original #93 fix left decide_vote() reading
        # majority_winner() (votes), so it ABSTAINed forever even once a
        # vote could be *attempted*. decide_vote() must actually resolve
        # from the evaluation just applied, with zero votes cast.
        decision = strategy.decide_vote(p)
        assert decision.vote == "APPROVE"

    def test_all_majority_voter_session_reaches_commitment(self):
        """Regression for the deadlock an independent post-merge review
        found: a Decision session where every participant runs
        majority_voter + majority_committer must be able to reach an
        actual commitment, not merely cast a non-abstaining first vote.

        With decide_vote() reading votes instead of evaluations, three
        all-ABSTAIN votes are a stable fixed point (ABSTAIN is excluded
        from vote_totals()/majority_winner()'s denominator), so
        majority_committer.should_commit() never becomes True. Driving
        decide_vote() from evaluations breaks that fixed point.

        Exercises the strategies directly against one shared
        DecisionProjection (three participant identities, not three
        Participant/dispatcher instances) -- should_vote/decide_vote/
        should_commit/decide_commitment is exactly the call sequence
        voting_handler/commitment_handler make, so this is a faithful
        driver of the real deadlock without the I/O from_bootstrap()
        would need. action="deploy" (not majority_committer's own
        default "commit") so the final assertion actually exercises
        decide_commitment()'s return value instead of restating the
        default.
        """
        p = DecisionProjection()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Proposal",
                decision_pb2.ProposalPayload(proposal_id="p1", option="deploy"),
                sender="planner",
            )
        )
        voter = majority_voter()
        committer = majority_committer(quorum_size=3, action="deploy")

        for sender in ("alice", "bob", "carol"):
            p.apply_envelope(
                make_envelope(
                    MODE_DECISION,
                    "Evaluation",
                    decision_pb2.EvaluationPayload(
                        proposal_id="p1", recommendation="APPROVE", confidence=0.9, reason="ok"
                    ),
                    sender=sender,
                )
            )
            assert voter.should_vote(p) is True
            decision = voter.decide_vote(p)
            assert decision.vote == "APPROVE", "must not be a permanent ABSTAIN fixed point"
            p.apply_envelope(
                make_envelope(
                    MODE_DECISION,
                    "Vote",
                    decision_pb2.VotePayload(
                        proposal_id="p1", vote=decision.vote, reason=decision.reason
                    ),
                    sender=sender,
                )
            )

        assert committer.should_commit(p) is True
        assert committer.decide_commitment(p).action == "deploy"


class TestMajorityCommitter:
    def _mock_projection(self, totals: dict[str, int], winner: str | None = None):
        proj = MagicMock()
        proj.vote_totals.return_value = totals
        proj.majority_winner.return_value = winner
        return proj

    def test_should_commit_with_quorum_and_winner(self):
        strategy = majority_committer(quorum_size=2)
        proj = self._mock_projection({"approve": 3}, "deploy")
        assert strategy.should_commit(proj) is True

    def test_should_commit_below_quorum(self):
        strategy = majority_committer(quorum_size=5)
        proj = self._mock_projection({"approve": 3}, "deploy")
        assert strategy.should_commit(proj) is False

    def test_should_commit_no_winner(self):
        strategy = majority_committer(quorum_size=1)
        proj = self._mock_projection({"approve": 2, "reject": 2})
        assert strategy.should_commit(proj) is False

    def test_should_commit_none_projection(self):
        strategy = majority_committer()
        assert strategy.should_commit(None) is False

    def test_decide_commitment(self):
        strategy = majority_committer(action="deploy", authority_scope="release")
        proj = self._mock_projection({"approve": 3}, "deploy-v2")
        decision = strategy.decide_commitment(proj)
        assert decision.action == "deploy"
        assert decision.authority_scope == "release"
        assert "deploy-v2" in decision.reason

    def test_default_action_and_scope(self):
        strategy = majority_committer()
        proj = self._mock_projection({"approve": 1}, "opt-a")
        decision = strategy.decide_commitment(proj)
        assert decision.action == "commit"
        assert decision.authority_scope == "session"

    def test_outcome_positive_inferred(self):
        strategy = majority_committer(action="proposal.accepted")
        proj = self._mock_projection({"approve": 1}, "opt-a")
        decision = strategy.decide_commitment(proj)
        assert decision.outcome_positive is True

    def test_outcome_positive_negative_action(self):
        strategy = majority_committer(action="proposal.rejected")
        proj = self._mock_projection({"approve": 1}, "opt-a")
        decision = strategy.decide_commitment(proj)
        assert decision.outcome_positive is False


class TestEvaluationValidation:
    """Test that evaluation_handler validates recommendation and confidence."""

    def test_invalid_recommendation_raises(self):
        strategy = function_evaluator(lambda p, c: EvaluationResult("INVALID", 0.5, "bad rec"))
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        try:
            handler(_make_message(), ctx)
            raise AssertionError("Should have raised")
        except MacpSessionError as exc:
            assert "invalid recommendation" in str(exc)
            assert isinstance(exc, MacpSdkError)

    def test_confidence_above_one_raises(self):
        strategy = function_evaluator(lambda p, c: EvaluationResult("APPROVE", 1.5, "too high"))
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        try:
            handler(_make_message(), ctx)
            raise AssertionError("Should have raised")
        except MacpSessionError as exc:
            assert "confidence" in str(exc)
            assert isinstance(exc, MacpSdkError)

    def test_confidence_below_zero_raises(self):
        strategy = function_evaluator(lambda p, c: EvaluationResult("APPROVE", -0.1, "too low"))
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        try:
            handler(_make_message(), ctx)
            raise AssertionError("Should have raised")
        except MacpSessionError as exc:
            assert "confidence" in str(exc)
            assert isinstance(exc, MacpSdkError)

    def test_confidence_nan_raises(self):
        # Regression gate for issue #121 item 1 ordering: validate_confidence
        # must reject NaN here exactly as it did before the de-duplication
        # (Phase 1 of plans/sdk-parity-sync-121.md landed the non-finite
        # guard this depends on).
        strategy = function_evaluator(
            lambda p, c: EvaluationResult("APPROVE", float("nan"), "not a number")
        )
        handler = evaluation_handler(strategy)
        ctx = _make_context()
        try:
            handler(_make_message(), ctx)
            raise AssertionError("Should have raised")
        except MacpSessionError as exc:
            assert "confidence" in str(exc)
            assert isinstance(exc, MacpSdkError)


class TestMajorityStrategiesUnderFirstWins:
    """Behavioural regression for issue #43 Phase 4 (D3): ``majority_voter``
    and ``majority_committer`` need no code change (they consume
    ``vote_totals()`` / ``majority_winner()`` through a duck-typed ``Any``),
    but first-wins changes the *values* those methods return, so this
    exercises a real ``DecisionProjection`` -- not a ``MagicMock`` -- to
    prove the strategies see the first-wins tally end to end.

    | Test type   | Requires                        | How                                   |
    |-------------|-----------------------------------|----------------------------------------|
    | Cardinality | two **distinct** non-empty ids   | two ``make_envelope(...)`` calls --    |
    |             |                                   | the default uuid4 already does this    |

    Concrete stake: Alice votes REJECT then (duplicate) APPROVE on ``p1``.
    Under first-wins her REJECT stands, so there is zero APPROVE vote and
    both strategies correctly decline to act. Under the old last-wins
    behaviour her APPROVE would have overwritten the REJECT, giving a
    (wrong) unanimous majority and flipping both decisions.
    """

    def _proj_with_discarded_vote_change(self) -> DecisionProjection:
        p = DecisionProjection()
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="reject"),
                sender="alice",
            )
        )
        # Duplicate: alice tries to change her vote. Distinct message_id
        # (fresh make_envelope call) -- discarded and recorded, not applied.
        p.apply_envelope(
            make_envelope(
                MODE_DECISION,
                "Vote",
                decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
                sender="alice",
            )
        )
        return p

    def test_majority_voter_sees_first_wins_tally(self):
        p = self._proj_with_discarded_vote_change()
        assert p.vote_totals() == {"p1": 0}
        assert p.majority_winner() is None
        assert len(p.anomalies) == 1

        strategy = majority_voter()
        assert strategy.should_vote(p) is False
        decision = strategy.decide_vote(p)
        assert decision.vote == "ABSTAIN"

    def test_majority_committer_sees_first_wins_tally(self):
        p = self._proj_with_discarded_vote_change()

        strategy = majority_committer(quorum_size=1)
        assert strategy.should_commit(p) is False


class TestDeprecatedAliases:
    """Issue #103 / multiagentcoordinationprotocol#135: ``VoteDecision``/
    ``CommitmentDecision`` are kept as deprecated aliases for ``VoteResult``/
    ``CommitmentResult`` for one minor version. Old names are resolved
    *inside* ``catch_warnings`` -- never at module level, which would fail
    test *collection* under this repo's ``filterwarnings = ["error", ...]``
    (pyproject.toml) before any test body ran.

    Uses real ``from ... import OldName`` statements (not attribute access)
    since that is what the plan's acceptance criteria describe and what
    real callers do. The submodule path (``macp_sdk.agent.strategies``, a
    plain file, no ``__path__``) and the package path (``macp_sdk.agent``,
    has ``__path__``) genuinely differ in warning count -- confirmed
    empirically, not assumed -- because CPython's import machinery probes a
    *package*'s fromlist name with an internal ``hasattr`` call before the
    statement's own getattr (see the comment beside ``agent/__init__.py``'s
    ``__getattr__``); a plain module has no such probe. Both still return an
    object ``is`` its ``*Result`` counterpart either way.
    """

    def test_vote_decision_alias_from_strategies_module(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.agent.strategies import VoteDecision
        assert [w.category for w in caught] == [DeprecationWarning]
        assert "VoteResult" in str(caught[0].message)
        module = importlib.import_module("macp_sdk.agent.strategies")
        assert VoteDecision is module.VoteResult

    def test_commitment_decision_alias_from_strategies_module(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.agent.strategies import CommitmentDecision
        assert [w.category for w in caught] == [DeprecationWarning]
        assert "CommitmentResult" in str(caught[0].message)
        module = importlib.import_module("macp_sdk.agent.strategies")
        assert CommitmentDecision is module.CommitmentResult

    def test_vote_decision_alias_from_agent_package(self):
        """The package path triggers CPython's fromlist hasattr-probe twice
        (see agent/__init__.py's __getattr__ comment) -- this asserts the
        real, verified count (2) rather than the naively-expected one, so a
        future change to the import machinery's behavior surfaces as a
        failing test instead of a silently-wrong docstring."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.agent import VoteDecision
        assert [w.category for w in caught] == [DeprecationWarning, DeprecationWarning]
        assert all("VoteResult" in str(w.message) for w in caught)
        module = importlib.import_module("macp_sdk.agent")
        assert VoteDecision is module.VoteResult

    def test_commitment_decision_alias_from_agent_package(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.agent import CommitmentDecision
        assert [w.category for w in caught] == [DeprecationWarning, DeprecationWarning]
        assert all("CommitmentResult" in str(w.message) for w in caught)
        module = importlib.import_module("macp_sdk.agent")
        assert CommitmentDecision is module.CommitmentResult

    def test_repeated_plain_attribute_access_warns_each_time(self):
        """PEP 562 results aren't cached in the module's __dict__ -- unlike
        the `from ... import` form (bound once as a local/global), plain
        attribute-style access (module.OldName, no `from` import) re-warns
        on every access. Documented in strategies.py's alias comment;
        proven here rather than only asserted."""
        module = importlib.import_module("macp_sdk.agent.strategies")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = module.VoteDecision
            _ = module.VoteDecision
        assert [w.category for w in caught] == [DeprecationWarning, DeprecationWarning]

    def test_unrecognized_name_still_raises_attribute_error(self):
        """Coverage for __getattr__'s fallthrough branch (not just the
        matched-name branch) -- required to hold the 85% branch floor."""
        module = importlib.import_module("macp_sdk.agent.strategies")
        try:
            _ = module.TotallyBogusName
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

        pkg = importlib.import_module("macp_sdk.agent")
        try:
            _ = pkg.TotallyBogusName
            raise AssertionError("Should have raised")
        except AttributeError:
            pass
