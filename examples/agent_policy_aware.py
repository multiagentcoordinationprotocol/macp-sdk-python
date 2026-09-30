"""Example: agent participant that uses policy_version to make policy-aware decisions.

Demonstrates the Participant + Strategy pattern where the strategy
inspects the session's policy_version to adjust its behavior.

This example uses process_event() for self-contained demonstration.
In production, call participant.run() to enter the streaming event loop.
"""

from __future__ import annotations

from macp.modes.decision.v1 import decision_pb2
from macp.v1 import envelope_pb2

from macp_sdk import MacpClient
from macp_sdk.agent import (
    EvaluationResult,
    Participant,
    VoteResult,
    evaluation_handler,
    function_evaluator,
    function_voter,
    voting_handler,
)
from macp_sdk.agent.types import SessionInfo
from macp_sdk.constants import MODE_DECISION
from macp_sdk.envelope import new_message_id, now_unix_ms, serialize_message

# ── Policy-aware evaluation strategy ─────────────────────────────────


def evaluate_proposal(proposal: dict, context: SessionInfo) -> EvaluationResult:
    """Evaluate a proposal, adjusting confidence threshold based on policy."""
    # A strict policy requires higher confidence
    is_strict = context.policy_version and "strict" in context.policy_version

    base_confidence = 0.85
    if is_strict:
        # Under strict policy, we're more conservative
        base_confidence = 0.95

    option = proposal.get("option", "unknown")
    if option == "deploy-canary":
        return EvaluationResult(
            recommendation="APPROVE",
            confidence=base_confidence,
            reason=f"Canary deployment is low risk (policy={context.policy_version})",
        )
    return EvaluationResult(
        recommendation="REVIEW",
        confidence=base_confidence * 0.7,
        reason=f"Non-canary deployment needs review (policy={context.policy_version})",
    )


# ── Policy-aware voting strategy ─────────────────────────────────────


def should_vote(projection) -> bool:
    """Vote when at least one proposal has been seen."""
    return bool(projection and projection.proposals)


def decide_vote(projection) -> VoteResult:
    """Vote based on the proposals in the projection."""
    if projection.proposals:
        return VoteResult(vote="APPROVE", reason="proposal looks good")
    return VoteResult(vote="ABSTAIN", reason="no proposals to evaluate")


# ── Demo: simulate events for a policy-aware participant ─────────────


def _make_envelope(
    message_type: str,
    payload_message: object,
    session_id: str = "demo-session",
    sender: str = "coordinator",
) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=MODE_DECISION,
        message_type=message_type,
        message_id=new_message_id(),
        session_id=session_id,
        sender=sender,
        timestamp_unix_ms=now_unix_ms(),
        payload=serialize_message(payload_message),
    )


def main() -> None:
    from unittest.mock import MagicMock

    # Use a mock client for demo (no runtime needed)
    client = MagicMock(spec=MacpClient)

    # Create a policy-aware participant
    participant = Participant(
        participant_id="fraud-detector",
        session_id="demo-session",
        mode=MODE_DECISION,
        client=client,
        participants=["fraud-detector", "coordinator", "reviewer"],
        policy_version="policy.fraud.strict-review",
    )

    # Register policy-aware handlers
    eval_strategy = function_evaluator(evaluate_proposal)
    vote_strategy = function_voter(should_vote, decide_vote)

    participant.on("Proposal", evaluation_handler(eval_strategy))
    participant.on("Evaluation", voting_handler(vote_strategy))

    # Log terminal outcomes
    participant.on_terminal(lambda r: print(f"Session ended: {r.state}"))

    print(f"Participant: {participant.participant_id}")
    print(f"Policy: {participant.session.policy_version}")
    print()

    # Simulate a proposal event -- triggers evaluation_handler, which sends
    # this agent's own Evaluation (via the mocked client, not observed here).
    proposal_env = _make_envelope(
        "Proposal",
        decision_pb2.ProposalPayload(proposal_id="p1", option="deploy-canary"),
    )
    participant.process_event(proposal_env)

    # Check projection state
    proj = participant.projection
    if proj:
        print(f"Proposals seen: {list(proj.proposals.keys())}")

    # Simulate the Evaluation the runtime broadcasts back to every
    # participant once an Evaluation has been accepted -- this is the
    # message voting_handler is gated on, not Proposal.
    evaluation_env = _make_envelope(
        "Evaluation",
        decision_pb2.EvaluationPayload(
            proposal_id="p1",
            recommendation="APPROVE",
            confidence=0.95,
            reason="canary deployment is low risk",
        ),
    )
    participant.process_event(evaluation_env)

    # Prove the handlers actually ran, not just that nothing crashed --
    # this is exactly the failure mode the message-type gates must not
    # reintroduce (unit tests green, shipped example silently inert).
    sent_types = [call.args[0].message_type for call in client.send.call_args_list]
    print(f"Envelopes sent by this agent: {sent_types}")
    assert "Evaluation" in sent_types, "evaluation_handler did not fire"
    assert "Vote" in sent_types, "voting_handler did not fire"

    print("\nDemo complete. In production, call participant.run() instead.")


if __name__ == "__main__":
    main()
