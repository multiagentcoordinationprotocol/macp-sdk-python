"""Integration tests for the ``ext.multi_round.v1`` session helper.

The spec's example transcript (``examples/multi-round-session.json``) against
a live runtime: alice contributes A, bob B, bob changes to A, and the session
converges but stays OPEN until the initiator commits. Also an initiator
Commitment sent before convergence, which the runtime refuses with
``INVALID_ENVELOPE`` (the session helper deliberately leaves that check to the
runtime).
"""

from __future__ import annotations

import pytest
from macp.v1 import envelope_pb2

from macp_sdk import AuthConfig, MacpAckError, MultiRoundSession
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration

ALICE, BOB, COORD = "mr-alice", "mr-bob", "mr-coordinator"


def _as(agent: str) -> dict[str, object]:
    return {"sender": agent, "auth": AuthConfig.for_dev_agent(agent)}


def _session_state(session: MultiRoundSession) -> str:
    state: str = envelope_pb2.SessionState.Name(session.metadata().metadata.state)
    return state


class TestMultiRound:
    def test_converges_then_initiator_commits(self) -> None:
        client = make_client(COORD)
        try:
            session = MultiRoundSession(client)
            session.start(
                intent="converge on a rollout option", participants=[ALICE, BOB], ttl_ms=60_000
            )
            session.contribute("option_a", **_as(ALICE))
            session.contribute("option_b", **_as(BOB))
            session.contribute("option_a", **_as(BOB))

            projection = session.multi_round_projection
            assert projection.converged
            assert projection.converged_value == "option_a"
            assert projection.round == 3
            # No auto-resolve: convergence leaves the session OPEN.
            assert _session_state(session) == "SESSION_STATE_OPEN"

            ack = session.commit(authority_scope="rollout", reason="all participants agree")
            assert ack.ok
            assert projection.phase == "Committed"
            assert _session_state(session) == "SESSION_STATE_RESOLVED"
        finally:
            client.close()

    def test_commit_before_convergence_is_refused(self) -> None:
        client = make_client(COORD)
        try:
            session = MultiRoundSession(client)
            session.start(intent="early commit", participants=[ALICE, BOB], ttl_ms=60_000)
            session.contribute("option_a", **_as(ALICE))
            with pytest.raises(MacpAckError) as excinfo:
                session.commit(authority_scope="rollout", reason="too early")
            assert excinfo.value.failure.code == "INVALID_ENVELOPE"
            assert not session.multi_round_projection.is_committed
            assert _session_state(session) == "SESSION_STATE_OPEN"
        finally:
            client.close()
