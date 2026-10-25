from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from macp_sdk import (
    AuthConfig,
    MacpSessionError,
    ReceiptSigner,
    commitment_hash,
    verify_receipt,
)
from macp_sdk.agent import EvaluationResult, SessionInfo, VoteResult, function_voter

from claude_agent_sdk_macp import (
    ConsensusEngine,
    ConsensusParticipant,
    consensus_receipt,
    normalize_recommendation,
)
from claude_agent_sdk_macp.engine import _now

from .conftest import FakeClient


def fixed(recommendation: str, confidence: float = 0.9, reason: str = "ok") -> Any:
    return lambda proposal, info: {
        "recommendation": recommendation,
        "confidence": confidence,
        "reason": reason,
    }


def voters(*recommendations: str) -> list[ConsensusParticipant]:
    return [ConsensusParticipant(f"agent-{i}", fixed(r)) for i, r in enumerate(recommendations)]


# ── decision ─────────────────────────────────────────────────────────


def test_decision_approved(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("APPROVE", "REJECT", "APPROVE"))
    result = node.run("ship v2")
    assert client.types() == [
        ("SessionStart", "coordinator"),
        ("Proposal", "coordinator"),
        ("Evaluation", "agent-0"),
        ("Evaluation", "agent-1"),
        ("Evaluation", "agent-2"),
        ("Vote", "agent-0"),
        ("Vote", "agent-1"),
        ("Vote", "agent-2"),
        ("Commitment", "coordinator"),
    ]
    start = client.payload("SessionStart")
    assert list(start.participants) == ["coordinator", "agent-0", "agent-1", "agent-2"]
    assert start.intent == "ship v2"
    assert start.ttl_ms == 60_000
    assert start.policy_version == "policy.default"
    proposal = client.payload("Proposal")
    assert (proposal.proposal_id, proposal.option, proposal.rationale) == ("p1", "ship v2", "")
    assert proposal.supporting_data == b""
    # Each participant sends under its own dev-agent auth.
    assert [a for t, _, a, _ in client.sent if t == "Vote"] == ["agent-0", "agent-1", "agent-2"]
    assert [p.vote for t, _, _, p in client.sent if t == "Vote"] == ["APPROVE", "REJECT", "APPROVE"]
    commitment = client.payload("Commitment")
    assert commitment.action == "macp.consensus.approved"
    assert commitment.authority_scope == "macp.consensus"
    assert commitment.reason == "2/3 approvals (threshold 2)"
    assert commitment.outcome_positive is True
    assert result["outcome"] == "approve"
    assert result["mode"] == "macp.mode.decision.v1"
    assert (result["approvals"], result["threshold"]) == (2, 2)
    assert result["commitment_hash"] == commitment_hash(commitment)
    assert result["commitment"]["commitment_id"] == commitment.commitment_id
    json.dumps(result)  # plain data: checkpointers can store it


def test_decision_rejected_short_circuits(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("REJECT", "BLOCK", "APPROVE"))
    result = node.run("ship v2")
    votes = [s for t, s in client.types() if t == "Vote"]
    assert votes == ["agent-0", "agent-1"]  # approval out of reach after two
    assert result["outcome"] == "reject"
    commitment = client.payload("Commitment")
    assert commitment.action == "macp.consensus.rejected"
    assert commitment.outcome_positive is False
    assert commitment.reason == "0/3 approvals (threshold 2)"


def test_review_and_abstain_become_abstain_votes(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("REVIEW", "ABSTAIN", "APPROVE"), threshold=1)
    result = node.run("x")
    evaluations = [p.recommendation for t, _, _, p in client.sent if t == "Evaluation"]
    assert evaluations == ["REVIEW", "REVIEW", "APPROVE"]
    assert [p.vote for t, _, _, p in client.sent if t == "Vote"] == [
        "ABSTAIN",
        "ABSTAIN",
        "APPROVE",
    ]
    assert result["outcome"] == "approve"
    assert result["approvals"] == 1


def test_proposal_mapping_and_options(client: FakeClient) -> None:
    node = ConsensusEngine(
        client,
        voters("APPROVE"),
        policy="policy.custom",
        ttl_ms=5_000,
        action="deploy",
    )
    node.run({"option": "deploy prod", "rationale": "because", "details": {"b": 1, "a": [1, 2]}})
    proposal = client.payload("Proposal")
    assert proposal.rationale == "because"
    assert json.loads(proposal.supporting_data) == {"b": 1, "a": [1, 2]}
    assert client.payload("SessionStart").ttl_ms == 5_000
    assert client.payload("Commitment").action == "deploy.approved"
    assert client.payload("Commitment").policy_version == "policy.custom"


