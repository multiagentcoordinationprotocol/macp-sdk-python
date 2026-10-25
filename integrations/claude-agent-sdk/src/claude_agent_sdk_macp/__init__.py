"""Claude Agent SDK subagents as voters in a binding MACP decision."""

from importlib.metadata import version as _version

from .claude import (
    CONSENSUS_INPUT_SCHEMA,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    EVALUATION_SCHEMA,
    EVALUATOR_INSTRUCTIONS,
    UNPARSEABLE,
    ClaudeEvaluator,
    QueryFn,
    claude_evaluator,
    consensus_tool,
    macp_consensus,
    macp_consensus_server,
    parse_evaluation,
)
from .engine import (
    AUTHORITY_SCOPE,
    DECISION_PROPOSAL_ID,
    DEFAULT_ACTION,
    DEFAULT_TTL_MS,
    QUORUM_REQUEST_ID,
    ConsensusEngine,
    ConsensusParticipant,
    ConsensusResult,
    Evaluator,
    Mode,
    Outcome,
    Proposal,
    consensus_receipt,
    normalize_recommendation,
)

__version__ = _version("claude-agent-sdk-macp")

__all__ = [
    "AUTHORITY_SCOPE",
    "CONSENSUS_INPUT_SCHEMA",
    "DECISION_PROPOSAL_ID",
    "DEFAULT_ACTION",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "DEFAULT_TTL_MS",
    "EVALUATION_SCHEMA",
    "EVALUATOR_INSTRUCTIONS",
    "QUORUM_REQUEST_ID",
    "UNPARSEABLE",
    "ClaudeEvaluator",
    "ConsensusEngine",
    "ConsensusParticipant",
    "ConsensusResult",
    "Evaluator",
    "Mode",
    "Outcome",
    "Proposal",
    "QueryFn",
    "__version__",
    "claude_evaluator",
    "consensus_receipt",
    "consensus_tool",
    "macp_consensus",
    "macp_consensus_server",
    "normalize_recommendation",
    "parse_evaluation",
]
