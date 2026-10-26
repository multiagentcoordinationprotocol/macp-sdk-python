import warnings
from importlib.metadata import version as _version
from typing import Any as _Any

from ._logging import configure_logging
from .async_client import AsyncMacpClient, AsyncMacpStream
from .async_session import AsyncBaseSession
from .auth import AuthConfig
from .base_projection import (
    ANOMALY_DUPLICATE_BALLOT,
    ANOMALY_DUPLICATE_TASK_ACCEPT,
    ANOMALY_DUPLICATE_VOTE,
    ANOMALY_SETTLED_HANDOFF,
    BaseProjection,
    ProjectionAnomaly,
)
from .base_session import BaseSession
from .client import UNBOUNDED, InlineErrorCallback, MacpClient, MacpStream, macp_error_code
from .commitment_hash import (
    VerifyOutcome,
    commitment_hash,
    is_canonical_commitment_hash,
    verify_commitment_hash,
)
from .constants import (
    DEFAULT_CONFIGURATION_VERSION,
    DEFAULT_MODE_VERSION,
    DEFAULT_POLICY_VERSION,
    MACP_VERSION,
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_MULTI_ROUND,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
    STANDARD_MODES,
)
from .decision import AsyncDecisionSession, DecisionSession
from .envelope import (
    build_commitment_payload,
    build_commitment_ref,
    build_contribute_payload,
    build_envelope,
    build_progress_payload,
    build_root,
    build_session_start_payload,
    build_signal_payload,
    infer_outcome_positive,
    new_commitment_id,
    new_message_id,
    new_session_id,
    now_unix_ms,
    serialize_message,
)
from .errors import (
    AGENT_NOT_FOUND,
    DUPLICATE_MESSAGE,
    FORBIDDEN,
    HISTORY_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_ENVELOPE,
    INVALID_POLICY_DEFINITION,
    INVALID_REQUEST,
    INVALID_SESSION_ID,
    KNOWN_DENY_CODES,
    MODE_NOT_SUPPORTED,
    PAYLOAD_TOO_LARGE,
    POLICY_DENIED,
    RATE_LIMITED,
    RECEIPT_UNAVAILABLE,
    REGISTRY_READ_ONLY,
    SESSION_ALREADY_EXISTS,
    SESSION_NOT_FOUND,
    SESSION_NOT_OPEN,
    STREAM_LAGGED,
    UNAUTHENTICATED,
    UNKNOWN_POLICY_VERSION,
    UNSUPPORTED_PROTOCOL_VERSION,
    AckFailure,
    ErrorDetails,
    MacpAckError,
    MacpIdentityMismatchError,
    MacpRetryError,
    MacpSdkError,
    MacpSessionError,
    MacpTimeoutError,
    MacpTransportError,
    PolicyDenial,
    parse_error_details,
)
from .handoff import AsyncHandoffSession, HandoffProjection, HandoffRecord, HandoffSession
from .policy import (
    ABSTENTION_INTERPRETATIONS,
    COMMITMENT_AUTHORITIES,
    CRITICAL_OBJECTION_ACTIONS,
    PROPOSAL_ACCEPTANCE_CRITERIA,
    AbstentionRules,
    CommitmentRules,
    CounterProposalRules,
    EvaluationRules,
    HandoffAcceptanceRules,
    ObjectionHandlingRules,
    ProposalAcceptanceRules,
    QuorumThreshold,
    RejectionRules,
    TaskAssignmentRules,
    TaskCompletionRules,
    VotingRules,
    build_decision_policy,
    build_handoff_policy,
    build_proposal_policy,
    build_quorum_policy,
    build_task_policy,
    effective_quorum_threshold,
)
from .projections import (
    DecisionEvaluationRecord,
    DecisionObjectionRecord,
    DecisionProjection,
    DecisionProposalRecord,
    DecisionVoteRecord,
)
from .proposal import (
    AsyncProposalSession,
    ProposalAcceptRecord,
    ProposalProjection,
    ProposalRecord,
    ProposalRejectRecord,
    ProposalSession,
)
from .proto_registry import ProtoRegistry
from .quorum import (
    ApprovalRequestRecord,
    AsyncQuorumSession,
    BallotRecord,
    QuorumProjection,
    QuorumSession,
)
from .receipt import (
    RECEIPT_FORMAT,
    DecisionReceipt,
    DecisionReceiptResult,
    ReceiptKeySet,
    ReceiptSigner,
    ReceiptVerification,
    build_receipt,
    commitment_from_projection,
    trusted_keys_from_jwks,
    verify_receipt,
)
from .retry import RetryPolicy, retry_send
from .task import (
    AsyncTaskSession,
    TaskCompleteRecord,
    TaskFailRecord,
    TaskProjection,
    TaskRecord,
    TaskRejectRecord,
    TaskSession,
    TaskUpdateRecord,
)
from .validation import (
    validate_commitment_hash,
    validate_confidence,
    validate_max_suspend_ms,
    validate_participant_count,
    validate_participants,
    validate_progress_scope,
    validate_recommendation,
    validate_required_field,
    validate_session_id,
    validate_session_start,
    validate_severity,
    validate_signal_type,
    validate_ttl_ms,
    validate_vote,
)
from .watchers import (
    TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES,
    ModeRegistryWatcher,
    PolicyChange,
    PolicyWatcher,
    RootsWatcher,
    SessionLifecycleEvent,
    SessionLifecycleWatcher,
    SignalEvent,
    SignalWatcher,
)

