from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..envelope import infer_outcome_positive
from .types import HandlerContext, IncomingMessage, MessageHandler, SessionInfo

# ── Evaluation ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Result of evaluating a proposal."""

    recommendation: str
    confidence: float
    reason: str


class EvaluationStrategy(Protocol):
    """Protocol for evaluating proposals."""

    def evaluate(self, proposal: dict[str, Any], context: SessionInfo) -> EvaluationResult: ...


_VALID_RECOMMENDATIONS = frozenset({"APPROVE", "REVIEW", "BLOCK", "REJECT"})


def evaluation_handler(strategy: EvaluationStrategy) -> MessageHandler:
    """Create a MessageHandler that evaluates proposals using the given strategy.

    When a ``Proposal`` message arrives, the strategy's ``evaluate()``
    method is called and the result is logged via the handler context.
    The caller can inspect the result via the context's projection.
    """

    def handler(message: IncomingMessage, ctx: HandlerContext) -> None:
        if message.message_type != "Proposal":
            return
        result = strategy.evaluate(message.payload, ctx.session)
        recommendation = result.recommendation.upper()
        if recommendation not in _VALID_RECOMMENDATIONS:
            raise ValueError(
                f"invalid recommendation {result.recommendation!r}: "
                "must be one of APPROVE, REVIEW, BLOCK, REJECT"
            )
        if not (0.0 <= result.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0], got {result.confidence}")
        ctx.log(
            "evaluation: recommendation=%s confidence=%.2f reason=%s",
            recommendation,
            result.confidence,
            result.reason,
        )
        proposal_id = message.proposal_id or message.payload.get("proposal_id", "")
        ctx.actions.evaluate(
            proposal_id,
            recommendation,
            confidence=result.confidence,
            reason=result.reason,
        )

    return handler


def function_evaluator(
    fn: Callable[[dict[str, Any], SessionInfo], EvaluationResult],
) -> EvaluationStrategy:
    """Wrap a plain function as an EvaluationStrategy."""

    class _FnEvaluator:
        __slots__ = ("_fn",)

        def __init__(self, fn: Callable[[dict[str, Any], SessionInfo], EvaluationResult]) -> None:
            self._fn = fn

        def evaluate(self, proposal: dict[str, Any], context: SessionInfo) -> EvaluationResult:
            return self._fn(proposal, context)

    return _FnEvaluator(fn)


# ── Voting ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VoteDecision:
    """Result of a voting decision."""

    vote: str
    reason: str


class VotingStrategy(Protocol):
    """Protocol for deciding how to vote."""

    def should_vote(self, projection: Any) -> bool: ...

    def decide_vote(self, projection: Any) -> VoteDecision: ...


def voting_handler(strategy: VotingStrategy) -> MessageHandler:
    """Create a MessageHandler that makes voting decisions using the given strategy.

    Fires only on an ``Evaluation`` message (parity with typescript-sdk's
    ``votingHandler``, ``strategies.ts:53``) -- if it should vote (via
    ``should_vote()``), ``decide_vote()`` is called and the decision logged.
    """

    def handler(message: IncomingMessage, ctx: HandlerContext) -> None:
        if message.message_type != "Evaluation":
            return
        if not strategy.should_vote(ctx.projection):
            return
        decision = strategy.decide_vote(ctx.projection)
        ctx.log(
            "vote: vote=%s reason=%s",
            decision.vote,
            decision.reason,
        )
        proposal_id = message.proposal_id or message.payload.get("proposal_id", "")
        ctx.actions.vote(
            proposal_id,
            decision.vote,
            reason=decision.reason,
        )

    return handler


def function_voter(
    should_vote_fn: Callable[[Any], bool],
    decide_fn: Callable[[Any], VoteDecision],
) -> VotingStrategy:
    """Wrap plain functions as a VotingStrategy."""

    class _FnVoter:
        __slots__ = ("_decide_fn", "_should_fn")

        def __init__(
            self,
            should_fn: Callable[[Any], bool],
            decide_fn: Callable[[Any], VoteDecision],
        ) -> None:
            self._should_fn = should_fn
            self._decide_fn = decide_fn

        def should_vote(self, projection: Any) -> bool:
            return self._should_fn(projection)

        def decide_vote(self, projection: Any) -> VoteDecision:
            return self._decide_fn(projection)

    return _FnVoter(should_vote_fn, decide_fn)


# ── Commitment ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommitmentDecision:
    """Result of a commitment decision."""

    action: str
    authority_scope: str
    reason: str
    outcome_positive: bool | None = None


class CommitmentStrategy(Protocol):
    """Protocol for deciding whether and how to commit."""

    def should_commit(self, projection: Any) -> bool: ...

    def decide_commitment(self, projection: Any) -> CommitmentDecision: ...


