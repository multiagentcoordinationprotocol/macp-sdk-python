"""LangGraph integration for MACP: add a binding multi-agent decision to a graph."""

from __future__ import annotations

from importlib.metadata import version as _version

from .llm import DEFAULT_SYSTEM_PROMPT, UNPARSEABLE, llm_evaluator, parse_evaluation
from .node import (
    AUTHORITY_SCOPE,
    DECISION_PROPOSAL_ID,
    DEFAULT_ACTION,
    DEFAULT_TTL_MS,
    QUORUM_REQUEST_ID,
    ConsensusParticipant,
    ConsensusResult,
    MacpConsensusNode,
    consensus_receipt,
    normalize_recommendation,
)

__version__ = _version("langgraph-macp")

__all__ = [
    "AUTHORITY_SCOPE",
    "DECISION_PROPOSAL_ID",
    "DEFAULT_ACTION",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TTL_MS",
    "QUORUM_REQUEST_ID",
    "UNPARSEABLE",
    "ConsensusParticipant",
    "ConsensusResult",
    "MacpConsensusNode",
    "__version__",
    "consensus_receipt",
    "llm_evaluator",
    "normalize_recommendation",
    "parse_evaluation",
]
