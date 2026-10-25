"""A LangGraph fraud review that ends in a binding MACP decision.

The graph mirrors macp-playground's LangGraph worker
(``agents/langgraph_worker/graph.py``). It scores device trust and chargeback
history, then hands the transaction to three specialist agents: fraud, growth
and compliance. They settle it through ``MacpConsensusNode``, and the graph
routes on the committed outcome.

The specialists' LLMs are LangChain ``FakeListChatModel`` objects with canned
JSON answers, so the example needs no API key and gives the same result every
run (``real_llm_review.py`` uses a real model). Run it against a local
runtime::

    docker run -d -p 50051:50051 -e MACP_ALLOW_INSECURE=1 \\
        ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0
    python examples/fraud_review_graph.py            # MACP_RUNTIME_TARGET to override
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.graph import END, START, StateGraph
from macp_sdk import AuthConfig, MacpClient

from langgraph_macp import (
    ConsensusParticipant,
    ConsensusResult,
    MacpConsensusNode,
    consensus_receipt,
    llm_evaluator,
)


class ReviewState(TypedDict, total=False):
    transaction_id: str
    transaction_amount: float
    device_trust_score: float
    prior_chargebacks: int
    signals: list[str]
    proposal: dict[str, Any]
    macp: ConsensusResult  # written by MacpConsensusNode; must be declared here
    action_taken: str


def score_signals(state: ReviewState) -> dict[str, Any]:
    signals = []
    if state["device_trust_score"] < 0.2:
        signals.append("low_device_trust")
    if state["prior_chargebacks"] >= 1:
        signals.append("chargeback_history")
    return {"signals": signals}


def build_proposal(state: ReviewState) -> dict[str, Any]:
    return {
        "proposal": {
            "option": f"approve transaction {state['transaction_id']}",
            "rationale": f"signals: {', '.join(state['signals']) or 'none'}",
            "details": {
                "amount": state["transaction_amount"],
                "signals": state["signals"],
            },
        }
    }


def release(state: ReviewState) -> dict[str, Any]:
    return {"action_taken": f"released {state['transaction_id']}"}


def hold(state: ReviewState) -> dict[str, Any]:
    return {"action_taken": f"held {state['transaction_id']} for manual review"}


def canned_model(recommendation: str, confidence: float, reason: str) -> FakeListChatModel:
    answer = {"recommendation": recommendation, "confidence": confidence, "reason": reason}
    return FakeListChatModel(responses=[f"```json\n{json.dumps(answer)}\n```"])


def specialists() -> list[ConsensusParticipant]:
    return [
        ConsensusParticipant(
            "fraud-agent",
            llm_evaluator(canned_model("BLOCK", 0.82, "low device trust plus a prior chargeback")),
        ),
        ConsensusParticipant(
            "growth-agent",
            llm_evaluator(canned_model("APPROVE", 0.74, "long-standing customer, low amount")),
        ),
        ConsensusParticipant(
            "compliance-agent",
            llm_evaluator(canned_model("APPROVE", 0.9, "no sanctions or KYC flags")),
        ),
    ]


def build_graph(client: MacpClient) -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("score_signals", score_signals)
    graph.add_node("build_proposal", build_proposal)
    graph.add_node("consensus", MacpConsensusNode(client, specialists(), mode="decision"))
    graph.add_node("release", release)
    graph.add_node("hold", hold)
    graph.add_edge(START, "score_signals")
    graph.add_edge("score_signals", "build_proposal")
    graph.add_edge("build_proposal", "consensus")
    graph.add_conditional_edges(
        "consensus",
        lambda state: "release" if state["macp"]["outcome"] == "approve" else "hold",
    )
    graph.add_edge("release", END)
    graph.add_edge("hold", END)
    return graph.compile()


TRANSACTION: ReviewState = {
    "transaction_id": "tx-1042",
    "transaction_amount": 129.0,
    "device_trust_score": 0.15,
    "prior_chargebacks": 1,
}


def run(target: str) -> ReviewState:
    client = MacpClient(
        target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("review-coordinator")
    )
    try:
        final: ReviewState = build_graph(client).invoke(TRANSACTION)
    finally:
        client.close()
    return final


def main() -> None:
    final = run(os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"))
    result = final["macp"]
    print(f"signals:     {final['signals']}")
    print(f"outcome:     {result['outcome']} ({result['commitment']['reason']})")
    print(f"session:     {result['session_id']}")
    print(f"commitment:  {result['commitment_hash']}")
    print(f"action:      {final['action_taken']}")
    receipt = consensus_receipt(result, issuer="did:web:review.example")
    print("receipt:")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
