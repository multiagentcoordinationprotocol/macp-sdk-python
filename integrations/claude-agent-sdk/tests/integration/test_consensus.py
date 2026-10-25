"""The Claude Agent SDK adapter against a real runtime (CI pins macp-runtime 0.9.0).

The subagents' model calls are stubbed (no API key), but every MACP envelope
goes to the runtime. The Decision case runs ``examples/release_review.py``
end to end. Every session must end ``RESOLVED``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage
from macp.v1 import envelope_pb2
from macp_sdk import AuthConfig, MacpClient, verify_receipt

from claude_agent_sdk_macp import (
    ConsensusParticipant,
    consensus_receipt,
    consensus_tool,
    macp_consensus,
)

from .conftest import RUNTIME_TARGET

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _load_example() -> ModuleType:
    path = ROOT / "examples" / "release_review.py"
    spec = importlib.util.spec_from_file_location("release_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client() -> Iterator[MacpClient]:
    c = MacpClient(
        target=RUNTIME_TARGET, allow_insecure=True, auth=AuthConfig.for_dev_agent("cas-initiator")
    )
    yield c
    c.close()


def _state(client: MacpClient, session_id: str, as_agent: str | None = None) -> str:
    auth = AuthConfig.for_dev_agent(as_agent) if as_agent else None
    state = client.get_session(session_id, auth=auth).metadata.state
    return envelope_pb2.SessionState.Name(state).removeprefix("SESSION_STATE_")


def _answers(*recommendations: str) -> Any:
    """A stub ``query`` answering with each recommendation in turn."""
    pending = list(recommendations)

    def query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        answer = {"recommendation": pending.pop(0), "confidence": 0.8, "reason": "stub"}

        async def stream() -> AsyncIterator[Any]:
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id="stub",
                structured_output=answer,
            )

        return stream()

    return query


def _agent(role: str) -> AgentDefinition:
    return AgentDefinition(description=role, prompt=f"You are the {role}.")


def test_example_decision_approved_two_of_three(client: MacpClient) -> None:
    result = asyncio.run(_load_example().review(RUNTIME_TARGET))
    assert result["outcome"] == "approve"
    assert result["mode"] == "macp.mode.decision.v1"
    assert (result["approvals"], result["threshold"]) == (2, 2)
    assert result["commitment"]["reason"] == "2/3 approvals (threshold 2)"
    # Only the session's own members may read it: ask as the example's initiator.
    assert _state(client, result["session_id"], "release-coordinator") == "RESOLVED"
    receipt = consensus_receipt(result, issuer="did:web:release.example")
    assert verify_receipt(receipt, {}) == {
        "integrity": "match",
        "attestations": [],
        "verdict": "integrity_only",
    }


def test_quorum_rejected(client: MacpClient) -> None:
    agents = {"q-a": _agent("auditor"), "q-b": _agent("operator"), "q-c": _agent("owner")}
    result = asyncio.run(
        macp_consensus(
            client,
            {"option": "rotate prod keys", "details": {"env": "prod"}},
            agents=agents,
            mode="quorum",
            query=_answers("REJECT", "BLOCK", "APPROVE"),
        )
    )
    assert result["outcome"] == "reject"
    assert result["mode"] == "macp.mode.quorum.v1"
    assert result["approvals"] == 0
    assert result["commitment"]["action"] == "macp.consensus.rejected"
    assert _state(client, result["session_id"]) == "RESOLVED"


def test_consensus_tool_with_review_abstain(client: MacpClient) -> None:
    consensus = consensus_tool(
        client,
        agents={"t-a": _agent("reviewer"), "t-b": _agent("approver")},
        threshold=1,
        query=_answers("REVIEW", "APPROVE"),
    )
    out = asyncio.run(consensus.handler({"option": "merge release branch"}))
    assert "is_error" not in out
    result = json.loads(out["content"][0]["text"])
    assert result["outcome"] == "approve"
    assert result["approvals"] == 1
    assert _state(client, result["session_id"]) == "RESOLVED"


def test_failing_evaluator_cancels_the_session(client: MacpClient) -> None:
    session_ids: list[str] = []

    def explode(proposal: dict[str, Any], info: Any) -> Any:
        session_ids.append(info.session_id)
        raise RuntimeError("evaluator down")

    with pytest.raises(RuntimeError, match="evaluator down"):
        asyncio.run(
            macp_consensus(client, "x", participants=[ConsensusParticipant("f-a", explode)])
        )
    assert _state(client, session_ids[0]) == "CANCELLED"
