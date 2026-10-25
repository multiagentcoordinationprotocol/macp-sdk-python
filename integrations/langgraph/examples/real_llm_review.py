"""The fraud review graph with real Claude models as the specialists.

Same graph as ``fraud_review_graph.py``, but each specialist's evaluator is
``llm_evaluator(ChatAnthropic(...))`` with its own system prompt, so the
outcome is whatever the models decide. Needs ``pip install
langchain-anthropic`` and ``ANTHROPIC_API_KEY`` in the environment::

    ANTHROPIC_API_KEY=... python examples/real_llm_review.py
"""

from __future__ import annotations

import json
import os
import sys

import fraud_review_graph as base
from langchain_anthropic import ChatAnthropic
from macp_sdk import AuthConfig, MacpClient

from langgraph_macp import (
    DEFAULT_SYSTEM_PROMPT,
    ConsensusParticipant,
    consensus_receipt,
    llm_evaluator,
)

MODEL = "claude-opus-5-5"

ROLES = {
    "fraud-agent": "You are a fraud analyst. Weigh device trust and chargeback signals heavily.",
    "growth-agent": "You are a growth lead. Weigh customer lifetime value and false-decline cost.",
    "compliance-agent": "You are a compliance officer. Block only on regulatory risk.",
}


def specialists() -> list[ConsensusParticipant]:
    model = ChatAnthropic(model=MODEL, max_tokens=400)  # type: ignore[call-arg]
    return [
        ConsensusParticipant(
            agent, llm_evaluator(model, system_prompt=f"{role}\n\n{DEFAULT_SYSTEM_PROMPT}")
        )
        for agent, role in ROLES.items()
    ]


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("set ANTHROPIC_API_KEY to run this example")
    base.specialists = specialists  # same graph, real models
    target = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")
    client = MacpClient(
        target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("review-coordinator")
    )
    try:
        final = base.build_graph(client).invoke(base.TRANSACTION)
    finally:
        client.close()
    result = final["macp"]
    print(f"outcome: {result['outcome']}  ({result['commitment']['reason']})")
    print(f"action:  {final['action_taken']}")
    print(json.dumps(consensus_receipt(result, issuer="did:web:review.example"), indent=2))


if __name__ == "__main__":
    main()
