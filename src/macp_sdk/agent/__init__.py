import warnings
from typing import Any

from .cancel_callback import CancelCallbackServer, start_cancel_callback_server
from .dispatcher import Dispatcher
from .participant import InitiatorConfig, Participant, ParticipantActions
from .runner import from_bootstrap
from .strategies import (
    CommitmentResult,
    CommitmentStrategy,
    EvaluationResult,
    EvaluationStrategy,
    VoteResult,
    VotingStrategy,
    commitment_handler,
    evaluation_handler,
    function_committer,
    function_evaluator,
    function_voter,
    majority_committer,
    majority_voter,
    voting_handler,
)
from .transports import (
    GrpcTransportAdapter,
    HttpTransportAdapter,
    TransportAdapter,
)
from .types import (
    HandlerContext,
    IncomingMessage,
    MessageHandler,
    PhaseChangeHandler,
    SessionInfo,
    TerminalHandler,
    TerminalResult,
)

__all__ = [
    "CancelCallbackServer",
    "CommitmentResult",
    "CommitmentStrategy",
    "Dispatcher",
    "EvaluationResult",
    "EvaluationStrategy",
    "GrpcTransportAdapter",
    "HandlerContext",
    "HttpTransportAdapter",
    "IncomingMessage",
    "InitiatorConfig",
    "MessageHandler",
    "Participant",
    "ParticipantActions",
    "PhaseChangeHandler",
    "SessionInfo",
    "TerminalHandler",
    "TerminalResult",
    "TransportAdapter",
    "VoteResult",
    "VotingStrategy",
    "commitment_handler",
    "evaluation_handler",
    "from_bootstrap",
    "function_committer",
    "function_evaluator",
    "function_voter",
    "majority_committer",
    "majority_voter",
    "start_cancel_callback_server",
    "voting_handler",
]

# ── Deprecated aliases (issue #103 / multiagentcoordinationprotocol#135) ─────
#
# Kept out of __all__ deliberately -- a deprecated name should not appear in
# `from macp_sdk.agent import *` or in generated API docs. Does not delegate
# to strategies.py's own __getattr__: doing so would point the warning's
# stacklevel at this module's frame instead of the caller's, since that's the
# far more common `from macp_sdk.agent import VoteDecision` import path.
#
# This module IS a package (`agent/`, has `__path__`), unlike strategies.py.
# CPython's import machinery (`importlib._bootstrap._handle_fromlist`) probes
# any fromlist name against a package with `hasattr(module, name)` *before*
# the `from ... import` statement's own bytecode-level getattr -- so a
# deprecated name accessed via `from macp_sdk.agent import VoteDecision`
# triggers this __getattr__ twice (confirmed empirically), not once. Under
# Python's default warning filters the two are deduplicated by (message,
# category, location) and a caller sees a single printed line regardless;
# under a strict `error` filter (as this repo's own test suite runs under)
# the first `warnings.warn()` call raises immediately, so the second never
# happens either. Only an explicit `simplefilter("always")` capture (as in
# this repo's own deprecation tests) observes both. This is inherent to
# package-level `__getattr__` and not specific to this alias -- there is no
# fix that preserves the "re-warn on every plain attribute access" property
# documented in strategies.py's own alias comment without also suppressing
# this.
_DEPRECATED_ALIASES = {
    "VoteDecision": "VoteResult",
    "CommitmentDecision": "CommitmentResult",
}


def __getattr__(name: str) -> Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
