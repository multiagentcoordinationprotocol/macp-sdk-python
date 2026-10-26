from __future__ import annotations

import json
from dataclasses import dataclass, field

# ── Well-known error codes ───────────────────────────────────────────

SESSION_ALREADY_EXISTS = "SESSION_ALREADY_EXISTS"
POLICY_DENIED = "POLICY_DENIED"
UNKNOWN_POLICY_VERSION = "UNKNOWN_POLICY_VERSION"
INVALID_POLICY_DEFINITION = "INVALID_POLICY_DEFINITION"
UNSUPPORTED_PROTOCOL_VERSION = "UNSUPPORTED_PROTOCOL_VERSION"
INVALID_ENVELOPE = "INVALID_ENVELOPE"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
SESSION_NOT_OPEN = "SESSION_NOT_OPEN"
MODE_NOT_SUPPORTED = "MODE_NOT_SUPPORTED"
FORBIDDEN = "FORBIDDEN"
UNAUTHENTICATED = "UNAUTHENTICATED"
DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
RATE_LIMITED = "RATE_LIMITED"
INTERNAL_ERROR = "INTERNAL_ERROR"
INVALID_SESSION_ID = "INVALID_SESSION_ID"
# Control-plane codes added in runtime 0.9.0.
REGISTRY_READ_ONLY = "REGISTRY_READ_ONLY"  # FAILED_PRECONDITION: file-loaded registry
INVALID_REQUEST = "INVALID_REQUEST"  # INVALID_ARGUMENT: malformed non-envelope request
STREAM_LAGGED = "STREAM_LAGGED"  # RESOURCE_EXHAUSTED: subscriber fell behind; resubscribe
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"  # NOT_FOUND: GetManifest for an agent not hosted here
# RFC-MACP-0012 §8.1 (spec #227).
HISTORY_UNAVAILABLE = (
    "HISTORY_UNAVAILABLE"  # FAILED_PRECONDITION: WhatIfReplay on compacted history
)
# RFC-MACP-0013 §13 (spec #230). Provisional in the registry, so it is not
# in the parity contract's permanent set yet.
RECEIPT_UNAVAILABLE = (
    "RECEIPT_UNAVAILABLE"  # FAILED_PRECONDITION: GetDecisionReceipt, no receipt to issue
)

# ── POLICY_DENIED denial codes (RFC-MACP-0012 §6.2, runtime ≥ 0.9.0) ──
#
# The set is open: a custom evaluator may send a code outside it, which the
# SDK passes through unchanged. Treat any code not in ``KNOWN_DENY_CODES`` as
# ``DENY_UNSPECIFIED``.

DENY_VETO = "veto"
DENY_VETO_HOLD = "veto_hold"
DENY_QUORUM_NOT_MET = "quorum_not_met"
DENY_THRESHOLD_NOT_MET = "threshold_not_met"
DENY_CONFIDENCE_BELOW_MINIMUM = "confidence_below_minimum"
DENY_EVALUATION_REQUIRED = "evaluation_required"
DENY_NO_VOTES = "no_votes"
DENY_DECLINE_OVER_APPROVAL = "decline_over_approval"
DENY_DECLINE_NOT_BACKED = "decline_not_backed"
DENY_CRITERION_NOT_MET = "criterion_not_met"
DENY_OUTPUT_REQUIRED = "output_required"
DENY_REASSIGNMENT_NOT_ALLOWED = "reassignment_not_allowed"
DENY_UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
DENY_INVALID_RULES = "invalid_rules"
DENY_UNSPECIFIED = "unspecified"

KNOWN_DENY_CODES: frozenset[str] = frozenset(
    {
        DENY_VETO,
        DENY_VETO_HOLD,
        DENY_QUORUM_NOT_MET,
        DENY_THRESHOLD_NOT_MET,
        DENY_CONFIDENCE_BELOW_MINIMUM,
        DENY_EVALUATION_REQUIRED,
        DENY_NO_VOTES,
        DENY_DECLINE_OVER_APPROVAL,
        DENY_DECLINE_NOT_BACKED,
        DENY_CRITERION_NOT_MET,
        DENY_OUTPUT_REQUIRED,
        DENY_REASSIGNMENT_NOT_ALLOWED,
        DENY_UNSUPPORTED_SCHEMA_VERSION,
        DENY_INVALID_RULES,
        DENY_UNSPECIFIED,
    }
)


@dataclass(frozen=True, slots=True)
class PolicyDenial:
    """One structured ``POLICY_DENIED`` reason (runtime ≥ 0.9.0).

    ``code`` is the wire code, unchanged (``"unspecified"`` when the runtime
    sent none). The set is open, so check it against :data:`KNOWN_DENY_CODES`
    and treat anything else as ``"unspecified"``.
    ``participants`` lists the agents the reason concerns (e.g. a veto's
    objectors) — since 0.9.0 they no longer appear inside ``message``.
    """

    code: str
    message: str = ""
    participants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ErrorDetails:
    """Decoded MACP error ``details`` JSON (``reasons`` + ``denials``)."""

    reasons: list[str] = field(default_factory=list)
    denials: list[PolicyDenial] = field(default_factory=list)


