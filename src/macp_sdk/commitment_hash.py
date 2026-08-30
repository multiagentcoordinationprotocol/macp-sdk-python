"""Canonical commitment hash (RFC-MACP-0013).

Computes ``CommitmentRef.commitment_hash`` as a domain-separated SHA-256
digest over an RFC 8785 (JCS) canonicalized JSON projection of the frozen
nine-field ``CommitmentPayload`` set (RFC-MACP-0013 §5).

This module intentionally has **no third-party imports beyond
``google.protobuf``/``macp.v1.core_pb2``** — the hashing algorithm is pure
stdlib (plus the protobuf runtime already required to hold ``payload``
itself) so that it can be reused (and re-verified) without pulling in the
rest of the SDK.
"""

from __future__ import annotations

import hashlib
import re

from google.protobuf import unknown_fields  # type: ignore[import-untyped]
from macp.v1 import core_pb2

from .errors import MacpSessionError

#: Identifies this projection/preimage construction (RFC-MACP-0013 §4, §7).
#: A change here is a MACP protocol MINOR change (new label, e.g. "/2").
LABEL = "macp-commitment-hash/1"

#: The frozen nine-field set ``CommitmentPayload`` MUST be limited to for this
#: hash label (RFC-MACP-0013 §5). A future ``macp-proto`` release that adds a
#: tenth field to the message is not hashable under this label -- see
#: `_check_frozen_field_set`.
_FROZEN_FIELD_NAMES = frozenset(
    {
        "commitment_id",
        "action",
        "authority_scope",
        "reason",
        "mode_version",
        "policy_version",
        "configuration_version",
        "outcome_positive",
        "supersedes",
    }
)

#: The installed ``macp-proto``'s actual ``CommitmentPayload`` field names,
#: computed once at import time so `_check_frozen_field_set` never has to
#: walk the descriptor on every `commitment_hash()` call.
_ACTUAL_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in core_pb2.CommitmentPayload.DESCRIPTOR.fields
)

#: The frozen two-field set ``CommitmentRef`` (the type of `supersedes`) MUST
#: be limited to for this hash label (RFC-MACP-0013 §5): "``supersedes``,
#: when set, carries exactly two fields (``session_id``, ``commitment_hash``)".
#: A future ``macp-proto`` release that adds a third field to ``CommitmentRef``
#: itself is not hashable under this label -- see `_check_frozen_ref_field_set`.
_FROZEN_REF_FIELD_NAMES = frozenset({"session_id", "commitment_hash"})

#: The installed ``macp-proto``'s actual ``CommitmentRef`` field names,
#: computed once at import time, mirroring `_ACTUAL_FIELD_NAMES` above.
_ACTUAL_REF_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in core_pb2.CommitmentRef.DESCRIPTOR.fields
)

#: Matched with `re.fullmatch` (see `is_canonical_commitment_hash`), so the
#: anchors here are redundant but kept for readability.
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# RFC 8785 §3.2.2.2 short-form escapes.
_SHORT_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_json_string(value: str) -> str:
    """Escape ``value`` per RFC 8785 §3.2.2.2 and wrap it in double quotes.

    Short-form escapes are used for backslash, quote, and the C0 controls
    that have them (\\b \\t \\n \\f \\r); other C0 controls (U+0000-U+001F)
    use \\u00XX; everything else -- including all non-ASCII and astral-plane
    code points -- is emitted literally as the Unicode scalar value it is
    (Python `str` already holds scalar values, not UTF-16 code units, so no
    surrogate-pair handling is needed here).
    """
    out: list[str] = ['"']
    for ch in value:
        short = _SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _json_bool(value: bool) -> str:
    return "true" if value else "false"


def _supersedes_member(ref: core_pb2.CommitmentRef) -> str:
    # Inside `supersedes`, key order is commitment_hash, session_id
    # (plain lexicographic sort of the two ASCII member names).
    members = [
        f"{_escape_json_string('commitment_hash')}:{_escape_json_string(ref.commitment_hash)}",
        f"{_escape_json_string('session_id')}:{_escape_json_string(ref.session_id)}",
    ]
    return "{" + ",".join(members) + "}"


