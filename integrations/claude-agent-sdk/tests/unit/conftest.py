from __future__ import annotations

from typing import Any

import pytest
from macp.modes.decision.v1 import decision_pb2
from macp.modes.quorum.v1 import quorum_pb2
from macp.v1 import core_pb2, envelope_pb2
from macp_sdk import AuthConfig

_PAYLOADS: dict[str, type[Any]] = {
    "SessionStart": core_pb2.SessionStartPayload,
    "Proposal": decision_pb2.ProposalPayload,
    "Evaluation": decision_pb2.EvaluationPayload,
    "Vote": decision_pb2.VotePayload,
    "ApprovalRequest": quorum_pb2.ApprovalRequestPayload,
    "Approve": quorum_pb2.ApprovePayload,
    "Reject": quorum_pb2.RejectPayload,
    "Abstain": quorum_pb2.AbstainPayload,
    "Commitment": core_pb2.CommitmentPayload,
}


class FakeClient:
    """Acks every envelope and records it, decoded. No runtime involved."""

    def __init__(self, agent: str | None = "coordinator", fail_on: str | None = None) -> None:
        self.auth = AuthConfig.for_dev_agent(agent) if agent else AuthConfig.for_bearer("tok")
        self.fail_on = fail_on
        self.sent: list[tuple[str, str, str | None, Any]] = []
        self.cancelled: list[str] = []

    def send(self, envelope: envelope_pb2.Envelope, auth: AuthConfig | None = None) -> Any:
        if envelope.message_type == self.fail_on:
            raise RuntimeError(f"boom on {envelope.message_type}")
        payload = _PAYLOADS[envelope.message_type]()
        payload.ParseFromString(envelope.payload)
        self.sent.append(
            (envelope.message_type, envelope.sender, auth.sender_hint if auth else None, payload)
        )
        return envelope_pb2.Ack(ok=True)

    def cancel_session(self, session_id: str, *, reason: str = "", auth: Any = None) -> Any:
        self.cancelled.append(reason)
        return envelope_pb2.Ack(ok=True)

    def types(self) -> list[tuple[str, str]]:
        return [(t, s) for t, s, _, _ in self.sent]

    def payload(self, message_type: str) -> Any:
        return next(p for t, _, _, p in self.sent if t == message_type)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()
