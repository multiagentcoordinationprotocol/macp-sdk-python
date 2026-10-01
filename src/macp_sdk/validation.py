"""Centralized input validation for MACP SDK operations.

All validation functions raise ``MacpSessionError`` on failure.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from .commitment_hash import is_canonical_commitment_hash
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


def validate_commitment_hash(value: str) -> None:
    """Validate that *value* has the shape of a canonical commitment hash.

    Mirrors the runtime's RFC-MACP-0013 syntax check: a valid
    ``commitment_hash`` MUST match ``^sha256:[0-9a-f]{64}$`` exactly (see
    `macp_sdk.commitment_hash.is_canonical_commitment_hash`). This is a pure
    shape check — it does not (and cannot) verify that the digest was
    actually produced by `macp_sdk.commitment_hash.commitment_hash` over the
    referenced payload; that requires the payload itself.
    """
    if not is_canonical_commitment_hash(value):
        raise MacpSessionError(
            f"commitment_hash must match ^sha256:[0-9a-f]{{64}}$ (RFC-MACP-0013), got: {value!r}"
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
    # NaN < 0.0 and NaN > 1.0 are both False, so a bare range check silently
    # *accepts* NaN; inf was already rejected incidentally by > 1.0 and is
    # now rejected explicitly.
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
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


def validate_progress_scope(session_id: str, mode: str) -> None:
    """Validate the ``Progress`` scope pairing (RFC-MACP-0001 §6).

    ``Progress`` is legal in exactly two shapes -- *ambient* (``session_id``
    and ``mode`` both empty) or *session-scoped* (both non-empty). An envelope
    with exactly one of the two empty is a mixed shape that the runtime
    rejects with ``INVALID_ENVELOPE``; raising here names the mismatched field
    instead.

    Unlike Signals, ``Progress`` is *not* required to be ambient -- this is a
    tri-state rule, so neither field may be inferred from the other.

    The emptiness test deliberately mirrors the runtime's
    ``validate_envelope_shape`` **exactly**, including its asymmetry:
    ``session_id`` is compared raw while ``mode`` is stripped first. Mirroring
    rather than normalising is the only choice under which the SDK neither
    accepts a shape the runtime rejects nor rejects one it accepts.
    Concretely, a whitespace-only ``mode`` alongside an empty ``session_id``
    is ambient to the runtime, so it stays ambient here.
    """
    session_id_empty = session_id == ""
    mode_empty = mode.strip() == ""
    if session_id_empty == mode_empty:
        return
    detail = (
        f"mode is {mode!r} but session_id is empty"
        if session_id_empty
        else f"session_id is {session_id!r} but mode is empty"
    )
    raise MacpSessionError(
        "Progress must be either ambient (session_id and mode both empty) or "
        f"session-scoped (both non-empty), but {detail} (RFC-MACP-0001 §6). "
        "Pass both fields for a session-scoped Progress, or neither for an ambient one."
    )


def validate_ttl_ms(ttl_ms: int) -> None:
    """Validate that *ttl_ms* is in [1, 86_400_000]."""
    if not math.isfinite(ttl_ms) or ttl_ms < 1 or ttl_ms > _MAX_TTL_MS:
        raise MacpSessionError(f"ttl_ms must be in [1, {_MAX_TTL_MS}], got {ttl_ms}")


def validate_max_suspend_ms(max_suspend_ms: int) -> None:
    """Validate that *max_suspend_ms* is >= 0 (``0`` selects the runtime default).

    No upper bound is imposed: the runtime does not cap the value either
    -- ``macp-runtime/src/runtime.rs:487-495`` binds whatever positive
    value it is given as the session's ``bound_max_suspend_ms`` and only
    falls back to its own 7-day default when the field is ``0``. The
    sibling TypeScript SDK's ``validateMaxSuspendMs``
    (``src/validation.ts:134-139``) is identical, deliberately.
    """
    if not math.isfinite(max_suspend_ms) or max_suspend_ms < 0:
        raise MacpSessionError(
            f"max_suspend_ms must be >= 0 (0 selects the runtime default), got {max_suspend_ms}"
        )


def validate_participants(participants: Sequence[str], *, allow_empty: bool = False) -> None:
    """Validate participant list: non-empty, no duplicates, within count limit.

    ``allow_empty`` skips the non-empty check only -- the duplicate check and
    the count limit still apply to whatever is passed. Decision mode is the
    sole caller of ``allow_empty=True`` (via ``validate_session_start``): the
    runtime deliberately accepts an empty ``participants`` list for Decision
    mode's ``SessionStart`` (RFC-MACP-0001 §7.1, RFC-MACP-0007), while the
    other four standards-track modes re-reject an insufficient roster in
    their own ``on_session_start`` regardless of what the SDK does here.
    """
    if not participants and not allow_empty:
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
    allow_empty_participants: bool = False,
) -> None:
    """Composite validation for SessionStart parameters.

    ``allow_empty_participants`` forwards to :func:`validate_participants` --
    see its docstring for why Decision mode is the one caller that sets it.
    """
    # RFC-MACP-0001 §7.1 does not require a non-empty intent, and the
    # runtime accepts an empty one. ``intent`` is kept in the signature
    # because this function is public and exported (``__init__.py``), so
    # removing the parameter would be a breaking change for a fix whose
    # entire point is to be *less* strict. ``del`` rather than an ARG001
    # suppression comment: ruff's ARG rules apply to ``src/`` (per-file-
    # ignores covers ``tests/**`` only), and this states the intent
    # instead of silencing the check.
    del intent  # accepted but deliberately unvalidated (RFC-MACP-0001 §7.1)
    validate_participants(participants, allow_empty=allow_empty_participants)
    validate_ttl_ms(ttl_ms)
    validate_required_field("mode_version", mode_version)
    validate_required_field("configuration_version", configuration_version)