def _check_frozen_field_set(field_names: frozenset[str] | None = None) -> None:
    """Raise if ``field_names`` contains anything outside the RFC-MACP-0013 §5
    frozen nine-field set for ``CommitmentPayload``.

    RFC-MACP-0013 §5: "A ``CommitmentPayload`` carrying a field outside this
    set is not hashable under this label. A verifier presented with such a
    payload MUST return a cannot-verify result... never silently ignored."
    §12 reinforces that skipping this check "produces a value that is not a
    canonical commitment hash at all."

    Defaults to the installed proto's actual field set (`_ACTUAL_FIELD_NAMES`,
    computed once at import time and re-read from the module namespace here
    -- not bound as a mutable default argument -- so that tests can
    monkeypatch it); a caller may also pass an explicit set directly. Either
    way this is how tests exercise the "extra field" branch without needing a
    real 10-field proto descriptor.

    This is *not* about an older proto missing a field (that's already
    handled by the comment in `commitment_hash()`: accessing a genuinely
    absent field raises ``AttributeError`` naturally) -- this is only about a
    newer proto carrying a field this module does not know about.
    """
    if field_names is None:
        field_names = _ACTUAL_FIELD_NAMES
    extra = field_names - _FROZEN_FIELD_NAMES
    if extra:
        raise MacpSessionError(
            "CommitmentPayload carries field(s) outside the RFC-MACP-0013 §5 "
            f"frozen nine-field set and is not hashable under label {LABEL!r}: "
            f"{sorted(extra)}"
        )


def _check_frozen_ref_field_set(field_names: frozenset[str] | None = None) -> None:
    """Raise if ``field_names`` contains anything outside the RFC-MACP-0013 §5
    frozen two-field set for ``CommitmentRef`` (the type of ``supersedes``).

    Mirrors `_check_frozen_field_set` exactly, one level down: that guard
    catches a future ``macp-proto`` growing a 10th field on
    ``CommitmentPayload`` itself; this one catches a future ``macp-proto``
    growing a 3rd field on the nested ``CommitmentRef`` message, which
    `_supersedes_member` would otherwise silently under-project without
    anything raising.

    Defaults to the installed proto's actual field set
    (`_ACTUAL_REF_FIELD_NAMES`, computed once at import time and re-read from
    the module namespace here -- not bound as a mutable default argument --
    so that tests can monkeypatch it); a caller may also pass an explicit set
    directly, exactly as `_check_frozen_field_set` does.
    """
    if field_names is None:
        field_names = _ACTUAL_REF_FIELD_NAMES
    extra = field_names - _FROZEN_REF_FIELD_NAMES
    if extra:
        raise MacpSessionError(
            "CommitmentRef carries field(s) outside the RFC-MACP-0013 §5 "
            f"frozen two-field set and is not hashable under label {LABEL!r}: "
            f"{sorted(extra)}"
        )


def _check_no_unknown_wire_fields(payload: core_pb2.CommitmentPayload) -> None:
    """Raise if ``payload`` carries wire data for a field number the
    installed schema does not recognize at all (an "unknown field").

    `_check_frozen_field_set` only catches schema drift: a *newer* installed
    ``macp-proto`` whose ``CommitmentPayload.DESCRIPTOR`` has grown a 10th
    field. It cannot see this case: a peer sent bytes for a field number the
    *local* schema has never heard of, so protobuf parses the message
    successfully and stashes those bytes as an "unknown field" on the
    instance, invisible to ``DESCRIPTOR.fields`` (RFC-MACP-0013 §5). Left
    unchecked, `canonical_projection` would silently hash only the known
    fields and drop the unknown field's contribution -- exactly the
    "not hashable under this label ... never silently ignored" outcome the
    RFC prohibits.

    Classic ``payload.UnknownFields()`` raises ``NotImplementedError`` under
    the ``upb`` backend (the default fast C-extension protobuf backend), so
    this uses ``google.protobuf.unknown_fields.UnknownFieldSet``, the public
    upb-safe replacement, instead.

    This only inspects ``payload`` itself, not the nested ``supersedes``
    submessage -- see `_check_no_unknown_ref_wire_fields` for the equivalent
    check one level down.
    """
    unknown = unknown_fields.UnknownFieldSet(payload)
    if len(unknown) > 0:
        field_numbers = sorted({f.field_number for f in unknown})
        raise MacpSessionError(
            "CommitmentPayload carries wire data for unrecognized field "
            "number(s) outside the RFC-MACP-0013 §5 frozen nine-field set "
            f"and is not hashable under label {LABEL!r}: {field_numbers}"
        )


def _check_no_unknown_ref_wire_fields(ref: core_pb2.CommitmentRef) -> None:
    """Raise if ``ref`` (a ``supersedes`` submessage) carries wire data for a
    field number the installed schema does not recognize at all.

    Mirrors `_check_no_unknown_wire_fields` one level down: a peer may send a
    ``CommitmentRef`` with wire data for a field number outside the
    RFC-MACP-0013 §5 frozen two-field set (``session_id``=1,
    ``commitment_hash``=2). `_check_frozen_ref_field_set` only catches schema
    drift (a newer installed ``macp-proto``'s ``CommitmentRef.DESCRIPTOR``
    growing a 3rd field); it cannot see wire data for a field number the
    *local* schema has never heard of at all, which protobuf parses
    successfully and stashes as an "unknown field", invisible to
    ``DESCRIPTOR.fields``. Left unchecked, `_supersedes_member` would
    silently hash only ``session_id``/``commitment_hash`` and drop the
    unknown field's contribution -- the same "not hashable under this label
    ... never silently ignored" outcome the RFC prohibits, just nested one
    level deeper.
    """
    unknown = unknown_fields.UnknownFieldSet(ref)
    if len(unknown) > 0:
        field_numbers = sorted({f.field_number for f in unknown})
        raise MacpSessionError(
            "CommitmentPayload.supersedes carries wire data for unrecognized "
            "field number(s) outside the RFC-MACP-0013 §5 frozen two-field "
            f"set and is not hashable under label {LABEL!r}: {field_numbers}"
        )