__version__ = _version("macp-sdk-python")

__all__ = [
    "ABSTENTION_INTERPRETATIONS",
    "AGENT_NOT_FOUND",
    "ANOMALY_DUPLICATE_BALLOT",
    "ANOMALY_DUPLICATE_TASK_ACCEPT",
    "ANOMALY_DUPLICATE_VOTE",
    "ANOMALY_SETTLED_HANDOFF",
    "COMMITMENT_AUTHORITIES",
    "CRITICAL_OBJECTION_ACTIONS",
    "DEFAULT_CONFIGURATION_VERSION",
    "DEFAULT_MODE_VERSION",
    "DEFAULT_POLICY_VERSION",
    "DUPLICATE_MESSAGE",
    "FORBIDDEN",
    "HISTORY_UNAVAILABLE",
    "INTERNAL_ERROR",
    "INVALID_ENVELOPE",
    "INVALID_POLICY_DEFINITION",
    "INVALID_REQUEST",
    "INVALID_SESSION_ID",
    "KNOWN_DENY_CODES",
    "MACP_VERSION",
    "MODE_DECISION",
    "MODE_HANDOFF",
    "MODE_MULTI_ROUND",
    "MODE_NOT_SUPPORTED",
    "MODE_PROPOSAL",
    "MODE_QUORUM",
    "MODE_TASK",
    "PAYLOAD_TOO_LARGE",
    "POLICY_DENIED",
    "PROPOSAL_ACCEPTANCE_CRITERIA",
    "RATE_LIMITED",
    "RECEIPT_FORMAT",
    "RECEIPT_UNAVAILABLE",
    "REGISTRY_READ_ONLY",
    "SESSION_ALREADY_EXISTS",
    "SESSION_NOT_FOUND",
    "SESSION_NOT_OPEN",
    "STANDARD_MODES",
    "STREAM_LAGGED",
    "TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES",
    "UNAUTHENTICATED",
    "UNBOUNDED",
    "UNKNOWN_POLICY_VERSION",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "AbstentionRules",
    "AckFailure",
    "ApprovalRequestRecord",
    "AsyncBaseSession",
    "AsyncDecisionSession",
    "AsyncHandoffSession",
    "AsyncMacpClient",
    "AsyncMacpStream",
    "AsyncProposalSession",
    "AsyncQuorumSession",
    "AsyncTaskSession",
    "AuthConfig",
    "BallotRecord",
    "BaseProjection",
    "BaseSession",
    "CommitmentRules",
    "CounterProposalRules",
    "DecisionEvaluationRecord",
    "DecisionObjectionRecord",
    "DecisionProjection",
    "DecisionProposalRecord",
    "DecisionReceipt",
    "DecisionReceiptResult",
    "DecisionSession",
    "DecisionVoteRecord",
    "ErrorDetails",
    "EvaluationRules",
    "HandoffAcceptanceRules",
    "HandoffProjection",
    "HandoffRecord",
    "HandoffSession",
    "InlineErrorCallback",
    "MacpAckError",
    "MacpClient",
    "MacpIdentityMismatchError",
    "MacpRetryError",
    "MacpSdkError",
    "MacpSessionError",
    "MacpStream",
    "MacpTimeoutError",
    "MacpTransportError",
    "ModeRegistryWatcher",
    "ObjectionHandlingRules",
    "PolicyChange",
    "PolicyDenial",
    "PolicyWatcher",
    "ProjectionAnomaly",
    "ProposalAcceptRecord",
    "ProposalAcceptanceRules",
    "ProposalProjection",
    "ProposalRecord",
    "ProposalRejectRecord",
    "ProposalSession",
    "ProtoRegistry",
    "QuorumProjection",
    "QuorumSession",
    "QuorumThreshold",
    "ReceiptKeySet",
    "ReceiptSigner",
    "ReceiptVerification",
    "RejectionRules",
    "RetryPolicy",
    "RootsWatcher",
    "SessionLifecycleEvent",
    "SessionLifecycleWatcher",
    "SignalEvent",
    "SignalWatcher",
    "TaskAssignmentRules",
    "TaskCompleteRecord",
    "TaskCompletionRules",
    "TaskFailRecord",
    "TaskProjection",
    "TaskRecord",
    "TaskRejectRecord",
    "TaskSession",
    "TaskUpdateRecord",
    "VerifyOutcome",
    "VotingRules",
    "build_commitment_payload",
    "build_commitment_ref",
    "build_contribute_payload",
    "build_decision_policy",
    "build_envelope",
    "build_handoff_policy",
    "build_progress_payload",
    "build_proposal_policy",
    "build_quorum_policy",
    "build_receipt",
    "build_root",
    "build_session_start_payload",
    "build_signal_payload",
    "build_task_policy",
    "commitment_from_projection",
    "commitment_hash",
    "configure_logging",
    "effective_quorum_threshold",
    "infer_outcome_positive",
    "is_canonical_commitment_hash",
    "macp_error_code",
    "new_commitment_id",
    "new_message_id",
    "new_session_id",
    "now_unix_ms",
    "parse_error_details",
    "retry_send",
    "serialize_message",
    "trusted_keys_from_jwks",
    "validate_commitment_hash",
    "validate_confidence",
    "validate_max_suspend_ms",
    "validate_participant_count",
    "validate_participants",
    "validate_progress_scope",
    "validate_recommendation",
    "validate_required_field",
    "validate_session_id",
    "validate_session_start",
    "validate_severity",
    "validate_signal_type",
    "validate_ttl_ms",
    "validate_vote",
    "verify_commitment_hash",
    "verify_receipt",
]

