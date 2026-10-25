"""Three Claude subagents settle a release through a binding MACP decision.

The security, reliability and product reviewers are ordinary Claude Agent SDK
``AgentDefinition`` objects, the same ones you would pass to
``ClaudeAgentOptions(agents=...)``. ``macp_consensus`` turns each into an MACP
participant, has them evaluate and vote in Decision mode, commits the outcome
and returns the Commitment.

By default the subagents' model calls are stubbed with canned answers, so the
example needs no API key and gives the same result every run (the integration
test runs it). Set ``ANTHROPIC_API_KEY`` and pass ``--live`` to have
``claude-opus-5-5`` do the reviewing. Run it against a local runtime::

    docker run -d -p 50051:50051 -e MACP_ALLOW_INSECURE=1 \\
        ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0
    python examples/release_review.py           # MACP_RUNTIME_TARGET to override
    ANTHROPIC_API_KEY=... python examples/release_review.py --live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage
from macp_sdk import AuthConfig, MacpClient

from claude_agent_sdk_macp import ConsensusResult, QueryFn, consensus_receipt, macp_consensus

AGENTS = {
    "security-reviewer": AgentDefinition(
        description="Reviews releases for security risk",
        prompt="You are a security reviewer. Block anything that weakens authentication.",
        model="claude-opus-5-5",
    ),
    "sre-reviewer": AgentDefinition(
        description="Reviews releases for operational risk",
        prompt="You are an SRE. Reject a rollout that has no rollback plan or canary stage.",
        model="claude-opus-5-5",
    ),
    "product-reviewer": AgentDefinition(
        description="Reviews releases for user impact",
        prompt="You are a product lead. Approve changes that users asked for.",
        model="claude-opus-5-5",
    ),
}

PROPOSAL = {
    "option": "ship checkout v2.3 to 100% of users",
    "rationale": "Fixes the double-charge bug; canary at 5% has been clean for 48h",
    "details": {"canary_error_rate": 0.0004, "rollback": "feature flag checkout_v23", "cves": 0},
}

#: What each stubbed reviewer answers, keyed by the first words of its prompt.
CANNED = {
    "You are a security reviewer.": {
        "recommendation": "APPROVE",
        "confidence": 0.86,
        "reason": "No auth or payment-data surface changes, and no new CVEs.",
    },
    "You are an SRE.": {
        "recommendation": "APPROVE",
        "confidence": 0.8,
        "reason": "Clean 48h canary and a flag-based rollback.",
    },
    "You are a product lead.": {
        "recommendation": "REVIEW",
        "confidence": 0.55,
        "reason": "Fix is wanted, but I would like the support team briefed first.",
    },
}


def stub_query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:  # noqa: ARG001
    """Stands in for ``claude_agent_sdk.query``: no model call, a canned verdict."""

    async def stream() -> AsyncIterator[Any]:
        system = str(options.system_prompt)
        answer = next(a for start, a in CANNED.items() if system.startswith(start))
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


async def review(target: str, *, live: bool = False) -> ConsensusResult:
    client = MacpClient(
        target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("release-coordinator")
    )
    query: QueryFn | None = None if live else stub_query
    try:
        return await macp_consensus(client, PROPOSAL, agents=AGENTS, query=query)
    finally:
        client.close()


def main() -> None:
    live = "--live" in sys.argv[1:]
    if live and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("set ANTHROPIC_API_KEY to run with --live")
    target = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")
    result = asyncio.run(review(target, live=live))
    print(f"outcome:     {result['outcome']} ({result['commitment']['reason']})")
    print(f"session:     {result['session_id']}")
    print(f"commitment:  {result['commitment_hash']}")
    receipt = consensus_receipt(result, issuer="did:web:release.example")
    print("receipt:")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