def canonical_projection(payload: core_pb2.CommitmentPayload) -> bytes:
    """Return the JCS-canonicalized UTF-8 bytes of the Section 3 projection.

    This is intermediate value ``C`` of RFC-MACP-0013 §4 step 2 -- the
    canonicalized projection, not the full domain-separated preimage.

    Raises `MacpSessionError` if the installed proto's `CommitmentPayload`
    carries a field outside the RFC-MACP-0013 §5 frozen nine-field set (see
    `_check_frozen_field_set`), or if this particular message instance
    carries wire data for a field number the installed schema does not
    recognize at all (see `_check_no_unknown_wire_fields`). When `supersedes`
    is set, the same two checks are additionally applied one level down to
    the nested `CommitmentRef` (see `_check_frozen_ref_field_set` and
    `_check_no_unknown_ref_wire_fields`) -- an absent `supersedes` has no
    `CommitmentRef` to check, so those two are skipped in that case.
    """
    _check_frozen_field_set()
    _check_no_unknown_wire_fields(payload)
    if payload.HasField("supersedes"):
        _check_frozen_ref_field_set()
        _check_no_unknown_ref_wire_fields(payload.supersedes)
    members: list[tuple[str, str]] = [
        ("action", _escape_json_string(payload.action)),
        ("authority_scope", _escape_json_string(payload.authority_scope)),
        ("commitment_id", _escape_json_string(payload.commitment_id)),
        ("configuration_version", _escape_json_string(payload.configuration_version)),
        ("mode_version", _escape_json_string(payload.mode_version)),
        ("outcome_positive", _json_bool(payload.outcome_positive)),
        ("policy_version", _escape_json_string(payload.policy_version)),
        ("reason", _escape_json_string(payload.reason)),
    ]
    # Message field: omit entirely when unset (RFC-MACP-0013 §3 rule 3).
    # HasField, not a truthiness check -- an empty CommitmentRef ({} with
    # both sub-fields "") is falsy-looking but MUST still be projected when
    # explicitly set, distinct from `supersedes` being absent altogether.
    if payload.HasField("supersedes"):
        members.append(("supersedes", _supersedes_member(payload.supersedes)))
        # Top-level members are already in lexicographic key order except for
        # "supersedes", which we appended last; re-sort to be explicit and
        # robust regardless of append order above.
        members.sort(key=lambda kv: kv[0])

    body = ",".join(f"{_escape_json_string(k)}:{v}" for k, v in members)
    text = "{" + body + "}"
    return text.encode("utf-8")


def commitment_hash(payload: core_pb2.CommitmentPayload) -> str:
    """Compute the canonical commitment hash of ``payload`` (RFC-MACP-0013 §4).

    Accepts any ``CommitmentPayload``, including one that is not
    well-formed under RFC-MACP-0001 §7.3.1 (e.g. all-empty fields, or a
    ``supersedes`` with empty sub-fields) -- hashing is a pure function of
    the projected field values and MUST NOT be gated on validity
    (RFC-MACP-0013 §6, "hashability"). Do not add a validation call here.
    """
    # No guard against an older macp-proto lacking `outcome_positive` /
    # `supersedes`: the repo's pin is macp-proto>=0.1.6 and both fields have
    # existed since well before that, so the fields are always present on
    # the installed descriptor. Accessing a genuinely absent field would
    # raise AttributeError from the generated proto class itself rather
    # than silently under-projecting, so no additional guard is needed here.
    preimage = LABEL.encode("ascii") + b":" + canonical_projection(payload)
    digest = hashlib.sha256(preimage).hexdigest()
    return f"sha256:{digest}"


def is_canonical_commitment_hash(value: str) -> bool:
    """Syntax predicate: ``^sha256:[0-9a-f]{64}$`` exactly (full match), no I/O.

    Uses `re.fullmatch` rather than `match` + a trailing-``$`` pattern: `$`
    alone matches immediately before a trailing ``\\n``, which would wrongly
    accept ``"sha256:" + "a" * 64 + "\\n"``. `fullmatch` anchors both ends
    with no such quirk, so no separate `strip()` check is needed.
    """
    return bool(_HASH_RE.fullmatch(value))
