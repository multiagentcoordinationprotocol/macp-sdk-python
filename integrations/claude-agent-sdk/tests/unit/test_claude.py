from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import mcp
import pytest
from claude_agent_sdk import AgentDefinition, AssistantMessage, ClaudeAgentOptions, ResultMessage
from macp_sdk import MacpSdkError
from macp_sdk.agent import EvaluationResult, SessionInfo

from claude_agent_sdk_macp import (
    CONSENSUS_INPUT_SCHEMA,
    EVALUATION_SCHEMA,
    EVALUATOR_INSTRUCTIONS,
    UNPARSEABLE,
    ConsensusParticipant,
    claude_evaluator,
    consensus_tool,
    macp_consensus,
    macp_consensus_server,
    parse_evaluation,
)

from .conftest import FakeClient

UNREADABLE = EvaluationResult("REVIEW", 0.0, UNPARSEABLE)
INFO = SessionInfo("sid", "macp.mode.decision.v1")
PROPOSAL = {
    "proposal_id": "p1",
    "option": "ship",
    "rationale": "tests pass",
    "details": {"n": 1},
    "mode": "macp.mode.decision.v1",
}


def result_message(
    *,
    structured: Any = None,
    text: str | None = None,
    subtype: str = "success",
    is_error: bool = False,
    errors: list[str] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="claude-session",
        result=text,
        structured_output=structured,
        errors=errors,
    )


