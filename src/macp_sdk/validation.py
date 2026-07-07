"""Centralized input validation for MACP SDK operations.

All validation functions raise ``MacpSessionError`` on failure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .errors import MacpSessionError

# A string with the structural shape of a UUID (36 chars, hyphens at
# 8-13-18-23, hex-only otherwise) — case-insensitive, any version/variant.
_UUID_SHAPE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Strict lowercase UUID v4/v7 (version nibble 4 or 7, RFC 9562 variant 8/9/a/b).
_UUID_V4V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[47][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]{22,}$")

_VALID_VOTES = frozenset({"APPROVE", "REJECT", "ABSTAIN"})
_VALID_RECOMMENDATIONS = frozenset({"APPROVE", "REVIEW", "BLOCK", "REJECT"})
_VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low"})

_MAX_PARTICIPANTS = 1000
_MAX_TTL_MS = 86_400_000  # 24 hours


def validate_session_id(sid: str) -> None:
    """Validate that *sid* is a lowercase UUID v4/v7 or base64url (22+ chars).

    Mirrors the runtime's **no-fall-through** rule (runtime v0.5.0 change
    review A4): a string that has the structural shape of a UUID is validated
    strictly as a lowercase v4/v7 UUID — it is *not* reinterpreted as
    base64url (which would otherwise accept an uppercase or wrong-version
    UUID). A 36-char base64url ID containing ``-`` that is not UUID-shaped
    (e.g. non-hex characters) still validates via the base64url branch, as the
    runtime accepts.

    This validation is advisory — it runs only when an explicit ``session_id``
    is passed; auto-generated IDs are always valid v4 UUIDs.
    """
    if _UUID_SHAPE_RE.match(sid):
        if not _UUID_V4V7_RE.match(sid):
            raise MacpSessionError(
                "session_id is UUID-shaped but not a lowercase v4/v7 UUID "
                f"(no fall-through to base64url), got: {sid!r}"
            )
        return
    if _BASE64URL_RE.match(sid):
        return
    raise MacpSessionError(
        f"session_id must be a lowercase UUID v4/v7 or base64url (22+ chars), got: {sid!r}"
    )


def validate_vote(value: str) -> str:
    """Normalize *value* to uppercase and validate as APPROVE/REJECT/ABSTAIN."""
    normalized = value.upper()
    if normalized not in _VALID_VOTES:
        raise MacpSessionError(
            f"invalid vote value {value!r}: must be one of APPROVE, REJECT, ABSTAIN"
        )
    return normalized


def validate_recommendation(value: str) -> str:
    """Normalize *value* to uppercase and validate as APPROVE/REVIEW/BLOCK/REJECT."""
    normalized = value.upper()
    if normalized not in _VALID_RECOMMENDATIONS:
        raise MacpSessionError(
            f"invalid recommendation {value!r}: must be one of APPROVE, REVIEW, BLOCK, REJECT"
        )
    return normalized


def validate_confidence(value: float) -> None:
    """Validate that *value* is in [0.0, 1.0]."""
    if value < 0.0 or value > 1.0:
        raise MacpSessionError(f"confidence must be in [0.0, 1.0], got {value}")


def validate_severity(value: str) -> str:
    """Normalize *value* to lowercase and validate as critical/high/medium/low."""
    normalized = value.lower()
    if normalized not in _VALID_SEVERITIES:
        raise MacpSessionError(
            f"invalid severity {value!r}: must be one of critical, high, medium, low"
        )
    return normalized


def validate_participant_count(count: int) -> None:
    """Validate that *count* does not exceed the maximum."""
    if count > _MAX_PARTICIPANTS:
        raise MacpSessionError(f"Maximum {_MAX_PARTICIPANTS} participants per session")


def validate_signal_type(signal_type: str, data: bytes | None = None) -> None:
    """Validate that *signal_type* is non-empty when *data* is present."""
    if data and len(data) > 0 and not signal_type.strip():
        raise MacpSessionError("signal_type must be non-empty when data is present")


def validate_ttl_ms(ttl_ms: int) -> None:
    """Validate that *ttl_ms* is in [1, 86_400_000]."""
    if ttl_ms < 1 or ttl_ms > _MAX_TTL_MS:
        raise MacpSessionError(f"ttl_ms must be in [1, {_MAX_TTL_MS}], got {ttl_ms}")


def validate_participants(participants: Sequence[str]) -> None:
    """Validate participant list: non-empty, no duplicates, within count limit."""
    if not participants:
        raise MacpSessionError("participants must be non-empty")
    seen: set[str] = set()
    for p in participants:
        if p in seen:
            raise MacpSessionError(f"duplicate participant: {p}")
        seen.add(p)
    validate_participant_count(len(participants))


def validate_required_field(field_name: str, value: str) -> None:
    """Validate that *value* is non-empty after stripping whitespace."""
    if not value or not value.strip():
        raise MacpSessionError(f"{field_name} must be non-empty")


def validate_session_start(
    *,
    intent: str,
    participants: Sequence[str],
    ttl_ms: int,
    mode_version: str,
    configuration_version: str,
) -> None:
    """Composite validation for SessionStart parameters."""
    validate_required_field("intent", intent)
    validate_participants(participants)
    validate_ttl_ms(ttl_ms)
    validate_required_field("mode_version", mode_version)
    validate_required_field("configuration_version", configuration_version)
