"""Session helper and projection for the ``ext.multi_round.v1`` extension mode.

The mode's normative semantics live in a comment block in
``macp/modes/multi_round/v1/multi_round.proto`` (spec #241); this module is
client sugar over them and mirrors macp-sdk-typescript's ``multi-round``
module name for name. Declared participants ``Contribute`` string values
until every latest value is equal (``all_equal``, sticky); the session then
stays OPEN until its initiator commits. The runtime decides: the projection's
convergence is advisory, and nothing here evaluates governance policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from macp.v1 import core_pb2, envelope_pb2

from .async_session import AsyncBaseSession
from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession, _SessionCore
from .constants import MODE_MULTI_ROUND
from .envelope import build_commitment_payload, build_contribute_payload
from .errors import MacpSessionError
from .validation import validate_required_field

__all__ = [
    "MULTI_ROUND_CONVERGED_ACTION",
    "MULTI_ROUND_MODE_VERSION",
    "AsyncMultiRoundSession",
    "MultiRoundProjection",
    "MultiRoundSession",
]

MULTI_ROUND_MODE_VERSION = "1.0.0"
"""The only ``mode_version`` the runtime accepts for this mode."""

MULTI_ROUND_CONVERGED_ACTION = "multi_round.converged"
"""The conventional Commitment action; it matches no outcome suffix."""

# RFC-MACP-0001 §7.3 as macp-runtime enforces it (``mode/util.rs``
# ``validate_outcome_positive``): a case-sensitive suffix match.
_NEGATIVE_SUFFIXES = ("rejected", "failed", "declined")
_POSITIVE_SUFFIXES = ("selected", "accepted", "completed", "approved")


class _Malformed(Exception):  # noqa: N818 - internal control flow, never raised out
    """The bytes are not what the runtime's decoder accepts."""


# ── prost 0.14 (``ContributePayload::decode``) ───────────────────────────
# google.protobuf is more lenient than prost: it skips a field-1 varint, an
# unmatched end group or nesting past prost's limit as unknown data. Each of
# those makes the runtime refuse the Contribute, so the wire format is read
# by hand, following prost's ``decode_key``/``skip_field``.

