"""Conformance tests: replay fixture messages through SDK projections.

These tests validate that the SDK's local projections correctly track state
when fed the same messages the runtime would accept. Reject-path messages
(expect=="reject") are skipped since rejection is enforced by the runtime,
not by the SDK projection layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.protobuf.descriptor import FieldDescriptor
from macp.modes.decision.v1 import decision_pb2
from macp.modes.handoff.v1 import handoff_pb2
from macp.modes.proposal.v1 import proposal_pb2
from macp.modes.quorum.v1 import quorum_pb2
from macp.modes.task.v1 import task_pb2
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk.envelope import new_message_id, now_unix_ms, serialize_message
from macp_sdk.handoff import HandoffProjection
from macp_sdk.projections import DecisionProjection
from macp_sdk.proposal import ProposalProjection
from macp_sdk.quorum import QuorumProjection
from macp_sdk.task import TaskProjection

FIXTURES_DIR = Path(__file__).parent

PAYLOAD_BUILDERS: dict[str, type] = {
    "decision.Proposal": decision_pb2.ProposalPayload,
    "decision.Evaluation": decision_pb2.EvaluationPayload,
    "decision.Objection": decision_pb2.ObjectionPayload,
    "decision.Vote": decision_pb2.VotePayload,
    "proposal.Proposal": proposal_pb2.ProposalPayload,
    "proposal.CounterProposal": proposal_pb2.CounterProposalPayload,
    "proposal.Accept": proposal_pb2.AcceptPayload,
    "proposal.Reject": proposal_pb2.RejectPayload,
    "proposal.Withdraw": proposal_pb2.WithdrawPayload,
    "task.TaskRequest": task_pb2.TaskRequestPayload,
    "task.TaskAccept": task_pb2.TaskAcceptPayload,
    "task.TaskReject": task_pb2.TaskRejectPayload,
    "task.TaskUpdate": task_pb2.TaskUpdatePayload,
    "task.TaskComplete": task_pb2.TaskCompletePayload,
    "task.TaskFail": task_pb2.TaskFailPayload,
    "handoff.HandoffOffer": handoff_pb2.HandoffOfferPayload,
    "handoff.HandoffContext": handoff_pb2.HandoffContextPayload,
    "handoff.HandoffAccept": handoff_pb2.HandoffAcceptPayload,
    "handoff.HandoffDecline": handoff_pb2.HandoffDeclinePayload,
    "quorum.ApprovalRequest": quorum_pb2.ApprovalRequestPayload,
    "quorum.Approve": quorum_pb2.ApprovePayload,
    "quorum.Reject": quorum_pb2.RejectPayload,
    "quorum.Abstain": quorum_pb2.AbstainPayload,
    "Commitment": core_pb2.CommitmentPayload,
}

MODE_PROJECTIONS: dict[str, type] = {
    "macp.mode.decision.v1": DecisionProjection,
    "macp.mode.proposal.v1": ProposalProjection,
    "macp.mode.task.v1": TaskProjection,
    "macp.mode.handoff.v1": HandoffProjection,
    "macp.mode.quorum.v1": QuorumProjection,
}


def _build_payload(payload_type: str, payload_data: dict) -> bytes:
    cls = PAYLOAD_BUILDERS.get(payload_type)
    if cls is None:
        raise ValueError(f"Unknown payload_type: {payload_type}")
    fields = cls.DESCRIPTOR.fields_by_name
    # Build kwargs from the fixture payload, coercing JSON scalars to the proto
    # field types. Fixtures are JSON, so `bytes` proto fields (e.g.
    # HandoffContext.context, *_data/output) arrive as plain strings and must be
    # UTF-8 encoded before construction. Repeated fields are skipped (the
    # projections under test don't assert on them).
    filtered = {}
    for k, v in payload_data.items():
        if isinstance(v, list):
            continue
        field = fields.get(k)
        if field is not None and field.type == FieldDescriptor.TYPE_BYTES and isinstance(v, str):
            v = v.encode("utf-8")
        filtered[k] = v
    msg = cls(**filtered)
    return serialize_message(msg)


def _build_envelope(mode: str, msg: dict, session_id: str) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=mode,
        message_type=msg["message_type"],
        message_id=new_message_id(),
        session_id=session_id,
        sender=msg["sender"],
        timestamp_unix_ms=now_unix_ms(),
        payload=_build_payload(msg["payload_type"], msg["payload"]),
    )


def _load_fixtures():
    """Yield (fixture_name, fixture_data) for every JSON fixture."""
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path) as f:
            yield path.stem, json.load(f)


FIXTURES = list(_load_fixtures())
FIXTURE_IDS = [name for name, _ in FIXTURES]


@pytest.mark.conformance
@pytest.mark.parametrize("name,fixture", FIXTURES, ids=FIXTURE_IDS)
def test_projection_replay(name: str, fixture: dict):
    """Replay accepted messages through the projection and verify commitment state."""
    mode = fixture["mode"]
    projection_cls = MODE_PROJECTIONS.get(mode)
    if projection_cls is None:
        pytest.skip(
            f"No projection for mode {mode} — "
            "multi_round is an extension mode; SDK projection not yet implemented"
        )

    projection = projection_cls()
    session_id = "conformance-session"

    # Replay only accepted messages (rejection is runtime-side). Reject-path
    # fixtures still replay their accepted prefix, in lockstep with the
    # TypeScript harness.
    accepted_count = 0
    for msg in fixture["messages"]:
        if msg.get("expect") != "accept":
            continue
        envelope = _build_envelope(mode, msg, session_id)
        projection.apply_envelope(envelope)
        accepted_count += 1

    # Verify transcript was tracked
    assert len(projection.transcript) == accepted_count

    # Commitment presence is driven by the terminal state: Resolved ⇒ committed.
    expected_final = fixture.get("expected_final_state", "Open")
    if expected_final == "Resolved":
        assert projection.is_committed, f"Expected committed state for {name}"
        assert projection.commitment is not None
    else:
        assert not projection.is_committed, f"Expected non-committed state for {name}"

    # Verify every scalar field of expected_resolution (incl. outcome_positive).
    expected_res = fixture.get("expected_resolution") or {}
    if expected_res:
        assert projection.commitment is not None
        for key, val in expected_res.items():
            assert getattr(projection.commitment, key) == val, f"{name}: commitment.{key}"

    # Verify mode-state phase and recorded votes when the fixture specifies them.
    expected_mode_state = fixture.get("expected_mode_state") or {}
    if "phase" in expected_mode_state:
        assert projection.phase == expected_mode_state["phase"], f"{name}: phase"
    if "votes" in expected_mode_state:
        for proposal_id, by_sender in expected_mode_state["votes"].items():
            for sender, record in by_sender.items():
                assert projection.votes[proposal_id][sender].vote == record["vote"], (
                    f"{name}: vote {proposal_id}/{sender}"
                )
