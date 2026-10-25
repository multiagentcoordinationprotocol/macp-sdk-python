"""Claude Agent SDK subagents as MACP evaluators, and the ``consensus`` helpers.

``claude_evaluator(agent)`` turns an ``AgentDefinition`` into an SDK
``EvaluationStrategy``: each evaluation is one ``query()`` run with the
subagent's prompt, tools and model, answering through a JSON schema.
``macp_consensus`` settles one session with such voters, and
``macp_consensus_server`` exposes that as an in-process MCP tool a lead agent
calls as ``mcp__macp__consensus``.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from importlib.metadata import version as _version
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    ResultMessage,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk import query as sdk_query
from macp_sdk import MacpClient, MacpSdkError
from macp_sdk.agent import EvaluationResult, SessionInfo

from .engine import (
    DEFAULT_ACTION,
    DEFAULT_TTL_MS,
    ConsensusEngine,
    ConsensusParticipant,
    ConsensusResult,
    Mode,
    Proposal,
    normalize_recommendation,
)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TURNS = 4
UNPARSEABLE = "unparseable model output"

EVALUATOR_INSTRUCTIONS = (
    "You are one participant in a multi-agent decision. The user message is the proposal, "
    "as JSON. Evaluate it from your role and answer with your recommendation (APPROVE, "
    "REVIEW, BLOCK or REJECT), your confidence between 0 and 1, and a one-sentence reason."
)

EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": ["APPROVE", "REVIEW", "BLOCK", "REJECT"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["recommendation", "confidence", "reason"],
    "additionalProperties": False,
}

#: ``claude_agent_sdk.query``'s shape, so tests can inject a stub.
QueryFn = Callable[..., AsyncIterator[Any]]

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)


def parse_evaluation(text: str) -> EvaluationResult:
    """Parse a model's JSON answer. Any defect gives ``REVIEW`` with confidence 0."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        recommendation = normalize_recommendation(data.get("recommendation"))
        confidence = data.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence out of range")
        reason = data.get("reason", "")
        return EvaluationResult(
            recommendation=recommendation,
            confidence=float(confidence),
            reason=reason if isinstance(reason, str) else json.dumps(reason),
        )
    except ValueError:
        return EvaluationResult(recommendation="REVIEW", confidence=0.0, reason=UNPARSEABLE)


def _read_result(message: ResultMessage) -> EvaluationResult:
    if message.subtype != "success":
        raise MacpSdkError(f"claude evaluator {message.subtype}: {'; '.join(message.errors or [])}")
    if message.is_error:
        raise MacpSdkError(f"claude evaluator error: {message.result or ''}")
    structured = message.structured_output
    if structured is not None:
        return parse_evaluation(
            structured if isinstance(structured, str) else json.dumps(structured)
        )
    return parse_evaluation(message.result or "")


class ClaudeEvaluator:
    """An ``EvaluationStrategy`` backed by one Claude Agent SDK ``query()`` per call.

    ``evaluate`` blocks: it runs the query on a fresh event loop, so call it
    from a thread with no running loop (``macp_consensus`` uses a worker thread).
    """

    __slots__ = ("_options", "_query")

    def __init__(self, options: ClaudeAgentOptions, query: QueryFn) -> None:
        self._options = options
        self._query = query

    @property
    def options(self) -> ClaudeAgentOptions:
        return self._options

    def evaluate(self, proposal: dict[str, Any], context: SessionInfo) -> EvaluationResult:  # noqa: ARG002
        return asyncio.run(self._evaluate(proposal))

    async def _evaluate(self, proposal: dict[str, Any]) -> EvaluationResult:
        result: ResultMessage | None = None
        async for message in self._query(
            prompt=json.dumps(proposal, default=str), options=self._options
        ):
            if isinstance(message, ResultMessage):
                result = message
        if result is None:
            raise MacpSdkError("claude evaluator: the query ended without a result")
        return _read_result(result)


def claude_evaluator(
    agent: AgentDefinition,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    options: Mapping[str, Any] | None = None,
    query: QueryFn | None = None,
) -> ClaudeEvaluator:
    """Turn a subagent definition into an MACP ``EvaluationStrategy``.

    Each evaluation is one ``query()`` with the proposal as the prompt and:

    - ``system_prompt``: ``agent.prompt`` plus ``EVALUATOR_INSTRUCTIONS``
    - ``model``: ``agent.model`` (``"inherit"`` counts as unset), else
      ``model``, else ``claude-opus-5-5``
    - ``max_turns``: ``agent.maxTurns``, else ``max_turns``, else 4
    - ``tools``: ``agent.tools``, else none; ``disallowedTools`` and ``effort``
      pass through
    - ``output_format``: ``EVALUATION_SCHEMA``; no setting sources and no
      session persistence

    ``options`` (``ClaudeAgentOptions`` fields) is applied last and wins.
    An answer that cannot be read becomes ``REVIEW`` with confidence 0. A
    failed run (an error result, or no result at all) raises ``MacpSdkError``.
    ``query`` replaces ``claude_agent_sdk.query``, for tests.
    """
    agent_model = agent.model if agent.model and agent.model != "inherit" else None
    kwargs: dict[str, Any] = {
        "system_prompt": f"{agent.prompt}\n\n{EVALUATOR_INSTRUCTIONS}",
        "model": agent_model or model or DEFAULT_MODEL,
        "max_turns": agent.maxTurns or max_turns or DEFAULT_MAX_TURNS,
        "tools": list(agent.tools or []),
        "output_format": {"type": "json_schema", "schema": EVALUATION_SCHEMA},
        "setting_sources": [],
        "extra_args": {"no-session-persistence": None},
    }
    if agent.disallowedTools:
        kwargs["disallowed_tools"] = list(agent.disallowedTools)
    if agent.effort is not None:
        kwargs["effort"] = agent.effort
    kwargs.update(options or {})
    return ClaudeEvaluator(ClaudeAgentOptions(**kwargs), query or sdk_query)