_PROST_RECURSION_LIMIT = 100


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for count in range(10):
        if pos >= len(buf):
            raise _Malformed("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * count)
        if byte < 0x80:
            if count == 9 and byte >= 0x02:
                raise _Malformed("varint overflows u64")
            return value, pos
    raise _Malformed("varint longer than 10 bytes")


def _key(buf: bytes, pos: int) -> tuple[int, int, int]:
    key, pos = _varint(buf, pos)
    if key > 0xFFFFFFFF or key & 7 > 5 or key >> 3 < 1:
        raise _Malformed("invalid key")
    return key >> 3, key & 7, pos


def _skip(buf: bytes, pos: int, tag: int, wire_type: int, depth: int) -> int:
    if depth == 0:
        raise _Malformed("recursion limit")
    if wire_type == 0:
        return _varint(buf, pos)[1]
    if wire_type == 2:
        length, pos = _varint(buf, pos)
    elif wire_type == 1:
        length = 8
    elif wire_type == 5:
        length = 4
    elif wire_type == 3:
        while True:
            inner_tag, inner_type, pos = _key(buf, pos)
            if inner_type == 4:
                if inner_tag != tag:
                    raise _Malformed("unexpected end group")
                return pos
            pos = _skip(buf, pos, inner_tag, inner_type, depth - 1)
    else:
        raise _Malformed("unexpected end group")
    if length > len(buf) - pos:
        raise _Malformed("buffer underflow")
    return pos + length


def _proto_value(buf: bytes) -> str | None:
    value = ""
    pos = 0
    try:
        while pos < len(buf):
            tag, wire_type, pos = _key(buf, pos)
            if tag != 1:
                pos = _skip(buf, pos, tag, wire_type, _PROST_RECURSION_LIMIT)
                continue
            if wire_type != 2:
                raise _Malformed("value has the wrong wire type")
            length, pos = _varint(buf, pos)
            if length > len(buf) - pos:
                raise _Malformed("buffer underflow")
            value = buf[pos : pos + length].decode("utf-8")  # the last occurrence wins
            pos += length
    except (_Malformed, UnicodeDecodeError):
        return None
    return value


def _canonical_proto(value: str) -> bytes:
    raw = value.encode("utf-8")
    if not raw:
        return b""
    length, out = len(raw), bytearray(b"\x0a")
    while length >= 0x80:
        out.append(length & 0x7F | 0x80)
        length >>= 7
    out.append(length)
    return bytes(out) + raw


# ── serde_json 1 (``from_str::<ContributeJson>``) ────────────────────────
# Python's json module differs from serde for this struct, so the text is
# scanned by hand. serde reads the struct from an object or from a
# one-element array. It decodes, and so surrogate-checks, only the top-level
# keys and the ``value`` string. Every other member is skipped with
# ``ignore_value``: syntax-checked, never decoded, no depth limit. Only a
# second ``value`` key is a duplicate.

_WS = " \t\n\r"
_SIMPLE_ESCAPES = '"\\/bfnrt'
_HEX = "0123456789abcdefABCDEF"
_LITERALS = ("true", "false", "null")


def _ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in _WS:
        pos += 1
    return pos


def _expect(text: str, pos: int, char: str) -> int:
    if pos >= len(text) or text[pos] != char:
        raise _Malformed(f"expected {char!r}")
    return pos + 1


def _hex4(text: str, pos: int) -> tuple[int, int]:
    digits = text[pos : pos + 4]
    if len(digits) != 4 or any(c not in _HEX for c in digits):
        raise _Malformed("invalid \\u escape")
    return int(digits, 16), pos + 4


def _string(text: str, pos: int, *, decode: bool) -> tuple[str, int]:
    """Scan a string whose opening quote is at *pos*; decoded only if asked."""
    pos = _expect(text, pos, '"')
    out: list[str] = []
    while True:
        if pos >= len(text):
            raise _Malformed("unterminated string")
        char = text[pos]
        pos += 1
        if char == '"':
            return "".join(out), pos
        if char < " ":
            raise _Malformed("control character in string")
        if char != "\\":
            out.append(char)
            continue
        if pos >= len(text):
            raise _Malformed("unterminated escape")
        esc = text[pos]
        pos += 1
        if esc in _SIMPLE_ESCAPES:
            out.append('"\\/\b\f\n\r\t'[_SIMPLE_ESCAPES.index(esc)])
        elif esc != "u":
            raise _Malformed("invalid escape")
        else:
            code, pos = _hex4(text, pos)
            if decode and 0xDC00 <= code <= 0xDFFF:
                raise _Malformed("lone trailing surrogate")
            if decode and 0xD800 <= code <= 0xDBFF:
                if text[pos : pos + 2] != "\\u":
                    raise _Malformed("lone leading surrogate")
                low, pos = _hex4(text, pos + 2)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise _Malformed("lone leading surrogate")
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            out.append(chr(code))


def _number(text: str, pos: int) -> int:
    def digits(at: int) -> int:
        start = at
        while at < len(text) and text[at].isascii() and text[at].isdigit():
            at += 1
        if at == start:
            raise _Malformed("expected a digit")
        return at

    if text.startswith("-", pos):
        pos += 1
    if text.startswith("0", pos):
        pos += 1
        if pos < len(text) and text[pos].isascii() and text[pos].isdigit():
            raise _Malformed("leading zero")
    else:
        pos = digits(pos)
    if text.startswith(".", pos):
        pos = digits(pos + 1)
    if pos < len(text) and text[pos] in "eE":
        pos += 1
        if pos < len(text) and text[pos] in "+-":
            pos += 1
        pos = digits(pos)
    return pos


def _ignore_value(text: str, pos: int) -> int:
    """Skip one JSON value iteratively, as serde's ``ignore_value`` does."""
    closers: list[str] = []
    while True:
        pos = _ws(text, pos)
        char = text[pos] if pos < len(text) else ""
        if char and char in "[{":
            closers.append("]" if char == "[" else "}")
            pos = _ws(text, pos + 1)
            if text.startswith(closers[-1], pos):
                pos += 1
                closers.pop()
            elif char == "{":
                pos = _expect(text, _ws(text, _string(text, pos, decode=False)[1]), ":")
                continue
            else:
                continue
        elif char == '"':
            pos = _string(text, pos, decode=False)[1]
        elif char == "-" or (char.isascii() and char.isdigit()):
            pos = _number(text, pos)
        else:
            literal = next((w for w in _LITERALS if text.startswith(w, pos)), None)
            if literal is None:
                raise _Malformed("expected a value")
            pos += len(literal)
        # After a value: close containers or move to the next member.
        while closers:
            pos = _ws(text, pos)
            if text.startswith(closers[-1], pos):
                pos += 1
                closers.pop()
                continue
            pos = _ws(text, _expect(text, pos, ","))
            if text.startswith(closers[-1], pos):
                raise _Malformed("trailing comma")
            if closers[-1] == "}":
                pos = _expect(text, _ws(text, _string(text, pos, decode=False)[1]), ":")
            break
        if not closers:
            return pos


def _json_value(payload: bytes) -> str | None:
    """``value`` exactly as serde reads legacy ``ContributeJson``, else ``None``."""
    try:
        text = payload.decode("utf-8")
        pos = _ws(text, 0)
        value: str | None = None
        if text.startswith("[", pos):
            value, pos = _string(text, _ws(text, pos + 1), decode=True)
            pos = _expect(text, _ws(text, pos), "]")
        else:
            pos = _ws(text, _expect(text, pos, "{"))
            while not text.startswith("}", pos):
                key, pos = _string(text, pos, decode=True)
                pos = _ws(text, _expect(text, _ws(text, pos), ":"))
                if key != "value":
                    pos = _ignore_value(text, pos)
                elif value is not None:
                    raise _Malformed("duplicate field value")
                else:
                    value, pos = _string(text, pos, decode=True)
                pos = _ws(text, pos)
                if text.startswith(",", pos):
                    pos = _ws(text, pos + 1)
                    if text.startswith("}", pos):
                        raise _Malformed("trailing comma")
                elif not text.startswith("}", pos):
                    raise _Malformed("expected ',' or '}'")
            pos += 1
        if _ws(text, pos) != len(text):
            raise _Malformed("trailing characters")
    except (_Malformed, UnicodeDecodeError):
        return None
    return value


def _contribute_value(payload: bytes) -> str | None:
    """The contributed value, or ``None`` when the runtime would refuse the bytes.

    Mirrors macp-runtime's ``parse_contribute_value`` at ``semantics_rev`` 3:
    legacy JSON (serde's reading of ``{"value": <string>}``) wins unless the
    bytes are also the canonical protobuf encoding of what prost decodes;
    otherwise prost decides. A non-empty payload can decode to ``""``.
    ``None`` covers an empty payload and bytes neither decoder accepts:
    the proto lets a history reader treat them as "nothing decodable".
    """
    decided = _contribute_decision(payload)
    return None if decided is None else decided[1]


def _contribute_decision(payload: bytes) -> tuple[str, str] | None:
    """``("json" | "proto", value)`` as the runtime decides it, else ``None``."""
    if not payload:
        return None
    from_proto = _proto_value(payload)
    from_json = _json_value(payload)
    if from_json is not None and (from_proto is None or _canonical_proto(from_proto) != payload):
        return "json", from_json
    return None if from_proto is None else ("proto", from_proto)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class MultiRoundProjection(BaseProjection):
    """In-process state tracking for ``ext.multi_round.v1`` sessions.

    Mirrors the runtime's ``handle_contribute``: a sender's first value, or a
    value that differs from its previous one, advances ``round`` by one;
    re-sending the same value changes nothing. The session has converged once
    every declared participant has contributed and all latest values are
    equal; convergence is sticky, and later Contributes are no-ops.

    ``participants`` and ``initiator`` are learned from the accepted
    SessionStart. Pass them to the constructor to seed a projection that never
    sees one (a participant joining mid-session); a later SessionStart
    overwrites both. Convergence is evaluated only once participants are
    known.

    Phases: ``"Pending"`` → ``"Contributing"`` (SessionStart) →
    ``"Converged"`` → ``"Committed"``.

    A session's own projection is fed the envelopes the session built, so it
    attributes them only when the sender resolves locally: pass ``sender=`` or
    give a bearer ``AuthConfig`` a ``sender_hint``. With neither, the runtime
    still accepts the messages (it stamps the authenticated sender), but this
    projection drops the unattributed Contributes as non-participant ones.
    Subscribing to the session stream does not have this limit.

    Limitation: the projection cannot see a session's ``semantics_rev``, so a
    session the runtime recorded below revision 3 that carried a canonical
    proto value of byte length 13, 32 or 123 decodes here under the
    tie-break, not the JSON-first reading it was accepted with.
    """

    MODE = MODE_MULTI_ROUND

    def __init__(
        self,
        *,
        participants: Sequence[str] | None = None,
        initiator: str | None = None,
    ) -> None:
        super().__init__()
        self.phase = "Pending"
        self.participants: list[str] = list(participants or [])
        self.initiator: str | None = initiator
        self.contributions: dict[str, str] = {}
        self.round = 0
        self.converged = False
        self.converged_value: str | None = None

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "SessionStart":
            p = core_pb2.SessionStartPayload()
            p.ParseFromString(envelope.payload)
            self.participants = list(p.participants)
            # A locally built envelope under bearer auth without a sender hint
            # has an empty sender; the runtime stamps the real one only on its
            # copy, so keep what is known.
            if envelope.sender:
                self.initiator = envelope.sender
            self._set_phase("Contributing")
            return

        if mt == "Contribute":
            value = _contribute_value(envelope.payload)
            if value is None or self.converged:
                return
            # authorize_sender: only a declared participant may contribute.
            if self.participants and envelope.sender not in self.participants:
                return
            if self.contributions.get(envelope.sender) != value:
                self.round += 1
                self.contributions[envelope.sender] = value
            self._check_convergence()

    def _check_convergence(self) -> None:
        if not self.participants:
            return
        if any(p not in self.contributions for p in self.participants):
            return
        values = {self.contributions[p] for p in self.participants}
        if len(values) == 1:
            self.converged = True
            self.converged_value = values.pop()
            self._set_phase("Converged")

    def is_participant(self, sender: str) -> bool:
        """Whether *sender* may contribute; ``True`` while participants are unknown."""
        return not self.participants or sender in self.participants


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class _MultiRoundActions(_SessionCore):
    """Envelope builders shared by :class:`MultiRoundSession` and
    :class:`AsyncMultiRoundSession`.

    Each builder refuses locally what the runtime would reject, as far as the
    local projection can tell, and returns the envelope to send.
    """

    MODE = MODE_MULTI_ROUND

    def _create_projection(self) -> BaseProjection:
        return MultiRoundProjection()

    def _multi_round_state(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    def _start_envelope(
        self,
        *,
        intent: str,
        participants: list[str],
        ttl_ms: int,
        context_id: str,
        extensions: Mapping[str, bytes] | None,
        roots: Iterable[Any] | None,
        max_suspend_ms: int,
        sender: str | None,
        auth: AuthConfig | None,
    ) -> envelope_pb2.Envelope:
        if self.mode_version != MULTI_ROUND_MODE_VERSION:
            raise MacpSessionError(
                f"ext.multi_round.v1 requires mode_version {MULTI_ROUND_MODE_VERSION!r}, "
                f"not {self.mode_version!r}"
            )
        return super()._start_envelope(
            intent=intent,
            participants=participants,
            ttl_ms=ttl_ms,
            context_id=context_id,
            extensions=extensions,
            roots=roots,
            max_suspend_ms=max_suspend_ms,
            sender=sender,
            auth=auth,
        )

    def _contribute_envelope(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        if not value:
            raise MacpSessionError(
                "contribution value must be non-empty: an empty value encodes to zero bytes, "
                "which the runtime rejects"
            )
        projection = self._multi_round_state()
        if projection.converged:
            raise MacpSessionError("the session has converged; the runtime rejects any Contribute")
        resolved = self._sender_for(sender, auth=auth)
        if resolved and not projection.is_participant(resolved):
            raise MacpSessionError(
                f"{resolved!r} is not a declared participant; the runtime rejects its Contribute"
            )
        return self._envelope(
            "Contribute", build_contribute_payload(value), sender=sender, auth=auth
        )

    def _multi_round_commit_envelope(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str,
        commitment_id: str | None,
        outcome_positive: bool | None,
        supersedes: core_pb2.CommitmentRef | None,
        sender: str | None,
        auth: AuthConfig | None,
    ) -> envelope_pb2.Envelope:
        initiator = self._multi_round_state().initiator
        resolved = self._sender_for(sender, auth=auth)
        if initiator and resolved and resolved != initiator:
            raise MacpSessionError(
                f"only the session initiator {initiator!r} may commit, not {resolved!r}"
            )
        if commitment_id is not None:
            validate_required_field("commitment_id", commitment_id)
        validate_required_field("action", action)
        validate_required_field("authority_scope", authority_scope)
        validate_required_field("reason", reason)
        if outcome_positive is not None:
            if outcome_positive and action.endswith(_NEGATIVE_SUFFIXES):
                raise MacpSessionError(f"action {action!r} has a negative suffix")
            if not outcome_positive and action.endswith(_POSITIVE_SUFFIXES):
                raise MacpSessionError(f"action {action!r} has a positive suffix")
        if supersedes is not None:
            validate_required_field("supersedes.session_id", supersedes.session_id)
        payload = build_commitment_payload(
            action=action,
            authority_scope=authority_scope,
            reason=reason,
            commitment_id=commitment_id,
            mode_version=self.mode_version,
            configuration_version=self.configuration_version,
            policy_version=self.policy_version,
            outcome_positive=outcome_positive,
            supersedes=supersedes,
        )
        return self._envelope("Commitment", payload, sender=sender, auth=auth)


class MultiRoundSession(BaseSession, _MultiRoundActions):
    """High-level helper for ``ext.multi_round.v1`` sessions.

    ``start()`` refuses a ``mode_version`` other than ``"1.0.0"``.
    ``contribute()`` refuses an empty value, a converged session and a sender
    the projection knows is not a declared participant. ``commit()`` defaults
    ``action`` to ``"multi_round.converged"`` and refuses a sender other than
    the known initiator; it does not wait for convergence, which the runtime
    checks.
    """

    MODE = MODE_MULTI_ROUND

    @property
    def multi_round_projection(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    def contribute(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Contribute *value* (compared for equality only, never interpreted)."""
        return self._send_and_track(
            self._contribute_envelope(value, sender=sender, auth=auth), auth=auth
        )

    def commit(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str = MULTI_ROUND_CONVERGED_ACTION,
        commitment_id: str | None = None,
        outcome_positive: bool | None = None,
        supersedes: core_pb2.CommitmentRef | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send the Commitment that resolves the session (initiator only)."""
        return self._send_and_track(
            self._multi_round_commit_envelope(
                authority_scope=authority_scope,
                reason=reason,
                action=action,
                commitment_id=commitment_id,
                outcome_positive=outcome_positive,
                supersedes=supersedes,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )


class AsyncMultiRoundSession(AsyncBaseSession, _MultiRoundActions):
    """Asyncio twin of :class:`MultiRoundSession`, driven by an
    :class:`~macp_sdk.async_client.AsyncMacpClient`.
    """

    MODE = MODE_MULTI_ROUND

    @property
    def multi_round_projection(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    async def contribute(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Contribute *value* (compared for equality only, never interpreted)."""
        return await self._send_and_track(
            self._contribute_envelope(value, sender=sender, auth=auth), auth=auth
        )

    async def commit(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str = MULTI_ROUND_CONVERGED_ACTION,
        commitment_id: str | None = None,
        outcome_positive: bool | None = None,
        supersedes: core_pb2.CommitmentRef | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send the Commitment that resolves the session (initiator only)."""
        return await self._send_and_track(
            self._multi_round_commit_envelope(
                authority_scope=authority_scope,
                reason=reason,
                action=action,
                commitment_id=commitment_id,
                outcome_positive=outcome_positive,
                supersedes=supersedes,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )
