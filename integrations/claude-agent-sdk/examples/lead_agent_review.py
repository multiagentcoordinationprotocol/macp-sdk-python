"""A Claude lead agent that puts its decision to a binding MACP vote.

The lead agent (``claude-opus-5-5``) gets one tool, ``mcp__macp__consensus``,
from ``macp_consensus_server``. When it calls the tool, the three reviewer
subagents from ``release_review.py`` evaluate and vote in a Decision session,
the outcome is committed, and the tool hands the Commitment back to the lead,
which reports it. ``on_result`` keeps a copy for the receipt.

Needs ``ANTHROPIC_API_KEY`` and a local runtime::

    docker run -d -p 50051:50051 -e MACP_ALLOW_INSECURE=1 \\
        ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0
    ANTHROPIC_API_KEY=... python examples/lead_agent_review.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from macp_sdk import AuthConfig, MacpClient
from release_review import AGENTS, PROPOSAL

from claude_agent_sdk_macp import ConsensusResult, consensus_receipt, macp_consensus_server

LEAD_PROMPT = (
    "You coordinate releases. Never decide a release yourself: put it to the reviewers with "
    "the consensus tool, passing the option, rationale and details you were given, then "
    "report the committed outcome and the session id in one sentence."
)


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("set ANTHROPIC_API_KEY to run this example")
    target = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")
    client = MacpClient(
        target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("release-coordinator")
    )
    results: list[ConsensusResult] = []
    server = macp_consensus_server(client, agents=AGENTS, on_result=results.append)
    options = ClaudeAgentOptions(
        model="claude-opus-5-5",
        system_prompt=LEAD_PROMPT,
        mcp_servers={"macp": server},
        tools=[],
        allowed_tools=["mcp__macp__consensus"],
        setting_sources=[],
        max_turns=4,
    )
    try:
        async for message in query(prompt=json.dumps(PROPOSAL), options=options):
            if isinstance(message, ResultMessage):
                print(f"lead agent:  {message.result}")
    finally:
        client.close()
    if not results:
        sys.exit("the lead agent did not call the consensus tool")
    result = results[-1]
    print(f"outcome:     {result['outcome']} ({result['commitment']['reason']})")
    print(f"commitment:  {result['commitment_hash']}")
    print(json.dumps(consensus_receipt(result, issuer="did:web:release.example"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
