"""``MacpConsensusNode`` against a real runtime (CI pins macp-runtime 0.9.0).

The Decision case runs ``examples/fraud_review_graph.py`` end to end. The
Quorum case and the async path use the node directly. Every session must end
``RESOLVED`` with the runtime holding the Commitment the node reported.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from macp.v1 import envelope_pb2
from macp_sdk import AuthConfig, MacpClient, verify_receipt

from langgraph_macp import ConsensusParticipant, MacpConsensusNode, consensus_receipt

from .conftest import RUNTIME_TARGET

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _load_example() -> ModuleType:
    path = ROOT / "examples" / "fraud_review_graph.py"
    spec = importlib.util.spec_from_file_location("fraud_review_graph", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client() -> Iterator[MacpClient]:
    c = MacpClient(
        target=RUNTIME_TARGET, allow_insecure=True, auth=AuthConfig.for_dev_agent("lg-initiator")
    )
    yield c
    c.close()


def _state(client: MacpClient, session_id: str, as_agent: str | None = None) -> str:
    auth = AuthConfig.for_dev_agent(as_agent) if as_agent else None
    state = client.get_session(session_id, auth=auth).metadata.state
    return envelope_pb2.SessionState.Name(state).removeprefix("SESSION_STATE_")


def _fixed(recommendation: str) -> Any:
    return lambda proposal, info: {"recommendation": recommendation, "confidence": 0.8}


def test_example_decision_approved_two_of_three(client: MacpClient) -> None:
    final = _load_example().run(RUNTIME_TARGET)
    result = final["macp"]
    assert result["outcome"] == "approve"
    assert result["mode"] == "macp.mode.decision.v1"
    assert (result["approvals"], result["threshold"]) == (2, 2)
    assert result["commitment"]["reason"] == "2/3 approvals (threshold 2)"
    assert final["action_taken"] == "released tx-1042"
    # Only the session's own members may read it: ask as the example's initiator.
    assert _state(client, result["session_id"], "review-coordinator") == "RESOLVED"
    receipt = consensus_receipt(result, issuer="did:web:review.example")
    assert verify_receipt(receipt, {}) == {
        "integrity": "match",
        "attestations": [],
        "verdict": "integrity_only",
    }


def test_quorum_rejected(client: MacpClient) -> None:
    participants = [
        ConsensusParticipant("q-a", _fixed("REJECT")),
        ConsensusParticipant("q-b", _fixed("BLOCK")),
        ConsensusParticipant("q-c", _fixed("APPROVE")),
    ]
    node = MacpConsensusNode(client, participants, mode="quorum")
    result = node.invoke({"proposal": {"option": "rotate prod keys", "details": {"env": "prod"}}})[
        "macp"
    ]
    assert result["outcome"] == "reject"
    assert result["mode"] == "macp.mode.quorum.v1"
    assert result["commitment"]["outcome_positive"] is False
    assert result["commitment"]["action"] == "macp.consensus.rejected"
    assert _state(client, result["session_id"]) == "RESOLVED"
    verdict = verify_receipt(consensus_receipt(result, issuer="did:web:ops.example"), {})
    assert verdict["verdict"] == "integrity_only"


def test_async_decision_with_review_abstain(client: MacpClient) -> None:
    participants = [
        ConsensusParticipant("d-a", _fixed("APPROVE")),
        ConsensusParticipant("d-b", _fixed("REVIEW")),
    ]
    node = MacpConsensusNode(client, participants, threshold=1)
    result = asyncio.run(node.ainvoke({"proposal": "merge release branch"}))["macp"]
    assert result["outcome"] == "approve"
    assert result["approvals"] == 1
    assert _state(client, result["session_id"]) == "RESOLVED"