def _engine(
    client: MacpClient,
    agents: Mapping[str, AgentDefinition] | None,
    participants: list[ConsensusParticipant] | None,
    query: QueryFn | None,
    **settings: Any,
) -> ConsensusEngine:
    if (agents is None) == (participants is None):
        raise TypeError("pass exactly one of agents= or participants=")
    voters = participants or [
        ConsensusParticipant(agent_id, claude_evaluator(agent, query=query))
        for agent_id, agent in (agents or {}).items()
    ]
    return ConsensusEngine(client, voters, **settings)


async def macp_consensus(
    client: MacpClient,
    proposal: Proposal,
    *,
    agents: Mapping[str, AgentDefinition] | None = None,
    participants: list[ConsensusParticipant] | None = None,
    mode: Mode = "decision",
    policy: str | None = None,
    threshold: int | None = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    action: str = DEFAULT_ACTION,
    initiator: str | None = None,
    query: QueryFn | None = None,
) -> ConsensusResult:
    """Put ``proposal`` to a binding MACP vote and return the Commitment.

    ``agents`` maps each participant id to its ``AgentDefinition`` (the same
    dict you give ``ClaudeAgentOptions(agents=...)``); they evaluate and vote
    in insertion order, each through ``claude_evaluator``. Pass
    ``participants`` instead to choose evaluators, auth or voting strategies
    yourself. The client is the initiator: it opens, proposes and commits, and
    never votes. The session runs in a worker thread.
    """
    engine = _engine(
        client,
        agents,
        participants,
        query,
        mode=mode,
        policy=policy,
        threshold=threshold,
        ttl_ms=ttl_ms,
        action=action,
        initiator=initiator,
    )
    return await asyncio.to_thread(engine.run, proposal)


CONSENSUS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "option": {"type": "string", "minLength": 1, "description": "What is being decided"},
        "rationale": {"type": "string", "description": "Why it is proposed"},
        "details": {"type": "object", "description": "Supporting data for the voters"},
    },
    "required": ["option"],
}


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["is_error"] = True
    return result


def consensus_tool(
    client: MacpClient,
    *,
    agents: Mapping[str, AgentDefinition] | None = None,
    participants: list[ConsensusParticipant] | None = None,
    mode: Mode = "decision",
    policy: str | None = None,
    threshold: int | None = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    action: str = DEFAULT_ACTION,
    initiator: str | None = None,
    on_result: Callable[[ConsensusResult], Awaitable[None] | None] | None = None,
    query: QueryFn | None = None,
) -> SdkMcpTool[Any]:
    """The ``consensus`` tool on its own, to mount in your own SDK MCP server.

    Takes the same arguments as ``macp_consensus_server``. Settings are
    checked here, once.
    """
    engine = _engine(
        client,
        agents,
        participants,
        query,
        mode=mode,
        policy=policy,
        threshold=threshold,
        ttl_ms=ttl_ms,
        action=action,
        initiator=initiator,
    )

    @tool(
        "consensus",
        "Put a decision to a binding MACP vote among the configured subagents",
        CONSENSUS_INPUT_SCHEMA,
    )
    async def consensus(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(engine.run, args)
        except Exception as exc:
            return _text(f"consensus failed: {exc}", error=True)
        if on_result is not None:
            try:
                pending = on_result(result)
                if pending is not None:
                    await pending
            except Exception as exc:
                return _text(
                    f"consensus committed {result['outcome']} (session {result['session_id']}) "
                    f"but on_result failed: {exc}",
                    error=True,
                )
        return _text(json.dumps(result))

    return consensus


def macp_consensus_server(
    client: MacpClient,
    *,
    agents: Mapping[str, AgentDefinition] | None = None,
    participants: list[ConsensusParticipant] | None = None,
    mode: Mode = "decision",
    policy: str | None = None,
    threshold: int | None = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    action: str = DEFAULT_ACTION,
    initiator: str | None = None,
    on_result: Callable[[ConsensusResult], Awaitable[None] | None] | None = None,
    query: QueryFn | None = None,
) -> McpSdkServerConfig:
    """An in-process MCP server ``macp`` with one tool, ``consensus``.

    Register it with ``ClaudeAgentOptions(mcp_servers={"macp": server},
    allowed_tools=["mcp__macp__consensus"])``. The lead agent calls the tool
    with ``{option, rationale?, details?}``; the configured voters settle it
    and the tool returns the ``ConsensusResult`` as JSON. A failure comes back
    as an error result, ``consensus failed: <message>``. ``on_result`` sees
    every committed result (sync or async); if it raises, the tool reports
    the committed outcome alongside the error.
    """
    consensus = consensus_tool(
        client,
        agents=agents,
        participants=participants,
        mode=mode,
        policy=policy,
        threshold=threshold,
        ttl_ms=ttl_ms,
        action=action,
        initiator=initiator,
        on_result=on_result,
        query=query,
    )
    return create_sdk_mcp_server(
        name="macp", version=_version("claude-agent-sdk-macp"), tools=[consensus]
    )
