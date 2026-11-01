"""Unit tests for the ``ext.multi_round.v1`` session helper and projection.

Test names are shared with macp-sdk-typescript's ``multi-round`` tests.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk import (
    MULTI_ROUND_CONVERGED_ACTION,
    MULTI_ROUND_MODE_VERSION,
    AuthConfig,
    MacpSessionError,
    MultiRoundProjection,
    MultiRoundSession,
    build_contribute_payload,
    build_session_start_payload,
)
from macp_sdk.constants import MODE_MULTI_ROUND
from macp_sdk.envelope import build_commitment_ref, build_envelope, serialize_message
from tests.conftest import VALID_SESSION_ID, make_ack

ALICE, BOB, COORD = "agent://alice", "agent://bob", "agent://coordinator"
_ids = iter(range(10**6))


def _env(message_type: str, sender: str, payload: bytes) -> envelope_pb2.Envelope:
    return build_envelope(
        mode=MODE_MULTI_ROUND,
        message_type=message_type,
        session_id=VALID_SESSION_ID,
        sender=sender,
        payload=payload,
        message_id=f"m{next(_ids)}",
    )


def _start(participants: list[str] | None = None, sender: str = COORD) -> envelope_pb2.Envelope:
    payload = build_session_start_payload(
        intent="converge",
        participants=participants if participants is not None else [ALICE, BOB],
        ttl_ms=60_000,
        mode_version="1.0.0",
        configuration_version="cfg-1",
    )
    return _env("SessionStart", sender, serialize_message(payload))


def _contribute(sender: str, value: str) -> envelope_pb2.Envelope:
    return _env("Contribute", sender, serialize_message(build_contribute_payload(value)))


def _started() -> MultiRoundProjection:
    projection = MultiRoundProjection()
    projection.apply_envelope(_start())
    return projection


# ── Projection ───────────────────────────────────────────────────────


class TestProjection:
    def test_session_start_learns_participants_and_initiator(self) -> None:
        projection = MultiRoundProjection()
        assert projection.phase == "Pending"
        assert projection.initiator is None
        projection.apply_envelope(_start())
        assert projection.participants == [ALICE, BOB]
        assert projection.initiator == COORD
        assert projection.phase == "Contributing"

    def test_first_contribution_advances_round(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(ALICE, "A"))
        assert projection.round == 1
        assert projection.contributions == {ALICE: "A"}
        assert not projection.converged

    def test_same_value_does_not_advance(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(ALICE, "A"))
        projection.apply_envelope(_contribute(ALICE, "A"))
        assert projection.round == 1
        assert len(projection.transcript) == 3

    def test_changed_value_advances(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(ALICE, "A"))
        projection.apply_envelope(_contribute(ALICE, "B"))
        assert projection.round == 2
        assert projection.contributions == {ALICE: "B"}

    def test_converges_when_all_equal(self) -> None:
        # The spec's example transcript: alice A, bob B, bob A.
        projection = _started()
        for sender, value in ((ALICE, "A"), (BOB, "B"), (BOB, "A")):
            assert not projection.converged
            projection.apply_envelope(_contribute(sender, value))
        assert projection.converged
        assert projection.converged_value == "A"
        assert projection.round == 3
        assert projection.phase == "Converged"

    def test_not_converged_until_every_participant_contributes(self) -> None:
        projection = MultiRoundProjection()
        projection.apply_envelope(_start([ALICE, BOB, COORD]))
        projection.apply_envelope(_contribute(ALICE, "A"))
        projection.apply_envelope(_contribute(BOB, "A"))
        assert not projection.converged
        assert projection.converged_value is None

    def test_convergence_is_sticky(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(ALICE, "A"))
        projection.apply_envelope(_contribute(BOB, "A"))
        projection.apply_envelope(_contribute(BOB, "B"))
        assert projection.converged
        assert projection.converged_value == "A"
        assert projection.contributions == {ALICE: "A", BOB: "A"}
        assert projection.round == 2

    def test_non_participant_contribution_ignored(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(COORD, "A"))
        assert projection.round == 0
        assert projection.contributions == {}

    def test_unknown_participants_never_converge(self) -> None:
        projection = MultiRoundProjection()
        projection.apply_envelope(_contribute(ALICE, "A"))
        assert projection.contributions == {ALICE: "A"}
        assert not projection.converged

    @pytest.mark.parametrize(
        "payload",
        [b"", b"\xff\xfe", json.dumps({"value": 5}).encode()],
        ids=["empty", "undecodable", "non-string-json"],
    )
    def test_undecodable_contribution_is_a_no_op(self, payload: bytes) -> None:
        projection = _started()
        projection.apply_envelope(_env("Contribute", ALICE, payload))
        assert projection.round == 0
        assert projection.contributions == {}
        assert len(projection.transcript) == 2

    def test_legacy_json_contribution_decodes(self) -> None:
        projection = _started()
        projection.apply_envelope(_env("Contribute", ALICE, b'{"value": "A"}'))
        assert projection.contributions == {ALICE: "A"}

    def test_constructor_seeds_roster_and_session_start_overwrites(self) -> None:
        projection = MultiRoundProjection(participants=[ALICE], initiator="agent://x")
        projection.apply_envelope(_contribute(ALICE, "A"))
        assert projection.converged
        fresh = MultiRoundProjection(participants=[ALICE], initiator="agent://x")
        fresh.apply_envelope(_start())
        assert fresh.participants == [ALICE, BOB]
        assert fresh.initiator == COORD

    def test_commitment_moves_to_committed(self) -> None:
        projection = _started()
        projection.apply_envelope(_contribute(ALICE, "A"))
        projection.apply_envelope(_contribute(BOB, "A"))
        commitment = core_pb2.CommitmentPayload(commitment_id="c1", action="multi_round.converged")
        projection.apply_envelope(_env("Commitment", COORD, serialize_message(commitment)))
        assert projection.is_committed
        assert projection.phase == "Committed"

    def test_redelivery_is_a_no_op(self) -> None:
        projection = _started()
        env = _contribute(ALICE, "A")
        projection.apply_envelope(env)
        projection.apply_envelope(_contribute(ALICE, "B"))
        projection.apply_envelope(env)
        assert projection.contributions == {ALICE: "B"}
        assert projection.round == 2


# ── Session ──────────────────────────────────────────────────────────


def _ok(envelope: envelope_pb2.Envelope, **_: Any) -> envelope_pb2.Ack:
    return make_ack(ok=True, session_id=envelope.session_id, message_id=envelope.message_id)


def _session(**kwargs: Any) -> tuple[MultiRoundSession, MagicMock]:
    client = MagicMock()
    client.auth = AuthConfig.for_dev_agent(COORD)
    client.send.side_effect = _ok
    return MultiRoundSession(client, session_id=VALID_SESSION_ID, **kwargs), client


def _sent(client: MagicMock) -> envelope_pb2.Envelope:
    envelope: envelope_pb2.Envelope = client.send.call_args.args[0]
    return envelope


def _auth(agent: str) -> dict[str, Any]:
    return {"sender": agent, "auth": AuthConfig.for_dev_agent(agent)}


def _converged_session() -> tuple[MultiRoundSession, MagicMock]:
    session, client = _session(configuration_version="cfg-1")
    session.start(intent="i", participants=[ALICE, BOB], ttl_ms=60_000)
    session.contribute("A", **_auth(ALICE))
    session.contribute("A", **_auth(BOB))
    return session, client


class TestSession:
    def test_constants(self) -> None:
        assert MULTI_ROUND_MODE_VERSION == "1.0.0"
        assert MULTI_ROUND_CONVERGED_ACTION == "multi_round.converged"

    def test_example_flow(self) -> None:
        session, client = _session(configuration_version="cfg-1")
        session.start(intent="i", participants=[ALICE, BOB], ttl_ms=60_000)
        session.contribute("A", **_auth(ALICE))
        session.contribute("B", **_auth(BOB))
        session.contribute("A", **_auth(BOB))
        contribute = _sent(client)
        assert contribute.mode == MODE_MULTI_ROUND
        assert contribute.message_type == "Contribute"
        # Canonical protobuf, never the legacy JSON form.
        assert contribute.payload == build_contribute_payload("A").SerializeToString()
        projection = session.multi_round_projection
        assert projection.converged and projection.round == 3
        session.commit(authority_scope="rollout", reason="converged")
        commitment = core_pb2.CommitmentPayload.FromString(_sent(client).payload)
        assert commitment.action == "multi_round.converged"
        assert commitment.outcome_positive is True
        assert commitment.commitment_id
        assert (commitment.mode_version, commitment.configuration_version) == ("1.0.0", "cfg-1")
        assert commitment.policy_version == session.policy_version
        assert projection.phase == "Committed"

    def test_start_refuses_other_mode_version(self) -> None:
        session, client = _session(mode_version="2.0.0")
        with pytest.raises(MacpSessionError, match="mode_version"):
            session.start(intent="i", participants=[ALICE], ttl_ms=1)
        client.send.assert_not_called()

    def test_start_refuses_empty_participants(self) -> None:
        session, client = _session()
        with pytest.raises(MacpSessionError):
            session.start(intent="i", participants=[], ttl_ms=1)
        client.send.assert_not_called()

    def test_contribute_refuses_empty_value(self) -> None:
        session, client = _session()
        with pytest.raises(MacpSessionError, match="non-empty"):
            session.contribute("", **_auth(ALICE))
        client.send.assert_not_called()

    def test_contribute_refuses_after_convergence(self) -> None:
        session, client = _converged_session()
        with pytest.raises(MacpSessionError, match="converged"):
            session.contribute("A", **_auth(ALICE))
        assert client.send.call_count == 3

    def test_contribute_refuses_non_participant_initiator(self) -> None:
        session, client = _session()
        session.start(intent="i", participants=[ALICE, BOB], ttl_ms=1)
        with pytest.raises(MacpSessionError, match="participant"):
            session.contribute("A")
        assert client.send.call_count == 1

    def test_contribute_allowed_while_participants_unknown(self) -> None:
        session, client = _session()
        session.contribute("A", **_auth(ALICE))
        assert client.send.call_count == 1

    def test_commit_refuses_non_initiator(self) -> None:
        session, client = _converged_session()
        with pytest.raises(MacpSessionError, match="initiator"):
            session.commit(authority_scope="s", reason="r", **_auth(ALICE))
        assert client.send.call_count == 3

    def test_commit_does_not_wait_for_convergence(self) -> None:
        session, client = _session()
        session.start(intent="i", participants=[ALICE, BOB], ttl_ms=1)
        session.commit(authority_scope="s", reason="r")
        assert _sent(client).message_type == "Commitment"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"authority_scope": " ", "reason": "r"},
            {"authority_scope": "s", "reason": ""},
            {"authority_scope": "s", "reason": "r", "action": "  "},
            {"authority_scope": "s", "reason": "r", "commitment_id": " "},
            {
                "authority_scope": "s",
                "reason": "r",
                "action": "x.rejected",
                "outcome_positive": True,
            },
            {
                "authority_scope": "s",
                "reason": "r",
                "action": "x.approved",
                "outcome_positive": False,
            },
            {
                "authority_scope": "s",
                "reason": "r",
                "supersedes": core_pb2.CommitmentRef(
                    session_id=" ", commitment_hash="sha256:" + "0" * 64
                ),
            },
            {
                "authority_scope": "s",
                "reason": "r",
                "supersedes": core_pb2.CommitmentRef(session_id="s0", commitment_hash="sha256:XYZ"),
            },
        ],
        ids=[
            "blank-scope",
            "blank-reason",
            "blank-action",
            "blank-commitment-id",
            "negative-suffix-positive",
            "positive-suffix-negative",
            "supersedes-blank-session",
            "supersedes-bad-hash",
        ],
    )
    def test_commit_refuses_malformed_payload(self, kwargs: dict[str, Any]) -> None:
        session, client = _session()
        with pytest.raises(MacpSessionError):
            session.commit(**kwargs)
        client.send.assert_not_called()

    @pytest.mark.parametrize(
        ("action", "outcome_positive"),
        [("multi_round.converged", False), ("x.REJECTED", True), ("x.approved", True)],
    )
    def test_commit_allows_unconstrained_or_agreeing_outcome(
        self, action: str, outcome_positive: bool
    ) -> None:
        # multi_round.converged matches no suffix, and the match is case-sensitive.
        session, client = _session()
        session.commit(
            authority_scope="s", reason="r", action=action, outcome_positive=outcome_positive
        )
        commitment = core_pb2.CommitmentPayload.FromString(_sent(client).payload)
        assert commitment.outcome_positive is outcome_positive

    def test_commit_carries_supersedes(self) -> None:
        session, client = _session()
        ref = build_commitment_ref(session_id="s0", commitment_hash="sha256:" + "a" * 64)
        session.commit(authority_scope="s", reason="r", supersedes=ref)
        commitment = core_pb2.CommitmentPayload.FromString(_sent(client).payload)
        assert commitment.supersedes == ref

    def test_nack_leaves_projection_untouched(self) -> None:
        session, client = _session()
        session.start(intent="i", participants=[ALICE, BOB], ttl_ms=1)
        client.send.side_effect = None
        client.send.return_value = make_ack(ok=False, session_id=VALID_SESSION_ID)
        session.contribute("A", **_auth(ALICE))
        assert session.multi_round_projection.round == 0