class StubQuery:
    """Stands in for ``claude_agent_sdk.query``: records calls, yields canned messages."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return self._stream(answer)

    @staticmethod
    async def _stream(answer: Any) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[], model="claude-opus-5-5")
        if isinstance(answer, ResultMessage):
            yield answer
        elif answer is not None:
            yield result_message(structured=answer)


def agent(**kwargs: Any) -> AgentDefinition:
    return AgentDefinition(description="reviewer", prompt="You review security.", **kwargs)


APPROVE = {"recommendation": "APPROVE", "confidence": 0.9, "reason": "safe"}
REJECT = {"recommendation": "REJECT", "confidence": 0.8, "reason": "risky"}


# ── parse_evaluation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"recommendation": "approve", "confidence": 0.8, "reason": "fine"}',
            EvaluationResult("APPROVE", 0.8, "fine"),
        ),
        (
            '```json\n{"recommendation": "BLOCK", "confidence": 1, "reason": "fraud"}\n```',
            EvaluationResult("BLOCK", 1.0, "fraud"),
        ),
        (
            '{"recommendation": "ABSTAIN", "confidence": 0.5, "reason": ["a", 1]}',
            EvaluationResult("REVIEW", 0.5, '["a", 1]'),
        ),
        ('{"recommendation": "reject", "confidence": 0}', EvaluationResult("REJECT", 0.0, "")),
        ("not json", UNREADABLE),
        ("[1, 2]", UNREADABLE),
        ('{"recommendation": "YES", "confidence": 0.5}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": 2}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": true}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": NaN}', UNREADABLE),
        ('{"recommendation": "APPROVE"}', UNREADABLE),
    ],
)
def test_parse_evaluation(text: str, expected: EvaluationResult) -> None:
    assert parse_evaluation(text) == expected


# ── claude_evaluator ─────────────────────────────────────────────────


def test_evaluator_options_from_agent_definition() -> None:
    stub = StubQuery(APPROVE)
    evaluator = claude_evaluator(
        agent(
            tools=["Read", "Grep"],
            disallowedTools=["Bash"],
            model="claude-sonnet-5-5",
            maxTurns=2,
            effort="high",
        ),
        model="ignored",
        max_turns=9,
        query=stub,
    )
    assert evaluator.evaluate(PROPOSAL, INFO) == EvaluationResult("APPROVE", 0.9, "safe")
    prompt, options = stub.calls[0]
    assert json.loads(prompt) == PROPOSAL
    assert options.system_prompt == f"You review security.\n\n{EVALUATOR_INSTRUCTIONS}"
    assert options.model == "claude-sonnet-5-5"
    assert options.max_turns == 2
    assert options.tools == ["Read", "Grep"]
    assert options.disallowed_tools == ["Bash"]
    assert options.effort == "high"
    assert options.output_format == {"type": "json_schema", "schema": EVALUATION_SCHEMA}
    assert options.setting_sources == []
    assert options.extra_args == {"no-session-persistence": None}


def test_evaluator_defaults_and_inherit() -> None:
    stub = StubQuery(APPROVE)
    claude_evaluator(agent(model="inherit"), query=stub).evaluate(PROPOSAL, INFO)
    options = stub.calls[0][1]
    assert options.model == "claude-opus-5-5"
    assert options.max_turns == 4
    assert options.tools == []
    assert options.disallowed_tools == []
    assert options.effort is None


def test_evaluator_fallback_model_and_turns() -> None:
    stub = StubQuery(APPROVE)
    claude_evaluator(agent(), model="claude-haiku-5-5", max_turns=1, query=stub).evaluate(
        PROPOSAL, INFO
    )
    assert (stub.calls[0][1].model, stub.calls[0][1].max_turns) == ("claude-haiku-5-5", 1)


def test_options_override_everything() -> None:
    stub = StubQuery(APPROVE)
    evaluator = claude_evaluator(
        agent(model="claude-sonnet-5-5"),
        options={"model": "claude-opus-5-5", "system_prompt": "custom", "cwd": "/tmp"},
        query=stub,
    )
    assert evaluator.options.model == "claude-opus-5-5"
    assert evaluator.options.system_prompt == "custom"
    assert str(evaluator.options.cwd) == "/tmp"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {"recommendation": "block", "confidence": 1, "reason": "x"},
            EvaluationResult("BLOCK", 1.0, "x"),
        ),
        (
            '{"recommendation": "REJECT", "confidence": 0.2, "reason": "s"}',
            EvaluationResult("REJECT", 0.2, "s"),
        ),
        ({"recommendation": "MAYBE", "confidence": 1}, UNREADABLE),
        (
            result_message(text='```json\n{"recommendation":"APPROVE","confidence":0.5}\n```'),
            EvaluationResult("APPROVE", 0.5, ""),
        ),
        (result_message(text="I approve!"), UNREADABLE),
        (result_message(), UNREADABLE),
    ],
)
def test_evaluator_reads_structured_output_then_text(
    answer: Any, expected: EvaluationResult
) -> None:
    result = claude_evaluator(agent(), query=StubQuery(answer)).evaluate(PROPOSAL, INFO)
    assert result == expected


@pytest.mark.parametrize(
    ("message", "match"),
    [
        (
            result_message(subtype="error_max_turns", is_error=True, errors=["a", "b"]),
            "claude evaluator error_max_turns: a; b",
        ),
        (result_message(subtype="error_during_execution", is_error=True), "error_during_execution"),
        (result_message(is_error=True, text="API Error: 529"), "claude evaluator error: API Error"),
        (None, "without a result"),
    ],
)
def test_evaluator_failures_raise(message: Any, match: str) -> None:
    evaluator = claude_evaluator(agent(), query=StubQuery(message))
    with pytest.raises(MacpSdkError, match=match):
        evaluator.evaluate(PROPOSAL, INFO)


# ── macp_consensus ───────────────────────────────────────────────────


def test_macp_consensus_with_agents(client: FakeClient) -> None:
    stub = StubQuery(APPROVE, REJECT, APPROVE)
    agents = {
        "security": agent(),
        "sre": AgentDefinition(description="sre", prompt="You review reliability."),
        "product": AgentDefinition(description="pm", prompt="You review user impact."),
    }
    result = asyncio.run(macp_consensus(client, "ship v2", agents=agents, query=stub))
    assert [s for t, s in client.types() if t == "Vote"] == ["security", "sre", "product"]
    assert [c[1].system_prompt.split("\n")[0] for c in stub.calls] == [  # type: ignore[union-attr]
        "You review security.",
        "You review reliability.",
        "You review user impact.",
    ]
    assert json.loads(stub.calls[0][0])["option"] == "ship v2"
    assert result["outcome"] == "approve"
    assert result["commitment"]["reason"] == "2/3 approvals (threshold 2)"


def test_macp_consensus_quorum_with_participants(client: FakeClient) -> None:
    participants = [
        ConsensusParticipant("a", lambda p, i: APPROVE),
        ConsensusParticipant("b", lambda p, i: APPROVE),
    ]
    result = asyncio.run(
        macp_consensus(
            client,
            {"option": "rotate keys", "rationale": "quarterly"},
            participants=participants,
            mode="quorum",
            threshold=1,
            action="keys",
        )
    )
    assert result["mode"] == "macp.mode.quorum.v1"
    assert client.payload("Commitment").action == "keys.approved"
    assert [t for t, _ in client.types()].count("Approve") == 1  # settled at 1/1


@pytest.mark.parametrize("both", [True, False])
def test_agents_xor_participants(client: FakeClient, both: bool) -> None:
    kwargs: dict[str, Any] = (
        {"agents": {"a": agent()}, "participants": [ConsensusParticipant("b", lambda p, i: {})]}
        if both
        else {}
    )
    with pytest.raises(TypeError, match="exactly one"):
        asyncio.run(macp_consensus(client, "x", **kwargs))
    with pytest.raises(TypeError, match="exactly one"):
        macp_consensus_server(client, **kwargs)


def test_macp_consensus_failure_cancels() -> None:
    client = FakeClient()
    stub = StubQuery(result_message(subtype="error_max_turns", is_error=True))
    with pytest.raises(MacpSdkError):
        asyncio.run(macp_consensus(client, "x", agents={"a": agent()}, query=stub))
    assert client.cancelled == ["macp_consensus failed"]


# ── macp_consensus_server ────────────────────────────────────────────


def call_tool(client: FakeClient, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("agents", {"a": agent()})
    kwargs.setdefault("query", StubQuery(APPROVE))
    return asyncio.run(consensus_tool(client, **kwargs).handler(args))


def test_tool_shape(client: FakeClient) -> None:
    consensus = consensus_tool(client, agents={"a": agent()})
    assert consensus.name == "consensus"
    assert consensus.description == (
        "Put a decision to a binding MACP vote among the configured subagents"
    )
    assert consensus.input_schema == CONSENSUS_INPUT_SCHEMA


def test_server_shape(client: FakeClient) -> None:
    server = macp_consensus_server(client, agents={"a": agent()})
    assert (server["type"], server["name"]) == ("sdk", "macp")


@pytest.mark.skipif(not hasattr(mcp, "Client"), reason="in-process mcp.Client needs mcp>=2")
def test_server_round_trip_over_mcp(client: FakeClient) -> None:
    server = macp_consensus_server(client, agents={"a": agent()}, query=StubQuery(APPROVE))

    async def go() -> tuple[Any, Any, Any]:
        async with mcp.Client(server["instance"]) as session:
            listed = await session.list_tools()
            ok = await session.call_tool("consensus", {"option": "ship"})
            bad = await session.call_tool("consensus", {"option": ""})
            return listed, ok, bad

    listed, ok, bad = asyncio.run(go())
    assert [t.name for t in listed.tools] == ["consensus"]
    assert ok.is_error is False
    assert json.loads(ok.content[0].text)["outcome"] == "approve"
    assert bad.is_error is True  # the SDK checks minLength before the handler runs
    assert [t for t, _ in client.types()].count("SessionStart") == 1


def test_server_settles_and_reports_result(client: FakeClient) -> None:
    seen: list[Any] = []
    out = call_tool(
        client,
        {"option": "ship", "rationale": "ready", "details": {"k": 1}},
        agents={"a": agent(), "b": agent()},
        on_result=seen.append,
    )
    assert "is_error" not in out
    result = json.loads(out["content"][0]["text"])
    assert result["outcome"] == "approve"
    assert seen == [result]
    assert json.loads(client.payload("Proposal").supporting_data) == {"k": 1}


def test_server_async_on_result(client: FakeClient) -> None:
    seen: list[Any] = []

    async def record(result: Any) -> None:
        seen.append(result["session_id"])

    out = call_tool(client, {"option": "ship"}, on_result=record)
    assert seen == [json.loads(out["content"][0]["text"])["session_id"]]


def test_server_reports_failure(client: FakeClient) -> None:
    out = call_tool(
        client, {"option": "ship"}, query=StubQuery(result_message(is_error=True, text="down"))
    )
    assert out["is_error"] is True
    assert out["content"][0]["text"] == "consensus failed: claude evaluator error: down"
    assert client.cancelled == ["macp_consensus failed"]


def test_server_rejects_bad_input(client: FakeClient) -> None:
    out = call_tool(client, {"option": ""})
    assert out == {
        "content": [
            {"type": "text", "text": "consensus failed: proposal option must be a non-empty string"}
        ],
        "is_error": True,
    }
    assert client.sent == []


def test_server_on_result_failure_after_commit(client: FakeClient) -> None:
    def explode(result: Any) -> None:
        raise RuntimeError("webhook down")

    out = call_tool(client, {"option": "ship"}, query=StubQuery(REJECT), on_result=explode)
    assert out["is_error"] is True
    text = out["content"][0]["text"]
    assert text.startswith("consensus committed reject (session ")
    assert text.endswith(") but on_result failed: webhook down")
    assert client.payload("Commitment").action == "macp.consensus.rejected"


def test_server_validates_settings_up_front(client: FakeClient) -> None:
    with pytest.raises(ValueError, match="threshold"):
        macp_consensus_server(client, agents={"a": agent()}, threshold=2)
