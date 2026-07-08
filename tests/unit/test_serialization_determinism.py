"""Determinism guarantees for envelope + payload serialization (Q-6).

`docs/determinism.md` promises that an accepted envelope serializes to a
stable byte sequence and that replaying a stream produces the same
projection state. These tests lock that contract in:

- the same input produces the same bytes twice,
- reordering repeated fields (e.g. ``participants``) changes the bytes —
  the proto-3 wire format is canonical only when callers pre-normalise —
  so users need to be explicit about participant order. This test
  documents that behaviour so a future "silent reordering" change trips
  the assertion.
- envelopes built with identical inputs serialize identically (modulo
  the SDK-assigned ``message_id`` and ``timestamp_unix_ms``, which are
  populated from UUID / clock sources and excluded from the comparison).
"""

from __future__ import annotations

from macp.modes.decision.v1 import decision_pb2
from macp.modes.proposal.v1 import proposal_pb2
from macp.modes.quorum.v1 import quorum_pb2
from macp.modes.task.v1 import task_pb2

from macp_sdk.constants import MODE_DECISION
from macp_sdk.envelope import (
    build_commitment_payload,
    build_envelope,
    build_progress_payload,
    build_session_start_payload,
    build_signal_payload,
    serialize_message,
)
from tests.conftest import VALID_SESSION_ID


class TestPayloadDeterminism:
    def test_session_start_payload_bytes_are_stable(self):
        def make():
            return build_session_start_payload(
                intent="decide",
                participants=["alice", "bob"],
                ttl_ms=60_000,
                policy_version="policy.default",
            )

        a = serialize_message(make())
        b = serialize_message(make())
        assert a == b
        assert isinstance(a, bytes) and len(a) > 0

    def test_commitment_payload_stable_with_explicit_commitment_id(self):
        def make():
            return build_commitment_payload(
                action="approved",
                authority_scope="release",
                reason="majority",
                commitment_id="cmt-1",
            )

        assert serialize_message(make()) == serialize_message(make())

    def test_signal_payload_stable(self):
        def make():
            return build_signal_payload(signal_type="heartbeat", data=b"\x01\x02", confidence=0.5)

        assert serialize_message(make()) == serialize_message(make())

    def test_progress_payload_stable(self):
        def make():
            return build_progress_payload(
                progress_token="tok",
                progress=0.25,
                total=1.0,
                message="halfway",
            )

        assert serialize_message(make()) == serialize_message(make())

    def test_participant_order_matters(self):
        """Different participant orderings MUST produce different bytes.

        This is the expected behaviour of proto-3 repeated fields. The SDK
        deliberately does NOT sort participants — it preserves caller
        order so downstream projection logic can rely on it. If we ever
        add auto-sorting, this test will loudly fail.
        """
        p1 = build_session_start_payload(intent="x", participants=["alice", "bob"], ttl_ms=1)
        p2 = build_session_start_payload(intent="x", participants=["bob", "alice"], ttl_ms=1)
        assert serialize_message(p1) != serialize_message(p2)


class TestModeActionPayloadDeterminism:
    """Each mode's top-level action payload must serialize deterministically."""

    def test_decision_proposal_payload(self):
        def make():
            return decision_pb2.ProposalPayload(
                proposal_id="p1",
                option="deploy-v2",
                rationale="tests passed",
            )

        assert serialize_message(make()) == serialize_message(make())

    def test_decision_vote_payload(self):
        def make():
            return decision_pb2.VotePayload(proposal_id="p1", vote="APPROVE", reason="ship it")

        assert serialize_message(make()) == serialize_message(make())

    def test_proposal_payload(self):
        def make():
            return proposal_pb2.ProposalPayload(
                proposal_id="p1",
                title="Plan A",
                summary="$100k",
                tags=["std", "basic"],
            )

        assert serialize_message(make()) == serialize_message(make())

    def test_task_request_payload(self):
        def make():
            return task_pb2.TaskRequestPayload(
                task_id="t1",
                title="Build widget",
                instructions="assemble parts",
            )

        assert serialize_message(make()) == serialize_message(make())

    def test_quorum_approval_request_payload(self):
        def make():
            return quorum_pb2.ApprovalRequestPayload(
                request_id="r1",
                action="deploy",
                required_approvals=2,
            )

        assert serialize_message(make()) == serialize_message(make())


class TestEnvelopeDeterminismExcludingGeneratedFields:
    """build_envelope stamps a UUID message_id and clock timestamp.

    Rather than mock them, we feed explicit values so the whole envelope
    is byte-for-byte stable. This is the pattern replay tooling should
    use to re-serialize captured transcripts.
    """

    def test_envelope_bytes_stable_with_explicit_message_id_and_timestamp(self):
        payload = build_session_start_payload(intent="x", participants=["a"], ttl_ms=1)
        payload_bytes = serialize_message(payload)

        def make_env():
            return build_envelope(
                mode=MODE_DECISION,
                message_type="SessionStart",
                session_id=VALID_SESSION_ID,
                sender="alice",
                payload=payload_bytes,
                message_id="00000000-0000-4000-8000-000000000aaa",
                timestamp_unix_ms=1_700_000_000_000,
            )

        assert serialize_message(make_env()) == serialize_message(make_env())