def test_evaluator_sees_proposal_and_session(client: FakeClient) -> None:
    seen: list[tuple[dict[str, Any], SessionInfo]] = []

    class Strategy:
        def evaluate(self, proposal: dict[str, Any], info: SessionInfo) -> EvaluationResult:
            seen.append((proposal, info))
            return EvaluationResult("approve", 1, "fine")

    node = ConsensusEngine(client, [ConsensusParticipant("solo", Strategy())])
    node.run({"option": "go", "details": [1]})
    proposal, info = seen[0]
    assert proposal == {
        "proposal_id": "p1",
        "option": "go",
        "rationale": "",
        "details": [1],
        "mode": "macp.mode.decision.v1",
    }
    assert info.mode == "macp.mode.decision.v1"
    assert info.participants == ["coordinator", "solo"]
    assert info.policy_version == "policy.default"
    assert client.payload("Evaluation").recommendation == "APPROVE"


def test_custom_voter(client: FakeClient) -> None:
    always_approve = function_voter(lambda proj: True, lambda proj: VoteResult("approve", "yes"))
    declines = function_voter(lambda proj: False, lambda proj: VoteResult("APPROVE", ""))
    participants = [
        ConsensusParticipant("a", fixed("REJECT"), voter=always_approve),
        ConsensusParticipant("b", fixed("APPROVE"), voter=declines),
        ConsensusParticipant("c", fixed("REJECT")),
    ]
    result = ConsensusEngine(client, participants).run("x")
    votes = [(s, p.vote, p.reason) for t, s, _, p in client.sent if t == "Vote"]
    assert votes == [("a", "APPROVE", "yes"), ("c", "REJECT", "ok")]
    assert result["outcome"] == "reject"
    assert result["commitment"]["reason"] == "1/3 approvals (threshold 2)"


def test_custom_voter_bad_vote_cancels(client: FakeClient) -> None:
    bad = function_voter(lambda proj: True, lambda proj: VoteResult("maybe", ""))
    node = ConsensusEngine(client, [ConsensusParticipant("a", fixed("APPROVE"), voter=bad)])
    with pytest.raises(MacpSessionError):
        node.run("x")
    assert client.cancelled == ["macp_consensus failed"]


def test_bearer_auth_per_participant(client: FakeClient) -> None:
    auth = AuthConfig.for_bearer("secret", sender_hint="svc-a", expected_sender="a")
    node = ConsensusEngine(client, [ConsensusParticipant("a", fixed("APPROVE"), auth=auth)])
    node.run("x")
    assert [a for t, _, a, _ in client.sent if t == "Vote"] == ["svc-a"]  # the given auth


# ── quorum ───────────────────────────────────────────────────────────


def test_quorum_approved(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("APPROVE", "REVIEW", "APPROVE"), mode="quorum")
    result = node.run({"option": "rotate keys", "rationale": "quarterly"})
    assert client.types() == [
        ("SessionStart", "coordinator"),
        ("ApprovalRequest", "coordinator"),
        ("Approve", "agent-0"),
        ("Abstain", "agent-1"),
        ("Approve", "agent-2"),
        ("Commitment", "coordinator"),
    ]
    assert list(client.payload("SessionStart").participants) == ["agent-0", "agent-1", "agent-2"]
    request = client.payload("ApprovalRequest")
    assert (request.request_id, request.action, request.summary) == (
        "r1",
        "rotate keys",
        "quarterly",
    )
    assert request.required_approvals == 2
    assert result["mode"] == "macp.mode.quorum.v1"
    assert result["outcome"] == "approve"


def test_quorum_rejected_short_circuits(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("BLOCK", "REJECT", "APPROVE"), mode="quorum")
    result = node.run("rotate keys")
    assert [t for t, _ in client.types()][2:] == ["Reject", "Reject", "Commitment"]
    assert result["outcome"] == "reject"
    assert result["approvals"] == 0


def test_threshold_and_unanimity(client: FakeClient) -> None:
    node = ConsensusEngine(client, voters("APPROVE", "APPROVE", "REJECT"), threshold=3)
    result = node.run("x")
    assert result["outcome"] == "reject"
    assert result["commitment"]["reason"] == "2/3 approvals (threshold 3)"


# ── failures ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fail_on", ["Proposal", "Evaluation", "Vote", "Commitment"])
def test_failure_after_start_cancels_and_reraises(fail_on: str) -> None:
    client = FakeClient(fail_on=fail_on)
    node = ConsensusEngine(client, voters("APPROVE", "APPROVE"))
    with pytest.raises(RuntimeError, match=f"boom on {fail_on}"):
        node.run("x")
    assert client.cancelled == ["macp_consensus failed"]


