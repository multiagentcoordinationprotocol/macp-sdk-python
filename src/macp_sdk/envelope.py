from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping, Sequence

from macp.v1 import core_pb2, envelope_pb2

from .constants import (
    DEFAULT_CONFIGURATION_VERSION,
    DEFAULT_MODE_VERSION,
    DEFAULT_POLICY_VERSION,
    MACP_VERSION,
)
from .errors import MacpSessionError

# ── Outcome inference ────────────────────────────────────────────────

_NEGATIVE_SUFFIXES = ("rejected", "failed", "declined")
_POSITIVE_SUFFIXES = ("selected", "accepted", "completed", "approved")


def infer_outcome_positive(action: str) -> bool:
    """Infer ``outcome_positive`` from the action suffix.

    Actions ending in *rejected*, *failed*, or *declined* are negative.
    Everything else (including unknown suffixes) defaults to positive.
    """
    lower = action.lower()
    return not any(lower.endswith(s) for s in _NEGATIVE_SUFFIXES)


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_message_id() -> str:
    return str(uuid.uuid4())


def new_commitment_id() -> str:
    return str(uuid.uuid4())


def now_unix_ms() -> int:
    return int(time.time() * 1000)


def build_root(uri: str, name: str = "") -> core_pb2.Root:
    return core_pb2.Root(uri=uri, name=name)


def build_session_start_payload(
    *,
    intent: str,
    participants: Sequence[str],
    ttl_ms: int,
    mode_version: str = DEFAULT_MODE_VERSION,
    configuration_version: str = DEFAULT_CONFIGURATION_VERSION,
    policy_version: str = DEFAULT_POLICY_VERSION,
    context_id: str = "",
    extensions: Mapping[str, bytes] | None = None,
    roots: Iterable[core_pb2.Root] | None = None,
) -> core_pb2.SessionStartPayload:
    return core_pb2.SessionStartPayload(
        intent=intent,
        participants=list(participants),
        mode_version=mode_version,
        configuration_version=configuration_version,
        policy_version=policy_version,
        ttl_ms=ttl_ms,
        context_id=context_id,
        extensions=dict(extensions) if extensions else {},
        roots=list(roots or []),
    )


def _has_outcome_positive_field() -> bool:
    """Check if the proto schema supports outcome_positive."""
    return any(f.name == "outcome_positive" for f in core_pb2.CommitmentPayload.DESCRIPTOR.fields)


def _has_supersedes_field() -> bool:
    """Check if the proto schema supports CommitmentPayload.supersedes (>=0.1.3)."""
    return any(f.name == "supersedes" for f in core_pb2.CommitmentPayload.DESCRIPTOR.fields)


def build_commitment_ref(*, session_id: str, commitment_hash: str) -> core_pb2.CommitmentRef:
    """Build a ``CommitmentRef`` (macp-proto 0.1.3) pointing at a prior
    commitment, for use as ``build_commitment_payload(supersedes=...)``."""
    return core_pb2.CommitmentRef(session_id=session_id, commitment_hash=commitment_hash)


def build_commitment_payload(
    *,
    action: str,
    authority_scope: str,
    reason: str,
    commitment_id: str | None = None,
    mode_version: str = DEFAULT_MODE_VERSION,
    configuration_version: str = DEFAULT_CONFIGURATION_VERSION,
    policy_version: str = DEFAULT_POLICY_VERSION,
    outcome_positive: bool | None = None,
    supersedes: core_pb2.CommitmentRef | None = None,
) -> core_pb2.CommitmentPayload:
    """Build a ``CommitmentPayload``.

    ``supersedes`` (macp-proto 0.1.3) optionally references a prior
    commitment this one revises, as a ``CommitmentRef`` of
    ``(session_id, commitment_hash)``. It is absent by default and unrelated
    to proposal-mode ``supersedes_proposal_id``.
    """
    if outcome_positive is None:
        outcome_positive = infer_outcome_positive(action)
    kwargs: dict[str, object] = dict(
        commitment_id=commitment_id or new_commitment_id(),
        action=action,
        authority_scope=authority_scope,
        reason=reason,
        mode_version=mode_version,
        configuration_version=configuration_version,
        policy_version=policy_version,
    )
    if _has_outcome_positive_field():
        kwargs["outcome_positive"] = outcome_positive
    if supersedes is not None:
        if not _has_supersedes_field():
            raise MacpSessionError("CommitmentPayload.supersedes requires macp-proto >= 0.1.3")
        kwargs["supersedes"] = supersedes
    return core_pb2.CommitmentPayload(**kwargs)


def build_signal_payload(
    *,
    signal_type: str,
    data: bytes = b"",
    confidence: float = 0.0,
    correlation_session_id: str = "",
) -> core_pb2.SignalPayload:
    if data and not signal_type.strip():
        raise MacpSessionError("signal_type must be non-empty when data is present")
    return core_pb2.SignalPayload(
        signal_type=signal_type,
        data=data,
        confidence=confidence,
        correlation_session_id=correlation_session_id,
    )


def build_progress_payload(
    *,
    progress_token: str,
    progress: float,
    total: float,
    message: str = "",
    target_message_id: str = "",
) -> core_pb2.ProgressPayload:
    return core_pb2.ProgressPayload(
        progress_token=progress_token,
        progress=progress,
        total=total,
        message=message,
        target_message_id=target_message_id,
    )


def serialize_message(message: object) -> bytes:
    serializer = getattr(message, "SerializeToString", None)
    if serializer is None:
        raise TypeError(f"object {type(message)!r} is not a protobuf message")
    return serializer()


def build_envelope(
    *,
    mode: str,
    message_type: str,
    session_id: str,
    payload: bytes,
    sender: str = "",
    message_id: str | None = None,
    macp_version: str = MACP_VERSION,
    timestamp_unix_ms: int | None = None,
) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version=macp_version,
        mode=mode,
        message_type=message_type,
        message_id=message_id or new_message_id(),
        session_id=session_id,
        sender=sender,
        timestamp_unix_ms=timestamp_unix_ms or now_unix_ms(),
        payload=payload,
    )
