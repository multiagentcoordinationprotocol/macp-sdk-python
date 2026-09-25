"""Centralized protobuf encode/decode registry for MACP message types.

Uses compiled ``_pb2`` modules (via ``google.protobuf.symbol_database``) to
look up message classes by fully-qualified type name.
"""

from __future__ import annotations

import base64
import importlib
import json
from typing import Any

from google.protobuf import json_format, symbol_database  # type: ignore[import-untyped]

from .constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_MULTI_ROUND,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)

# ── Type-name mappings (mirrors TypeScript CORE_MAP / MODE_MAP) ──────

CORE_MAP: dict[str, str] = {
    "SessionStart": "macp.v1.SessionStartPayload",
    "Commitment": "macp.v1.CommitmentPayload",
    "Signal": "macp.v1.SignalPayload",
    "Progress": "macp.v1.ProgressPayload",
}

MODE_MAP: dict[str, dict[str, str]] = {
    MODE_DECISION: {
        "Proposal": "macp.modes.decision.v1.ProposalPayload",
        "Evaluation": "macp.modes.decision.v1.EvaluationPayload",
        "Objection": "macp.modes.decision.v1.ObjectionPayload",
        "Vote": "macp.modes.decision.v1.VotePayload",
    },
    MODE_PROPOSAL: {
        "Proposal": "macp.modes.proposal.v1.ProposalPayload",
        "CounterProposal": "macp.modes.proposal.v1.CounterProposalPayload",
        "Accept": "macp.modes.proposal.v1.AcceptPayload",
        "Reject": "macp.modes.proposal.v1.RejectPayload",
        "Withdraw": "macp.modes.proposal.v1.WithdrawPayload",
    },
    MODE_TASK: {
        "TaskRequest": "macp.modes.task.v1.TaskRequestPayload",
        "TaskAccept": "macp.modes.task.v1.TaskAcceptPayload",
        "TaskReject": "macp.modes.task.v1.TaskRejectPayload",
        "TaskUpdate": "macp.modes.task.v1.TaskUpdatePayload",
        "TaskComplete": "macp.modes.task.v1.TaskCompletePayload",
        "TaskFail": "macp.modes.task.v1.TaskFailPayload",
    },
    MODE_HANDOFF: {
        "HandoffOffer": "macp.modes.handoff.v1.HandoffOfferPayload",
        "HandoffContext": "macp.modes.handoff.v1.HandoffContextPayload",
        "HandoffAccept": "macp.modes.handoff.v1.HandoffAcceptPayload",
        "HandoffDecline": "macp.modes.handoff.v1.HandoffDeclinePayload",
    },
    MODE_QUORUM: {
        "ApprovalRequest": "macp.modes.quorum.v1.ApprovalRequestPayload",
        "Approve": "macp.modes.quorum.v1.ApprovePayload",
        "Reject": "macp.modes.quorum.v1.RejectPayload",
        "Abstain": "macp.modes.quorum.v1.AbstainPayload",
    },
    MODE_MULTI_ROUND: {
        # Runtime v0.5.0 / macp-proto >= 0.1.4: Contribute is the last
        # advertised mode moved onto the canonical protobuf wire format.
        # Legacy JSON (``{"value": "..."}``) is still decoded (tried first,
        # permanently) — see ``decode_known_payload``.
        "Contribute": "macp.modes.multi_round.v1.ContributePayload",
    },
}

# Ensure all _pb2 modules are imported so descriptors are registered.
_PB2_MODULES_LOADED = False


def _ensure_pb2_imports() -> None:
    global _PB2_MODULES_LOADED
    if _PB2_MODULES_LOADED:
        return
    # Import all proto modules to register their descriptors in the global pool.
    for _mod in (
        "macp.v1.core_pb2",
        "macp.v1.envelope_pb2",
        "macp.v1.policy_pb2",
        "macp.modes.decision.v1.decision_pb2",
        "macp.modes.proposal.v1.proposal_pb2",
        "macp.modes.task.v1.task_pb2",
        "macp.modes.handoff.v1.handoff_pb2",
        "macp.modes.quorum.v1.quorum_pb2",
        "macp.modes.multi_round.v1.multi_round_pb2",
    ):
        importlib.import_module(_mod)
    _PB2_MODULES_LOADED = True


