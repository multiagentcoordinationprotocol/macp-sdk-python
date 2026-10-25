"""``MacpConsensusNode``: a LangGraph node that settles a binding MACP decision.

The node's client is the session's initiator: it opens the session, puts the
proposal, and commits. It never votes. Each participant evaluates through an
SDK ``EvaluationStrategy`` and casts its ballot under its own auth:

- **Decision** (``macp.mode.decision.v1``): the runtime only accepts a
  ``Proposal`` from a declared participant, so the initiator is listed first.
  It sends ``Proposal`` ``p1``. Every participant sends an ``Evaluation``,
  then they vote in list order.
- **Quorum** (``macp.mode.quorum.v1``): the initiator is not a participant.
  It sends ``ApprovalRequest`` ``r1``, and each participant answers with
  ``Approve`` / ``Reject`` / ``Abstain``.

The outcome is approved once ``threshold`` participants approve. It is
rejected as soon as the approvals plus the participants yet to vote fall
short of the threshold. The initiator commits that outcome at once, and the
remaining participants never vote. The Commitment, its hash and the tally
are written to graph state under ``output_key``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableLambda
from macp_sdk import (
    DEFAULT_POLICY_VERSION,
    MODE_DECISION,
    MODE_QUORUM,
    AuthConfig,
    DecisionReceipt,
    DecisionSession,
    MacpClient,
    QuorumSession,
    ReceiptSigner,
    build_receipt,
    commitment_from_projection,
    commitment_hash,
)
from macp_sdk.agent import EvaluationResult, EvaluationStrategy, SessionInfo, VotingStrategy
from macp_sdk.commitment_hash import canonical_projection
from macp_sdk.validation import validate_confidence, validate_vote

Mode = Literal["decision", "quorum"]
Outcome = Literal["approve", "reject"]

DECISION_PROPOSAL_ID = "p1"
QUORUM_REQUEST_ID = "r1"
DEFAULT_ACTION = "macp.consensus"
AUTHORITY_SCOPE = "macp.consensus"
DEFAULT_TTL_MS = 60_000

_MODES: Mapping[str, str] = {"decision": MODE_DECISION, "quorum": MODE_QUORUM}

#: A wire-valid Decision ``Evaluation`` recommendation, as the runtime accepts it.
_RECOMMENDATIONS = frozenset({"APPROVE", "REVIEW", "BLOCK", "REJECT"})
#: Recommendation → Decision vote. Quorum uses the same map, title-cased.
_BALLOTS: Mapping[str, str] = {
    "APPROVE": "APPROVE",
    "REVIEW": "ABSTAIN",
    "REJECT": "REJECT",
    "BLOCK": "REJECT",
}

EvaluatorFn = Callable[[dict[str, Any], SessionInfo], "EvaluationResult | Mapping[str, Any]"]
Evaluator = EvaluationStrategy | EvaluatorFn


@dataclass(frozen=True)
class ConsensusParticipant:
    """One voter: its agent id, how it evaluates, and how it authenticates.

    ``auth`` defaults to dev-agent auth for ``id``; pass a bearer
    ``AuthConfig`` in production. ``voter`` (Decision only) overrides the
    default vote, which maps the participant's own recommendation.
    """

    id: str
    evaluator: Evaluator
    auth: AuthConfig | None = None
    voter: VotingStrategy | None = None


class ConsensusResult(TypedDict):
    """What the node writes under ``output_key``.

    ``commitment`` is the RFC-MACP-0013 §3 projection of the committed
    ``CommitmentPayload`` (a plain dict, so checkpointers can store it).
    """

    outcome: Outcome
    session_id: str
    commitment_hash: str
    mode: str
    commitment: dict[str, Any]
    approvals: int
    threshold: int


def normalize_recommendation(value: object) -> str:
    """Uppercase ``value`` and check it is APPROVE / REVIEW / BLOCK / REJECT.

    ``ABSTAIN`` becomes ``REVIEW``, so every result is a wire-valid Decision
    ``Evaluation``. Anything else raises ``ValueError``.
    """
    if not isinstance(value, str):
        raise ValueError(f"recommendation must be a string, got {type(value).__name__}")
    normalized = value.strip().upper()
    if normalized == "ABSTAIN":
        return "REVIEW"
    if normalized not in _RECOMMENDATIONS:
        raise ValueError(
            f"invalid recommendation {value!r}: must be one of APPROVE, REVIEW, BLOCK, REJECT"
        )
    return normalized


def _as_result(value: EvaluationResult | Mapping[str, Any]) -> EvaluationResult:
    if isinstance(value, Mapping):
        value = EvaluationResult(
            recommendation=value.get("recommendation"),  # type: ignore[arg-type]
            confidence=value.get("confidence"),  # type: ignore[arg-type]
            reason=str(value.get("reason", "")),
        )
    if not isinstance(value, EvaluationResult):
        raise TypeError(
            f"evaluator returned {type(value).__name__}; expected EvaluationResult or a mapping"
        )
    confidence = value.confidence
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise ValueError(f"confidence must be a number, got {confidence!r}")
    validate_confidence(float(confidence))
    return EvaluationResult(
        recommendation=normalize_recommendation(value.recommendation),
        confidence=float(confidence),
        reason=str(value.reason),
    )


def _evaluate(
    evaluator: Evaluator, proposal: dict[str, Any], info: SessionInfo
) -> EvaluationResult:
    evaluate = getattr(evaluator, "evaluate", None)
    raw = evaluate(proposal, info) if callable(evaluate) else evaluator(proposal, info)  # type: ignore[operator]
    return _as_result(raw)


def _default_proposal(state: Mapping[str, Any]) -> Mapping[str, Any] | str:
    if "proposal" not in state:
        raise KeyError("MacpConsensusNode: state has no 'proposal' (or pass proposal=)")
    return state["proposal"]  # type: ignore[no-any-return]


def _read_proposal(value: Mapping[str, Any] | str) -> tuple[str, str, Any]:
    option: object
    rationale: object
    if isinstance(value, str):
        option, rationale, details = value, "", None
    elif isinstance(value, Mapping):
        option = value.get("option")
        rationale = value.get("rationale") or ""
        details = value.get("details")
    else:
        raise TypeError(f"proposal must be a string or a mapping, got {type(value).__name__}")
    if not isinstance(option, str) or not option:
        raise ValueError("proposal option must be a non-empty string")
    if not isinstance(rationale, str):
        raise ValueError("proposal rationale must be a string")
    return option, rationale, details


def _initiator_of(client: MacpClient, initiator: str | None) -> str:
    agent = initiator or (client.auth.sender_hint if client.auth else None)
    if not agent:
        raise ValueError(
            "MacpConsensusNode: cannot tell the initiator's agent id from the client's auth; "
            "pass initiator="
        )
    return agent


class _Tally:
    def __init__(self, voters: int, threshold: int) -> None:
        self.voters = voters
        self.threshold = threshold
        self.cast = 0
        self.approvals = 0

    def add(self, approved: bool) -> None:
        self.cast += 1
        self.approvals += approved

    def outcome(self) -> Outcome | None:
        if self.approvals >= self.threshold:
            return "approve"
        if self.approvals + (self.voters - self.cast) < self.threshold:
            return "reject"
        return None


class MacpConsensusNode(RunnableLambda[Any, dict[str, ConsensusResult]]):
    """A LangGraph node that settles one MACP Decision or Quorum session.

    Add it like any node: ``graph.add_node("consensus", MacpConsensusNode(...))``.
    It works under both ``invoke`` and ``ainvoke``. The async path runs the
    blocking gRPC calls in a worker thread.

    Args:
        client: A ``MacpClient`` authenticated as the initiator.
        participants: The voters, in the order they evaluate and vote.
        mode: ``"decision"`` (default) or ``"quorum"``.
        policy: A registered ``policy_version`` (default ``policy.default``).
        threshold: Approvals required (default a strict majority, ``n // 2 + 1``).
        proposal: ``state -> str | {option, rationale?, details?}``. The
            default reads ``state["proposal"]``.
        output_key: The state key written (default ``"macp"``). Declare it in
            your state schema, or LangGraph drops it.
        ttl_ms: Session TTL (default 60 s).
        action: The commitment action stem, committed as ``<action>.approved``
            or ``<action>.rejected``.
        initiator: The initiator's agent id, when the client's auth does not
            carry it (a production bearer token).
    """

    def __init__(
        self,
        client: MacpClient,
        participants: Sequence[ConsensusParticipant],
        mode: Mode = "decision",
        policy: str | None = None,
        *,
        threshold: int | None = None,
        proposal: Callable[[Mapping[str, Any]], Mapping[str, Any] | str] | None = None,
        output_key: str = "macp",
        ttl_ms: int = DEFAULT_TTL_MS,
        action: str = DEFAULT_ACTION,
        initiator: str | None = None,
        name: str = "macp_consensus",
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be 'decision' or 'quorum', got {mode!r}")
        voters = list(participants)
        if not voters:
            raise ValueError("MacpConsensusNode needs at least one participant")
        ids = [p.id for p in voters]
        if len(set(ids)) != len(ids):
            raise ValueError(f"participant ids must be unique: {ids}")
        self._initiator = _initiator_of(client, initiator)
        if self._initiator in ids:
            raise ValueError(
                f"the initiator {self._initiator!r} commits and cannot also be a participant"
            )
        n = len(voters)
        self._threshold = n // 2 + 1 if threshold is None else threshold
        if isinstance(self._threshold, bool) or not 1 <= self._threshold <= n:
            raise ValueError(f"threshold must be between 1 and {n}, got {threshold!r}")
        if ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")
        self._client = client
        self._participants = voters
        self._mode = mode
        self._policy = policy or DEFAULT_POLICY_VERSION
        self._proposal = proposal or _default_proposal
        self._output_key = output_key
        self._ttl_ms = ttl_ms
        self._action = action
        super().__init__(self._run, afunc=self._arun, name=name)

    async def _arun(self, state: Mapping[str, Any]) -> dict[str, ConsensusResult]:
        return await asyncio.to_thread(self._run, state)

    def _run(self, state: Mapping[str, Any]) -> dict[str, ConsensusResult]:
        option, rationale, details = _read_proposal(self._proposal(state))
        ids = [p.id for p in self._participants]
        session: DecisionSession | QuorumSession
        if self._mode == "decision":
            session = DecisionSession(self._client, policy_version=self._policy)
            members = [self._initiator, *ids]
            proposal_id = DECISION_PROPOSAL_ID
        else:
            session = QuorumSession(self._client, policy_version=self._policy)
            members = ids
            proposal_id = QUORUM_REQUEST_ID
        session.start(intent=option, participants=members, ttl_ms=self._ttl_ms)
        try:
            tally = self._settle(session, proposal_id, option, rationale, details, members)
            outcome = tally.outcome() or "reject"
            session.commit(
                action=f"{self._action}.{'approved' if outcome == 'approve' else 'rejected'}",
                authority_scope=AUTHORITY_SCOPE,
                reason=f"{tally.approvals}/{tally.voters} approvals (threshold {tally.threshold})",
                outcome_positive=outcome == "approve",
            )
        except BaseException:
            with contextlib.suppress(Exception):
                session.cancel(reason="MacpConsensusNode failed")
            raise
        payload = session.projection.commitment
        if payload is None:  # pragma: no cover - an acked commit is always projected
            raise RuntimeError("the runtime acked the Commitment but it was not projected")
        result: ConsensusResult = {
            "outcome": outcome,
            "session_id": session.session_id,
            "commitment_hash": commitment_hash(payload),
            "mode": _MODES[self._mode],
            "commitment": json.loads(canonical_projection(payload)),
            "approvals": tally.approvals,
            "threshold": tally.threshold,
        }
        return {self._output_key: result}

    def _settle(
        self,
        session: DecisionSession | QuorumSession,
        proposal_id: str,
        option: str,
        rationale: str,
        details: Any,
        members: list[str],
    ) -> _Tally:
        wire_details = (
            b"" if details is None else json.dumps(details, separators=(",", ":")).encode()
        )
        if isinstance(session, DecisionSession):
            session.propose(proposal_id, option, rationale=rationale, supporting_data=wire_details)
        else:
            session.request_approval(
                proposal_id,
                option,
                summary=rationale,
                details=wire_details,
                required_approvals=self._threshold,
            )
        mode = _MODES[self._mode]
        proposal = {
            "proposal_id": proposal_id,
            "option": option,
            "rationale": rationale,
            "details": details,
            "mode": mode,
        }
        info = SessionInfo(
            session_id=session.session_id,
            mode=mode,
            participants=list(members),
            mode_version=session.mode_version,
            configuration_version=session.configuration_version,
            policy_version=session.policy_version,
        )
        tally = _Tally(len(self._participants), self._threshold)
        if isinstance(session, DecisionSession):
            results = []
            for p in self._participants:
                result = _evaluate(p.evaluator, proposal, info)
                session.evaluate(
                    proposal_id,
                    result.recommendation,
                    confidence=result.confidence,
                    reason=result.reason,
                    sender=p.id,
                    auth=self._auth(p),
                )
                results.append(result)
            for p, result in zip(self._participants, results, strict=True):
                vote, reason = self._vote(session, p, result)
                if vote is not None:
                    session.vote(proposal_id, vote, reason=reason, sender=p.id, auth=self._auth(p))
                    tally.add(vote == "APPROVE")
                else:
                    tally.add(False)  # declined to vote: no approval from it
                if tally.outcome() is not None:
                    break
        else:
            for p in self._participants:
                result = _evaluate(p.evaluator, proposal, info)
                ballot = _BALLOTS[result.recommendation]
                send = {"APPROVE": session.approve, "REJECT": session.reject}.get(
                    ballot, session.abstain
                )
                send(proposal_id, reason=result.reason, sender=p.id, auth=self._auth(p))
                tally.add(ballot == "APPROVE")
                if tally.outcome() is not None:
                    break
        return tally

    @staticmethod
    def _vote(
        session: DecisionSession, p: ConsensusParticipant, result: EvaluationResult
    ) -> tuple[str | None, str]:
        if p.voter is None:
            return _BALLOTS[result.recommendation], result.reason
        projection = session.decision_projection
        if not p.voter.should_vote(projection):
            return None, ""
        decision = p.voter.decide_vote(projection)
        return validate_vote(decision.vote), decision.reason

    @staticmethod
    def _auth(p: ConsensusParticipant) -> AuthConfig:
        return p.auth or AuthConfig.for_dev_agent(p.id)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def consensus_receipt(
    result: ConsensusResult,
    *,
    issuer: str,
    issued_at: str | None = None,
    signer: ReceiptSigner | None = None,
) -> DecisionReceipt:
    """A ``macp-receipt/1`` decision receipt for the node's result, in one call.

    ``issued_at`` defaults to now (``YYYY-MM-DDTHH:MM:SSZ``). Pass a
    ``ReceiptSigner`` to add an Ed25519 attestation. Without one, the
    receipt verifies as ``integrity_only``.
    """
    return build_receipt(
        commitment_from_projection(result["commitment"]),
        session_id=result["session_id"],
        mode=result["mode"],
        issuer=issuer,
        issued_at=issued_at or _now(),
        signer=signer,
    )
