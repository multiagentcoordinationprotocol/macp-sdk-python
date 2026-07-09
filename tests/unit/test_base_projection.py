"""Direct unit tests for the shared ``BaseProjection`` ABC."""

from __future__ import annotations

from macp.v1 import core_pb2

from macp_sdk.base_projection import BaseProjection
from tests.conftest import make_envelope

TEST_MODE = "macp.mode.test.v1"


class _Projection(BaseProjection):
    MODE = TEST_MODE

    def __init__(self) -> None:
        super().__init__()
        self.mode_messages: list[str] = []

    def _apply_mode_message(self, envelope) -> None:
        self.mode_messages.append(envelope.message_type)


def _commitment(outcome_positive: bool = True) -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="c1",
        action="done",
        authority_scope="test",
        reason="ok",
        outcome_positive=outcome_positive,
    )


class TestApplyEnvelope:
    def test_wrong_mode_envelope_ignored(self):
        proj = _Projection()
        env = make_envelope("macp.mode.other.v1", "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        assert proj.transcript == []
        assert proj.mode_messages == []

    def test_matching_mode_appended_and_dispatched(self):
        proj = _Projection()
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.mode_messages == ["Proposal"]

    def test_commitment_not_dispatched_to_mode_handler(self):
        proj = _Projection()
        proj.apply_envelope(make_envelope(TEST_MODE, "Commitment", _commitment()))
        assert proj.mode_messages == []


class TestCommitmentState:
    def test_initial_state(self):
        proj = _Projection()
        assert not proj.is_committed
        assert proj.commitment is None
        assert proj.phase == ""
        assert proj.is_positive_outcome is None

    def test_commitment_sets_state(self):
        proj = _Projection()
        proj.apply_envelope(make_envelope(TEST_MODE, "Commitment", _commitment()))
        assert proj.is_committed
        assert proj.phase == "Committed"
        assert proj.commitment is not None
        assert proj.commitment.commitment_id == "c1"
        assert proj.is_positive_outcome is True

    def test_negative_outcome_surfaces(self):
        proj = _Projection()
        proj.apply_envelope(
            make_envelope(TEST_MODE, "Commitment", _commitment(outcome_positive=False))
        )
        assert proj.is_committed
        assert proj.is_positive_outcome is False
