from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..envelope import infer_outcome_positive
from ..validation import validate_confidence, validate_recommendation
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
        recommendation = validate_recommendation(result.recommendation)
        validate_confidence(result.confidence)
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
class VoteResult:
    """Result of a voting decision."""

    vote: str
    reason: str


class VotingStrategy(Protocol):
    """Protocol for deciding how to vote."""

    def should_vote(self, projection: Any) -> bool: ...

    def decide_vote(self, projection: Any) -> VoteResult: ...


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
    decide_fn: Callable[[Any], VoteResult],
) -> VotingStrategy:
    """Wrap plain functions as a VotingStrategy."""

    class _FnVoter:
        __slots__ = ("_decide_fn", "_should_fn")

        def __init__(
            self,
            should_fn: Callable[[Any], bool],
            decide_fn: Callable[[Any], VoteResult],
        ) -> None:
            self._should_fn = should_fn
            self._decide_fn = decide_fn

        def should_vote(self, projection: Any) -> bool:
            return self._should_fn(projection)

        def decide_vote(self, projection: Any) -> VoteResult:
            return self._decide_fn(projection)

    return _FnVoter(should_vote_fn, decide_fn)


# ── Commitment ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommitmentResult:
    """Result of a commitment decision."""

    action: str
    authority_scope: str
    reason: str
    outcome_positive: bool | None = None


class CommitmentStrategy(Protocol):
    """Protocol for deciding whether and how to commit."""

    def should_commit(self, projection: Any) -> bool: ...

    def decide_commitment(self, projection: Any) -> CommitmentResult: ...


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
    decide_fn: Callable[[Any], CommitmentResult],
) -> CommitmentStrategy:
    """Wrap plain functions as a CommitmentStrategy."""

    class _FnCommitter:
        __slots__ = ("_decide_fn", "_should_fn")

        def __init__(
            self,
            should_fn: Callable[[Any], bool],
            decide_fn: Callable[[Any], CommitmentResult],
        ) -> None:
            self._should_fn = should_fn
            self._decide_fn = decide_fn

        def should_commit(self, projection: Any) -> bool:
            return self._should_fn(projection)

        def decide_commitment(self, projection: Any) -> CommitmentResult:
            return self._decide_fn(projection)

    return _FnCommitter(should_commit_fn, decide_fn)


# ── Built-in strategy factories ─────────────────────────────────────