def parse_error_details(details: bytes | str | None) -> ErrorDetails:
    """Decode MACP error ``details`` bytes, tolerating malformed input.

    Accepts the bytes found in ``Ack.error.details``, an inline stream
    ``MACPError.details``, or the ``macp-error-details-bin`` trailing
    metadata. Pre-0.9 runtimes send only ``reasons``; ``denials`` is then
    empty. Anything that doesn't decode yields empty lists, never raises.
    """
    if not details:
        return ErrorDetails()
    try:
        parsed = json.loads(details)
    except (ValueError, TypeError):
        return ErrorDetails()
    if not isinstance(parsed, dict):
        return ErrorDetails()

    raw_reasons = parsed.get("reasons")
    reasons = (
        [r for r in raw_reasons if isinstance(r, str)] if isinstance(raw_reasons, list) else []
    )

    denials: list[PolicyDenial] = []
    raw_denials = parsed.get("denials")
    if isinstance(raw_denials, list):
        for item in raw_denials:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            message = item.get("message")
            participants = item.get("participants")
            denials.append(
                PolicyDenial(
                    code=code if isinstance(code, str) and code else DENY_UNSPECIFIED,
                    message=message if isinstance(message, str) else "",
                    participants=tuple(p for p in participants if isinstance(p, str))
                    if isinstance(participants, list)
                    else (),
                )
            )
    return ErrorDetails(reasons=reasons, denials=denials)


class MacpSdkError(Exception):
    """Base SDK exception."""


@dataclass(slots=True)
class AckFailure:
    code: str
    message: str
    session_id: str = ""
    message_id: str = ""
    reasons: list[str] = field(default_factory=list)
    denials: list[PolicyDenial] = field(default_factory=list)


class MacpAckError(MacpSdkError):
    """Runtime rejected the message (NACK)."""

    def __init__(
        self,
        failure: AckFailure,
        *,
        mode: str = "",
        message_type: str = "",
        macp_code: str | None = None,
    ):
        self.failure = failure
        self.mode = mode
        self.message_type = message_type
        # Registry code from ``macp-error-code`` metadata (runtime ≥ 0.9.0) when
        # this NACK was built from a gRPC status. Usually equals
        # ``failure.code``; differs on the policy-registry mutations, which keep
        # ``failure.code="FAILED_PRECONDITION"`` while this names the cause
        # (``REGISTRY_READ_ONLY``). ``None`` for in-band NACKs and old runtimes.
        self.macp_code = macp_code
        super().__init__(f"{failure.code}: {failure.message}")

    @property
    def reasons(self) -> list[str]:
        """Structured denial reasons (populated for POLICY_DENIED)."""
        return self.failure.reasons

    @property
    def denials(self) -> list[PolicyDenial]:
        """Structured denials with stable codes (POLICY_DENIED, runtime ≥ 0.9.0)."""
        return self.failure.denials

    def __repr__(self) -> str:
        parts = [f"code={self.failure.code!r}", f"message={self.failure.message!r}"]
        if self.macp_code and self.macp_code != self.failure.code:
            parts.append(f"macp_code={self.macp_code!r}")
        if self.failure.session_id:
            parts.append(f"session_id={self.failure.session_id!r}")
        if self.mode:
            parts.append(f"mode={self.mode!r}")
        if self.message_type:
            parts.append(f"message_type={self.message_type!r}")
        if self.failure.reasons:
            parts.append(f"reasons={self.failure.reasons!r}")
        if self.failure.denials:
            parts.append(f"denials={[d.code for d in self.failure.denials]!r}")
        return f"MacpAckError({', '.join(parts)})"


class MacpTransportError(MacpSdkError):
    """gRPC communication failure.

    ``code`` carries the gRPC status code name (e.g. ``"RESOURCE_EXHAUSTED"``,
    ``"UNAUTHENTICATED"``, ``"FAILED_PRECONDITION"``) when the failure
    originated from an ``RpcError``, or ``None`` otherwise. It lets callers
    distinguish a lagging watch/stream consumer (``RESOURCE_EXHAUSTED`` →
    reconnect; ``macp_code == "STREAM_LAGGED"`` on runtime ≥ 0.9.0) or a
    missing-auth watch (``UNAUTHENTICATED``) from a generic transport failure
    without parsing the message string.

    ``macp_code`` carries the MACP registry code from the ``macp-error-code``
    trailing metadata (runtime ≥ 0.9.0), e.g. ``"SESSION_NOT_FOUND"`` behind
    a ``NOT_FOUND`` status. Several MACP codes share one gRPC status, so
    branch on ``macp_code`` when it is set. ``None`` against older runtimes
    or for failures the runtime didn't attribute to a MACP code.
    """

    def __init__(
        self, message: str, *, code: str | None = None, macp_code: str | None = None
    ) -> None:
        self.code = code
        self.macp_code = macp_code
        super().__init__(message)


class MacpSessionError(MacpSdkError):
    """Session-level error (wrong state, not started, already committed)."""


class MacpIdentityMismatchError(MacpSdkError):
    """Envelope ``sender`` does not match the auth identity's ``expected_sender``.

    The runtime derives ``sender`` from authenticated identity and rejects any
    value that does not match (RFC-MACP-0004 §4). Catching this mismatch client-side
    surfaces a clearer error than an opaque ``UNAUTHENTICATED`` from the runtime.
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"sender {actual!r} does not match auth identity {expected!r}; "
            f"the runtime will reject this envelope as UNAUTHENTICATED"
        )


class MacpTimeoutError(MacpTransportError):
    """Operation timed out."""


class MacpRetryError(MacpTransportError):
    """All retry attempts exhausted."""
