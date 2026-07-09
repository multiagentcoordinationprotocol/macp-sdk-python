"""Integration tests for all MACP session modes against a live runtime.

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import pytest

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.decision import DecisionSession
from macp_sdk.handoff import HandoffSession
from macp_sdk.proposal import ProposalSession
from macp_sdk.quorum import QuorumSession
from macp_sdk.task import TaskSession
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration


def _auth(agent_id: str) -> AuthConfig:
    return AuthConfig.for_dev_agent(agent_id)


@pytest.fixture(scope="module")
def client() -> MacpClient:
    c = make_client("coordinator")
    yield c
    c.close()


# ── Client basics ─────────────────────────────────────────────────────


class TestClientBasics:
    def test_initialize(self, client: MacpClient) -> None:
        resp = client.initialize()
        assert resp.runtime_info.name

    def test_list_modes(self, client: MacpClient) -> None:
        resp = client.list_modes()
        mode_names = [m.mode for m in resp.modes]
        assert "macp.mode.decision.v1" in mode_names
        assert "macp.mode.proposal.v1" in mode_names
        assert "macp.mode.task.v1" in mode_names
        assert "macp.mode.handoff.v1" in mode_names
        assert "macp.mode.quorum.v1" in mode_names

    def test_get_manifest(self, client: MacpClient) -> None:
        resp = client.get_manifest()
        assert resp is not None


# ── Decision mode ─────────────────────────────────────────────────────


class TestDecisionMode:
    def test_happy_path(self, client: MacpClient) -> None:
        session = DecisionSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="integration test decision",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=30_000,
        )
        assert ack.ok

        ack = session.propose("p1", "Option A", rationale="test")
        assert ack.ok

        ack = session.evaluate("p1", "APPROVE", confidence=0.9, sender="alice", auth=_auth("alice"))
        assert ack.ok

        ack = session.vote("p1", "APPROVE", sender="alice", auth=_auth("alice"))
        assert ack.ok

        ack = session.vote("p1", "APPROVE", sender="bob", auth=_auth("bob"))
        assert ack.ok

        ack = session.commit(
            action="decision.approved",
            authority_scope="test",
            reason="majority approved",
        )
        assert ack.ok

        proj = session.decision_projection
        # vote_totals() returns {proposal_id: approve_count}
        assert proj.vote_totals()["p1"] == 2
        assert proj.commitment is not None

    def test_cancel(self, client: MacpClient) -> None:
        session = DecisionSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="cancel test",
            participants=["coordinator"],
            ttl_ms=30_000,
        )
        assert ack.ok

        cancel_ack = session.cancel(reason="no longer needed")
        assert cancel_ack.ok

    def test_get_session_metadata(self, client: MacpClient) -> None:
        session = DecisionSession(client, auth=_auth("coordinator"))
        session.start(
            intent="metadata test",
            participants=["coordinator"],
            ttl_ms=30_000,
        )
        resp = session.metadata()
        assert resp.metadata.session_id == session.session_id


# ── Proposal mode ─────────────────────────────────────────────────────


class TestProposalMode:
    def test_happy_path(self, client: MacpClient) -> None:
        session = ProposalSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="integration test proposal",
            participants=["coordinator", "buyer", "seller"],
            ttl_ms=30_000,
        )
        assert ack.ok

        ack = session.propose(
            "p1", "Standard Plan", summary="$100k", sender="seller", auth=_auth("seller")
        )
        assert ack.ok

        ack = session.counter_propose(
            "p2", "p1", "Counter Plan", summary="$80k", sender="buyer", auth=_auth("buyer")
        )
        assert ack.ok

        # all_parties acceptance: every participant must accept for convergence
        ack = session.accept("p2", reason="agreed", sender="seller", auth=_auth("seller"))
        assert ack.ok
        ack = session.accept("p2", reason="agreed", sender="buyer", auth=_auth("buyer"))
        assert ack.ok
        ack = session.accept("p2", reason="agreed", sender="coordinator", auth=_auth("coordinator"))
        assert ack.ok

        ack = session.commit(
            action="contract.accepted",
            authority_scope="procurement",
            reason="all parties accepted p2",
        )
        assert ack.ok

        proj = session.proposal_projection
        assert proj.accepted_proposal() == "p2"
        assert proj.commitment is not None


# ── Task mode ─────────────────────────────────────────────────────────


class TestTaskMode:
    def test_happy_path(self, client: MacpClient) -> None:
        session = TaskSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="integration test task",
            participants=["coordinator", "worker"],
            ttl_ms=30_000,
        )
        assert ack.ok

        ack = session.request_task("t1", "Build widget", instructions="build it")
        assert ack.ok

        ack = session.accept_task("t1", sender="worker", auth=_auth("worker"))
        assert ack.ok

        ack = session.update_task(
            "t1",
            status="in_progress",
            progress=0.5,
            message="halfway",
            sender="worker",
            auth=_auth("worker"),
        )
        assert ack.ok

        ack = session.complete_task("t1", summary="done", sender="worker", auth=_auth("worker"))
        assert ack.ok

        ack = session.commit(
            action="task.completed",
            authority_scope="project",
            reason="task finished",
        )
        assert ack.ok

        proj = session.task_projection
        assert proj.is_completed("t1")
        assert proj.commitment is not None


# ── Handoff mode ──────────────────────────────────────────────────────


class TestHandoffMode:
    def test_happy_path(self, client: MacpClient) -> None:
        # Handoff: only initiator can send HandoffOffer
        session = HandoffSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="integration test handoff",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=30_000,
        )
        assert ack.ok

        # Initiator (coordinator) offers handoff to bob
        ack = session.offer("h1", "bob", scope="support", reason="shift change")
        assert ack.ok

        ack = session.add_context(
            "h1",
            content_type="text/plain",
            context=b"customer context",
        )
        assert ack.ok

        ack = session.accept_handoff("h1", sender="bob", auth=_auth("bob"))
        assert ack.ok

        ack = session.commit(
            action="handoff.accepted",
            authority_scope="support",
            reason="bob accepted",
        )
        assert ack.ok

        proj = session.handoff_projection
        assert proj.is_accepted("h1")
        assert proj.commitment is not None


# ── Quorum mode ───────────────────────────────────────────────────────


class TestQuorumMode:
    def test_happy_path(self, client: MacpClient) -> None:
        session = QuorumSession(client, auth=_auth("coordinator"))
        ack = session.start(
            intent="integration test quorum",
            participants=["coordinator", "alice", "bob", "charlie"],
            ttl_ms=30_000,
        )
        assert ack.ok

        ack = session.request_approval(
            "r1",
            "deploy v2",
            summary="production deploy",
            required_approvals=2,
        )
        assert ack.ok

        ack = session.approve("r1", reason="lgtm", sender="alice", auth=_auth("alice"))
        assert ack.ok

        ack = session.approve("r1", reason="ship it", sender="bob", auth=_auth("bob"))
        assert ack.ok

        ack = session.commit(
            action="deploy.approved",
            authority_scope="ops",
            reason="quorum reached",
        )
        assert ack.ok

        proj = session.quorum_projection
        assert proj.has_quorum("r1")
        assert proj.approval_count("r1") == 2
        assert proj.commitment is not None