def majority_voter(
    *,
    positive_threshold: float = 0.5,
) -> VotingStrategy:
    """Built-in voting strategy that votes ``APPROVE`` once the fraction of
    qualifying (non-``REVIEW``) evaluations recommending ``APPROVE``, for the
    most recently evaluated proposal, meets ``positive_threshold`` --
    otherwise ``ABSTAIN``. ``should_vote`` applies the same qualifying/latest-
    proposal rule: it returns ``True`` only when there is at least one
    qualifying evaluation for ``decide_vote`` to actually decide from.

    Both ``should_vote`` and ``decide_vote`` read only from
    ``projection.evaluations``, never from votes already cast (issue #93 item
    4 / #97 follow-up): a decision derived from votes cannot bootstrap a
    session where every participant runs ``majority_voter``, since
    ``vote_totals()`` starts empty and stays empty until *someone* votes
    first. Deriving from evaluations instead lets the first vote be cast as
    soon as there is evaluation data to decide from.

    Args:
        positive_threshold: Fraction of qualifying evaluations that must
            recommend ``APPROVE`` for ``decide_vote`` to return ``APPROVE``
            (default ``0.5``).
    """

    class _MajorityVoter:
        __slots__ = ("_threshold",)

        def __init__(self, threshold: float) -> None:
            self._threshold = threshold

        def should_vote(self, projection: Any) -> bool:
            if projection is None:
                return False
            evaluations = list(getattr(projection, "evaluations", None) or [])
            if not evaluations:
                return False
            # Agree with decide_vote(): it votes on the most recently evaluated
            # proposal and counts only qualifying (non-REVIEW) evaluations for
            # that proposal, so should_vote() must ask the same question.
            # RFC-MACP-0007 §4 (rfcs/RFC-MACP-0007-decision-mode.md:73): REVIEW
            # evaluations "do not block or approve a proposal; they serve as
            # informational analysis records only" -- a set of only REVIEWs has
            # nothing decisive to vote on.
            #
            # Cross-SDK note: macp-sdk-typescript's majorityVoter
            # (src/agent/strategies.ts:90-98) applies the same REVIEW filter but
            # does NOT scope to a proposal -- its decideVote counts every
            # decisive evaluation in the session. Python scopes both methods to
            # the latest proposal, which is the stricter and more correct
            # behaviour for a multi-proposal session (the vote is cast for one
            # proposal_id). The difference is deliberate; see plan issue #121.
            proposal_id = evaluations[-1].proposal_id
            return any(
                e.proposal_id == proposal_id and e.recommendation.upper() != "REVIEW"
                for e in evaluations
            )

        def decide_vote(self, projection: Any) -> VoteResult:
            evaluations = list(getattr(projection, "evaluations", None) or [])
            if not evaluations:
                return VoteResult(vote="ABSTAIN", reason="no evaluations to decide from")
            # The most recently evaluated proposal is the one being voted on.
            proposal_id = evaluations[-1].proposal_id
            qualifying = [
                e
                for e in evaluations
                if e.proposal_id == proposal_id and e.recommendation.upper() != "REVIEW"
            ]
            if not qualifying:
                return VoteResult(
                    vote="ABSTAIN",
                    reason=f"no qualifying evaluations for {proposal_id!r}",
                )
            approvals = sum(1 for e in qualifying if e.recommendation.upper() == "APPROVE")
            ratio = approvals / len(qualifying)
            if ratio >= self._threshold:
                return VoteResult(
                    vote="APPROVE",
                    reason=(
                        f"{approvals}/{len(qualifying)} evaluations approve "
                        f"{proposal_id!r} (>= {self._threshold:.0%})"
                    ),
                )
            return VoteResult(
                vote="ABSTAIN",
                reason=(
                    f"{approvals}/{len(qualifying)} evaluations approve "
                    f"{proposal_id!r} (< {self._threshold:.0%})"
                ),
            )

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

        def decide_commitment(self, projection: Any) -> CommitmentResult:
            winner = projection.majority_winner()
            return CommitmentResult(
                action=self._action,
                authority_scope=self._scope,
                reason=f"majority winner: {winner}",
                outcome_positive=infer_outcome_positive(self._action),
            )

    return _MajorityCommitter(quorum_size, action, authority_scope)


# ── Deprecated aliases (issue #103 / multiagentcoordinationprotocol#135) ─────
#
# ``VoteDecision``/``CommitmentDecision`` are the pre-rename names, kept as
# module-level lazy aliases (PEP 562) for one minor version, removed at this
# SDK's next major. A plain assignment (``VoteDecision = VoteResult``) would
# be silent; a wrapper subclass would fight ``frozen=True, slots=True``. This
# module has no ``__path__`` (a plain file, not a package), so CPython's
# ``from ... import VoteDecision`` resolves via a single ``getattr`` call --
# the warning fires exactly once per such import, not per call. (Contrast
# ``agent/__init__.py``'s own alias dict: a *package* import goes through an
# extra internal ``hasattr`` probe first, firing this pattern twice -- see
# the comment there.) Plain attribute access (``strategies.VoteDecision``,
# no ``from`` import) is not cached in ``globals()`` and re-warns on every
# such access -- the returned object ``is`` its ``*Result`` counterpart
# either way.
_DEPRECATED_ALIASES = {
    "VoteDecision": "VoteResult",
    "CommitmentDecision": "CommitmentResult",
}


def __getattr__(name: str) -> Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
