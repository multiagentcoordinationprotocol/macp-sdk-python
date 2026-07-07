from __future__ import annotations

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


class MacpSdkError(Exception):
    """Base SDK exception."""


@dataclass(slots=True)
class AckFailure:
    code: str
    message: str
    session_id: str = ""
    message_id: str = ""
    reasons: list[str] = field(default_factory=list)


class MacpAckError(MacpSdkError):
    """Runtime rejected the message (NACK)."""

    def __init__(
        self,
        failure: AckFailure,
        *,
        mode: str = "",
        message_type: str = "",
    ):
        self.failure = failure
        self.mode = mode
        self.message_type = message_type
        super().__init__(f"{failure.code}: {failure.message}")

    @property
    def reasons(self) -> list[str]:
        """Structured denial reasons (populated for POLICY_DENIED)."""
        return self.failure.reasons

    def __repr__(self) -> str:
        parts = [f"code={self.failure.code!r}", f"message={self.failure.message!r}"]
        if self.failure.session_id:
            parts.append(f"session_id={self.failure.session_id!r}")
        if self.mode:
            parts.append(f"mode={self.mode!r}")
        if self.message_type:
            parts.append(f"message_type={self.message_type!r}")
        if self.failure.reasons:
            parts.append(f"reasons={self.failure.reasons!r}")
        return f"MacpAckError({', '.join(parts)})"


class MacpTransportError(MacpSdkError):
    """gRPC communication failure.

    ``code`` carries the gRPC status code name (e.g. ``"RESOURCE_EXHAUSTED"``,
    ``"UNAUTHENTICATED"``, ``"FAILED_PRECONDITION"``) when the failure
    originated from an ``RpcError``, or ``None`` otherwise. It lets callers
    distinguish a lagging watch/stream consumer (``RESOURCE_EXHAUSTED`` →
    reconnect) or a missing-auth watch (``UNAUTHENTICATED``) from a generic
    transport failure without parsing the message string.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
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