# ── Deprecated aliases (issue #103 / multiagentcoordinationprotocol#135) ─────
#
# Kept out of __all__ deliberately -- a deprecated name should not appear in
# `from macp_sdk import *` or in generated API docs. Does not delegate to the
# defining submodule's own __getattr__ (proposal.py's, watchers.py's): doing
# so would point the warning's stacklevel at this module's frame instead of
# the caller's, since `from macp_sdk import OldName` (this top-level package)
# is the far more common import path -- same reasoning as agent/__init__.py's
# own alias dict.
#
# This module IS a package (`macp_sdk/`, has __path__), like agent/__init__.py.
# CPython's import machinery (`importlib._bootstrap._handle_fromlist`) probes
# any fromlist name against a package with `hasattr(module, name)` *before*
# the `from ... import` statement's own bytecode-level getattr -- so a
# deprecated name accessed via `from macp_sdk import RejectRecord` triggers
# this __getattr__ twice (confirmed empirically in Phase 1), not once. Under
# Python's default warning filters the two are deduplicated by (message,
# category, location) and a caller sees a single printed line regardless;
# under a strict `error` filter (as this repo's own test suite runs under)
# the first `warnings.warn()` call raises immediately, so the second never
# happens either. Only an explicit `simplefilter("always")` capture (as in
# this repo's own deprecation tests) observes both. By contrast, resolving
# the same old name from its *defining* submodule (`macp_sdk.proposal`,
# `macp_sdk.watchers`, `macp_sdk.task` -- plain files, no __path__) fires
# this pattern once.
#
# One dict, one __getattr__, covering all renamed top-level exports (issue
# #103 items 3-5, issue #108) -- each entry's defining submodule (proposal.py,
# watchers.py, task.py) owns its own identical-shaped __getattr__ for direct
# submodule imports; this one is only reached via the `macp_sdk` top-level
# package path.
_DEPRECATED_ALIASES = {
    "RejectRecord": "ProposalRejectRecord",
    "AcceptRecord": "ProposalAcceptRecord",
    "SessionLifecycle": "SessionLifecycleEvent",
    "TaskRequestRecord": "TaskRecord",
}


def __getattr__(name: str) -> _Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