class ProtoRegistry:
    """Registry for protobuf type-name-based encode/decode of MACP payloads."""

    def __init__(self) -> None:
        _ensure_pb2_imports()
        self._db = symbol_database.Default()

    def get_known_type_name(self, mode: str, message_type: str) -> str | None:
        """Return the fully-qualified protobuf type name, or None if unknown."""
        return MODE_MAP.get(mode, {}).get(message_type) or CORE_MAP.get(message_type)

    def encode_message(self, type_name: str, value: dict[str, Any]) -> bytes:
        """Encode *value* as a protobuf message identified by *type_name*."""
        cls = self._db.GetSymbol(type_name)
        msg = json_format.ParseDict(value, cls())
        return msg.SerializeToString()

    def decode_message(self, type_name: str, payload: bytes) -> dict[str, Any]:
        """Decode *payload* into a dict using the message class for *type_name*."""
        cls = self._db.GetSymbol(type_name)
        msg = cls()
        msg.ParseFromString(payload)
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)

    def encode_known_payload(self, mode: str, message_type: str, value: dict[str, Any]) -> bytes:
        """Encode *value* using the known type mapping for *mode*/*message_type*."""
        type_name = self.get_known_type_name(mode, message_type)
        if type_name is None:
            raise ValueError(f"unknown payload mapping for {mode}/{message_type}")
        if type_name == "__json__":
            return json.dumps(value).encode("utf-8")
        return self.encode_message(type_name, value)

    def decode_known_payload(
        self, mode: str, message_type: str, payload: bytes
    ) -> dict[str, Any] | None:
        """Decode *payload* using the known type mapping, or try UTF-8 fallback."""
        type_name = self.get_known_type_name(mode, message_type)
        if type_name is None or type_name == "__json__":
            return self._try_decode_utf8(payload)
        if mode == MODE_MULTI_ROUND and message_type == "Contribute":
            # RFC-MACP contract: multi_round Contribute accepts legacy JSON
            # (``{"value": "..."}``) *permanently* and tries it first, so
            # pre-proto histories/replays decode byte-identically. See
            # ``_decode_json_first_then_proto`` for why a canonical-proto
            # tie-break is required on top of that (issue #69).
            return self._decode_json_first_then_proto(type_name, payload)
        return self.decode_message(type_name, payload)

    def _decode_json_first_then_proto(
        self, type_name: str, payload: bytes
    ) -> dict[str, Any] | None:
        """Decode a multi_round ``Contribute`` payload, JSON tried first.

        **Empty payload (``b""``) decodes to ``None`` -- deliberately, and this
        is not an oversight.** There is no decode-layer rejection of an empty
        Contribute value, and none is added here. The runtime is the sole
        acceptance gate for this mode and it already rejects empty payloads,
        precisely because canonical proto3 cannot distinguish an absent
        ``value`` from an explicit empty string (no field presence on a bare
        ``string value = 1;`` -- see
        ``macp-runtime/crates/macp-modes/src/mode/multi_round.rs:67-69``,
        surfaced as ``MacpError::InvalidPayload`` -> wire code
        ``INVALID_ENVELOPE`` via
        ``macp-runtime/crates/macp-core/src/error.rs:66``). That reject *is*
        the mitigation for the round-trip hole this creates on the encode
        side (see ``build_contribute_payload``'s docstring) -- adding a
        decode-layer raise here would duplicate a gate that already exists
        downstream, on a call whose result no in-repo caller currently reaches unguarded
        (``agent/transports.py`` only calls this behind an
        ``if envelope.payload:`` check). ``None`` is also this file's existing
        "nothing decodable" sentinel, shared with ``_try_decode_utf8``.

        **Why JSON is tried first, and why a byte-identical round-trip alone
        is not enough to call something proto.** This always attempts a real
        JSON parse first -- it does not sniff the leading byte (``0x7B`` vs.
        ``0x0A``) to pick a branch. That matters because a first-byte
        shortcut does not generalize to leading whitespace before legacy
        JSON (insignificant per the JSON grammar, and stripped transparently
        by ``json.loads``); parse-then-fallback handles it for free. A
        non-string ``value`` inside valid JSON is likewise accepted
        uninterpreted -- this layer decodes, it doesn't validate
        business-level shape.

        A successful JSON parse is *not* proof the bytes are legacy JSON,
        because the canonical proto tag byte for field 1 (``0x0A``) is
        itself insignificant JSON whitespace. Two distinct mechanisms follow
        from that, both real and independently reachable from this SDK's own
        encoder (exhaustively swept, value lengths 1-127):

        1. **The length varint is a significant JSON opener.** When the
           single-byte length varint is ``-``, a digit, ``"`` or ``[``, the
           value's own bytes continue that literal as a JSON number, string
           or array. Concretely: lengths 45 and 49-57 (all-digit values),
           48 when the value supplies a decimal point, 34 (a quoted
           string-shaped value), and 91 (an array-shaped value).
        2. **The length varint is itself JSON whitespace.** JSON whitespace
           is exactly ``0x09``, ``0x0A``, ``0x0D``, ``0x20`` -- lengths 9,
           10, 13 and 32 -- which makes *both* leading bytes insignificant
           and hands the opening character to the **value's own first
           byte**. An object-shaped value then parses as that JSON object.
           At length 123 the length varint is ``0x7B`` = ``{`` itself, which
           is mechanism 2's most naturally-reachable instance: any ordinary
           123-byte value shaped to complete that opened object (e.g.
           ``"value":"..."}``, so the full bytes read as ``{"value":"..."}``)
           collides.

        Every length >= 128 is immune, though not for one uniform reason: the
        two-byte length varint's first byte is ``0x80 | (length & 0x7F)``
        (range ``0x80``-``0xFF``) and its second byte is ``length >> 7``
        (range ``0x01``-``0x7F``, i.e. plain ASCII, never a continuation
        byte). When the first byte itself falls in ``0x80``-``0xBF`` it is a
        bare UTF-8 continuation byte and is invalid as a sequence start on
        its own; when it instead falls in ``0xC0``-``0xFF`` (a byte that
        *would* open a valid multi-byte UTF-8 sequence), the second byte's
        exclusively-ASCII range means it can never be a legal continuation
        byte, so the sequence is still malformed. Either way ``payload.decode
        ("utf-8")`` raises before either mechanism can apply (verified for
        every length 128-999).

        ``_is_canonical_proto`` resolves both mechanisms by tie-break rather
        than by refusing to try JSON: bytes that parse as JSON *and* are the
        exact canonical proto encoding (no unknown fields once discarded)
        are treated as proto. Verified exhaustively: this closes every
        forward-direction case above (mechanisms 1 and 2, including length
        123 -- see
        ``test_canonical_proto_length_123_collision_is_resolved_toward_proto``),
        leaving zero corrupting lengths in 1-127. ``macp-runtime`` has no
        equivalent tie-break and mis-decodes canonical proto at value
        lengths 13, 32 and 123 today (mechanism 2 on its own
        ``parse_contribute_value``,
        ``macp-runtime/crates/macp-modes/src/mode/multi_round.rs:70-77``);
        its docstring's claim "a proto payload never parses as a JSON
        object" (``:55-56``) is therefore false, not merely imprecise.

        The tie-break also has a cost on the *reverse* direction -- legacy
        JSON misread as proto -- that it introduces rather than merely fails
        to close: without any tie-break (this file's pre-fix behavior, and
        still the behavior under a bare ``isinstance(parsed, dict)`` guard),
        a payload starting with a literal ``0x0A`` followed by real legacy
        JSON always decoded correctly, because nothing re-examined a
        successful dict parse. This tie-break creates the one case where
        that stops holding: when the remainder also happens to form a
        complete, well-formed proto field-1 string, so there is no unknown
        field left for ``DiscardUnknownFields`` to expose a mismatch on. See
        ``_is_canonical_proto``'s own docstring for that case and why it is
        the right trade regardless; it is the symmetric counterpart of the
        length-123 case above, just genuinely irreducible once the
        tie-break is adopted, rather than resolved.

        One structural asymmetry is worth naming: whitespace *before* legacy
        JSON is handled for free, because ``json.loads`` skips leading
        whitespace natively. Whitespace before proto bytes is not
        recoverable the same way -- those bytes are simply not a valid
        ``ContributePayload``, so they correctly raise rather than silently
        losing data.
        """
        if not payload:
            return None
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self.decode_message(type_name, payload)
        if self._is_canonical_proto(type_name, payload):
            return self.decode_message(type_name, payload)
        # Legacy JSON payload — keep the ``{"encoding": "json", "json": ...}``
        # shape that existing consumers of decoded dicts already handle.
        return {"encoding": "json", "json": parsed}

    def _is_canonical_proto(self, type_name: str, payload: bytes) -> bool:
        """True iff *payload* is the exact canonical proto encoding of *type_name*.

        Used to break the JSON/proto ambiguity in
        ``_decode_json_first_then_proto``: bytes that both parse as JSON
        *and* round-trip byte-identically through the proto message, with
        no unknown fields, are treated as proto, not JSON.

        ``DiscardUnknownFields`` matters: the Python protobuf runtime
        preserves unknown fields verbatim through
        ``ParseFromString``/``SerializeToString`` by default, so a plain
        round-trip check is *not* proof of canonicality -- it is also true
        for arbitrary well-formed-but-foreign byte strings that merely
        happen to parse as *some* valid protobuf wire format. Dropping
        unknown fields first closes that false-positive class entirely
        (verified: every JSON-whitespace-prefixed legacy payload up to
        length 300 that isn't a genuine one-field collision).

        Choosing to run this tie-break at all has a narrow, priced cost, and
        it is important not to describe it as a pre-existing bug this check
        merely fails to close: without any canonicality check (this file's
        behavior before this fix, and still the behavior under a bare
        ``isinstance(parsed, dict)`` guard with no tie-break), a payload
        whose first byte is a literal ``0x0A`` (``\\n``) -- legacy-JSON-valid,
        insignificant leading whitespace -- followed by real legacy JSON
        decodes *correctly* as that JSON, every time, because nothing ever
        re-examines a successful dict-shaped parse. Adding this
        canonicality tie-break is what creates the one case where that
        stops being true: when the remaining bytes *also* happen to form a
        complete, well-formed proto field-1 string with no leftover (the
        leading ``0x0A`` doubles as field 1's own tag byte, so there is
        nothing unknown for ``DiscardUnknownFields`` to strip and expose as
        a mismatch). That byte string is genuinely, symmetrically
        ambiguous -- simultaneously a legal JSON reading and the canonical
        proto encoding of some (possibly nonsensical) string -- and once
        the tie-break is in place, no cheap additional check resolves it
        without reintroducing the tie-break's own blind spot. It is the
        mirror image of the *forward*-direction collision at value length
        123 (whose length byte is ``0x7B`` = ``{``, so a genuine proto
        value can also parse as a JSON object), but the two do **not**
        trade symmetrically: the forward case is a real, naturally-reachable
        corruption of an ordinary proto-encoded value that this tie-break
        *fixes* (see
        ``test_canonical_proto_length_123_collision_is_resolved_toward_proto``),
        while the reverse case this tie-break *creates* requires a
        deliberately newline-prefixed legacy JSON payload that no known
        encoder (including this SDK's own) emits. That asymmetry -- fixing
        an ordinary corruption at the cost of an adversarial one -- is why
        the tie-break is still the right call, not a wash. See
        ``TestMultiRoundContribute`` for characterization tests pinning both
        outcomes.
        """
        try:
            cls = self._db.GetSymbol(type_name)
            msg = cls()
            msg.ParseFromString(payload)
            msg.DiscardUnknownFields()
            return msg.SerializeToString() == payload
        except Exception:
            return False

    @staticmethod
    def _try_decode_utf8(payload: bytes) -> dict[str, Any] | None:
        if not payload:
            return None
        text = payload.decode("utf-8")
        try:
            return {"encoding": "json", "json": json.loads(text)}
        except (json.JSONDecodeError, ValueError):
            return {
                "encoding": "text",
                "text": text,
                "payload_base64": base64.b64encode(payload).decode(),
            }