def commitment_handler(strategy: CommitmentStrategy) -> MessageHandler:
    """Create a MessageHandler that makes commitment decisions using the given strategy.

    Fires only on a ``Vote`` message (parity with typescript-sdk's
    ``commitmentHandler``, ``strategies.ts:116``) -- if a commitment should
    be made (via ``should_commit()``), ``decide_commitment()`` is called and
    the decision logged. A decision that leaves ``outcome_positive`` unset
    gets it inferred from ``action`` (mirroring ``majority_committer``'s own
    ``infer_outcome_positive`` call and ``build_commitment_payload``'s
    lower-level default), so a caller-written strategy that forgets to set
    it doesn't silently commit a wrong-signed ``outcome_positive=True``.
    """

    def handler(message: IncomingMessage, ctx: HandlerContext) -> None:
        if message.message_type != "Vote":
            return
        if not strategy.should_commit(ctx.projection):
            return
        decision = strategy.decide_commitment(ctx.projection)
        outcome_positive = (
            decision.outcome_positive
            if decision.outcome_positive is not None
            else infer_outcome_positive(decision.action)
        )
        ctx.log(
            "commitment: action=%s scope=%s reason=%s",
            decision.action,
            decision.authority_scope,
            decision.reason,
        )
        ctx.actions.commit(
            decision.action,
            decision.authority_scope,
            reason=decision.reason,
            outcome_positive=outcome_positive,
        )

    return handler


def function_committer(
    should_commit_fn: Callable[[Any], bool],
    decide_fn: Callable[[Any], CommitmentDecision],
) -> CommitmentStrategy:
    """Wrap plain functions as a CommitmentStrategy."""

    class _FnCommitter:
        __slots__ = ("_decide_fn", "_should_fn")

        def __init__(
            self,
            should_fn: Callable[[Any], bool],
            decide_fn: Callable[[Any], CommitmentDecision],
        ) -> None:
            self._should_fn = should_fn
            self._decide_fn = decide_fn

        def should_commit(self, projection: Any) -> bool:
            return self._should_fn(projection)

        def decide_commitment(self, projection: Any) -> CommitmentDecision:
            return self._decide_fn(projection)

    return _FnCommitter(should_commit_fn, decide_fn)


# ── Built-in strategy factories ─────────────────────────────────────


def majority_voter(
    *,
    positive_threshold: float = 0.5,
) -> VotingStrategy:
    """Built-in voting strategy that votes ``approve`` when the majority winner
    matches the first proposal option, based on the decision projection.

    Args:
        positive_threshold: Fraction of votes required to trigger voting
            (default ``0.5``).
    """

    class _MajorityVoter:
        __slots__ = ("_threshold",)

        def __init__(self, threshold: float) -> None:
            self._threshold = threshold

        def should_vote(self, projection: Any) -> bool:
            if projection is None:
                return False
            totals = projection.vote_totals()
            total_votes = sum(totals.values())
            return total_votes > 0 and any(
                count / total_votes >= self._threshold for count in totals.values()
            )

        def decide_vote(self, projection: Any) -> VoteDecision:
            winner = projection.majority_winner()
            if winner:
                return VoteDecision(vote="APPROVE", reason=f"majority winner: {winner}")
            return VoteDecision(vote="ABSTAIN", reason="no majority winner")

    return _MajorityVoter(positive_threshold)


def majority_committer(
    *,
    quorum_size: int = 1,
    action: str = "commit",
    authority_scope: str = "session",
) -> CommitmentStrategy:
    """Built-in commitment strategy that commits when a majority winner exists
    and the quorum has been met.

    Args:
        quorum_size: Minimum number of votes before commitment (default ``1``).
        action: The commitment action string (default ``"commit"``).
        authority_scope: The commitment authority scope (default ``"session"``).
    """

    class _MajorityCommitter:
        __slots__ = ("_action", "_quorum", "_scope")

        def __init__(self, quorum: int, commit_action: str, scope: str) -> None:
            self._quorum = quorum
            self._action = commit_action
            self._scope = scope

        def should_commit(self, projection: Any) -> bool:
            if projection is None:
                return False
            totals = projection.vote_totals()
            total_votes = sum(totals.values())
            if total_votes < self._quorum:
                return False
            return projection.majority_winner() is not None

        def decide_commitment(self, projection: Any) -> CommitmentDecision:
            winner = projection.majority_winner()
            return CommitmentDecision(
                action=self._action,
                authority_scope=self._scope,
                reason=f"majority winner: {winner}",
                outcome_positive=infer_outcome_positive(self._action),
            )

    return _MajorityCommitter(quorum_size, action, authority_scope)