def test_failed_start_does_not_cancel() -> None:
    client = FakeClient(fail_on="SessionStart")
    with pytest.raises(RuntimeError):
        ConsensusEngine(client, voters("APPROVE")).run("x")
    assert client.cancelled == []


def test_cancel_failure_is_suppressed() -> None:
    client = FakeClient(fail_on="Vote")

    def broken_cancel(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cancel failed too")

    client.cancel_session = broken_cancel  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="boom on Vote"):
        ConsensusEngine(client, voters("APPROVE")).run("x")


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"recommendation": "MAYBE", "confidence": 0.5}, ValueError),
        ({"recommendation": 3, "confidence": 0.5}, ValueError),
        ({"recommendation": "APPROVE", "confidence": 1.5}, MacpSessionError),
        ({"recommendation": "APPROVE", "confidence": "high"}, ValueError),
        ({"recommendation": "APPROVE", "confidence": True}, ValueError),
        ("APPROVE", TypeError),
    ],
)
def test_bad_evaluator_result(client: FakeClient, answer: Any, error: type[Exception]) -> None:
    node = ConsensusEngine(client, [ConsensusParticipant("a", lambda p, i: answer)])
    with pytest.raises(error):
        node.run("x")
    assert client.cancelled == ["macp_consensus failed"]


@pytest.mark.parametrize(
    ("proposal", "error"),
    [
        (7, TypeError),
        ("", ValueError),
        ({"rationale": "no option"}, ValueError),
        ({"option": "x", "rationale": 1}, ValueError),
    ],
)
def test_bad_proposal(client: FakeClient, proposal: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        ConsensusEngine(client, voters("APPROVE")).run(proposal)
    assert client.sent == []  # rejected before the session opened


# ── construction ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"participants": []}, "at least one"),
        ({"participants": voters("APPROVE") * 2}, "unique"),
        ({"participants": [ConsensusParticipant("coordinator", fixed("APPROVE"))]}, "initiator"),
        ({"mode": "handoff"}, "mode"),
        ({"threshold": 0}, "threshold"),
        ({"threshold": 3}, "threshold"),
        ({"threshold": True}, "threshold"),
        ({"ttl_ms": 0}, "ttl_ms"),
    ],
)
def test_invalid_construction(client: FakeClient, kwargs: dict[str, Any], match: str) -> None:
    options: dict[str, Any] = {"participants": voters("APPROVE", "APPROVE"), **kwargs}
    with pytest.raises(ValueError, match=match):
        ConsensusEngine(client, **options)


def test_initiator_needed_for_bearer_client() -> None:
    client = FakeClient(agent=None)
    with pytest.raises(ValueError, match="initiator="):
        ConsensusEngine(client, voters("APPROVE"))
    node = ConsensusEngine(client, voters("APPROVE"), initiator="svc")
    node.run("x")
    assert list(client.payload("SessionStart").participants) == ["svc", "agent-0"]


def test_default_threshold_is_strict_majority(client: FakeClient) -> None:
    for n, expected in [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3)]:
        fresh = FakeClient()
        result = ConsensusEngine(fresh, voters(*["APPROVE"] * n)).run("x")
        assert result["threshold"] == expected


def test_normalize_recommendation() -> None:
    assert normalize_recommendation(" block ") == "BLOCK"
    assert normalize_recommendation("abstain") == "REVIEW"
    with pytest.raises(ValueError):
        normalize_recommendation("yes")


# ── receipt ──────────────────────────────────────────────────────────


def test_consensus_receipt_integrity_only(client: FakeClient) -> None:
    result = ConsensusEngine(client, voters("APPROVE")).run("x")
    receipt = consensus_receipt(
        result, issuer="did:web:example.com", issued_at="2026-01-01T00:00:00Z"
    )
    assert receipt["session_id"] == result["session_id"]
    assert receipt["mode"] == "macp.mode.decision.v1"
    assert receipt["commitment_hash"] == result["commitment_hash"]
    assert receipt["issued_at"] == "2026-01-01T00:00:00Z"
    assert verify_receipt(receipt, {})["verdict"] == "integrity_only"


def test_consensus_receipt_signed(client: FakeClient) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    seed = bytes(range(32))
    public = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    result = ConsensusEngine(client, voters("APPROVE")).run("x")
    receipt = consensus_receipt(
        result, issuer="did:web:example.com", signer=ReceiptSigner("k1", private_key=seed)
    )
    trusted = {"k1": base64.urlsafe_b64encode(public).rstrip(b"=").decode()}
    assert verify_receipt(receipt, trusted)["verdict"] == "attested"


def test_now_format() -> None:
    import re

    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", _now())
