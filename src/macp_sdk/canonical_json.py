"""Canonical JSON form of an Envelope and an Ack (RFC-MACP-0001 §10).

This is the JSON a transport binding carries, for example an A2A ``data`` Part
(RFC-MACP-0014). It is the proto3 JSON mapping with the §10.8 deviations:

- original (snake_case) field names only; a lowerCamelCase key is an unknown
  field and is ignored on decode;
- 64-bit integers as JSON numbers, quoted above 2^53-1 (§10.9); decode
  accepts both forms;
- every field without explicit presence is emitted, defaults included (§10.10);
- ``Envelope.timestamp_unix_ms`` becomes ``timestamp``, an RFC 3339 string:
  ``YYYY-MM-DDTHH:MM:SSZ`` when the milliseconds are 0, otherwise exactly three
  fraction digits, always ``Z``. Decode accepts any RFC 3339 offset and
  precision, truncated to the millisecond.

**Payload.** ``Envelope.payload`` becomes either ``payload`` (the payload
message as JSON) or ``payload_b64`` (standard padded base64; decode also takes
URL-safe and unpadded input). The form is picked by ``(mode, message_type)``
(§10.12):

- a Core message type (Signal, Progress, SessionStart, SessionCancel,
  SessionSuspend, SessionResume, Commitment) is always ``payload``, in any
  mode, including an empty-mode Signal;
- a mode-defined payload of a standard ``macp.mode.*`` mode is ``payload``
  when the registry knows it, otherwise ``payload_b64``;
- a mode-defined payload of an ``ext.*`` or reverse-domain mode is always
  ``payload_b64``.

``payload_encoding="b64"`` forces ``payload_b64`` for every payload.

**Exact bytes.** A JSON ``payload`` is converted back to protobuf bytes by this
module's own encoder, which follows every §10.13 rule (field-number order,
defaults omitted, packed repeated scalars, map entries sorted by the key's
UTF-8 bytes with key and value always written, canonical quiet NaN), because
protobuf's own deterministic mode does not. A payload carrying fields this SDK
does not know still loses them on the way to JSON, so a caller that needs the
original bytes (a commitment hash, a receipt's ``commitment_wire_b64``) sends
``payload_b64`` (RFC-MACP-0014 §5.3).

Every decode failure raises :class:`~macp_sdk.errors.CanonicalJsonError`
(``code == "INVALID_ENVELOPE"``). The module carries no transport.
"""

from __future__ import annotations

import base64
import calendar
import math
import re
import struct
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from google.protobuf import json_format, symbol_database  # type: ignore[import-untyped]
from google.protobuf.descriptor import Descriptor, FieldDescriptor  # type: ignore[import-untyped]
from google.protobuf.message import DecodeError, Message  # type: ignore[import-untyped]
from macp.v1 import core_pb2, envelope_pb2

from .constants import MODE_DECISION, MODE_HANDOFF, MODE_PROPOSAL, MODE_QUORUM, MODE_TASK
from .errors import CanonicalJsonError
from .proto_registry import ProtoRegistry

__all__ = [
    "ack_from_canonical_json",
    "ack_to_canonical_json",
    "envelope_from_canonical_json",
    "envelope_to_canonical_json",
]

#: Largest integer a JSON number carries exactly as an IEEE 754 double (§10.9).
MAX_SAFE_INTEGER = 2**53 - 1

_CORE_PAYLOADS: dict[str, type[Message]] = {
    "Signal": core_pb2.SignalPayload,
    "Progress": core_pb2.ProgressPayload,
    "SessionStart": core_pb2.SessionStartPayload,
    "SessionCancel": core_pb2.SessionCancelPayload,
    "SessionSuspend": core_pb2.SessionSuspendPayload,
    "SessionResume": core_pb2.SessionResumePayload,
    "Commitment": core_pb2.CommitmentPayload,
}

_STANDARD_MODES = frozenset({MODE_DECISION, MODE_PROPOSAL, MODE_TASK, MODE_HANDOFF, MODE_QUORUM})

_ENVELOPE_STRINGS = ("macp_version", "mode", "message_type", "message_id", "session_id", "sender")

_INT64_TYPES = frozenset(
    {
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED64,
    }
)
_S32, _U32 = (-(2**31), 2**31 - 1), (0, 2**32 - 1)
_S64, _U64 = (-(2**63), 2**63 - 1), (0, 2**64 - 1)
_INTEGER_RANGES = {
    FieldDescriptor.TYPE_INT32: _S32,
    FieldDescriptor.TYPE_SINT32: _S32,
    FieldDescriptor.TYPE_SFIXED32: _S32,
    FieldDescriptor.TYPE_UINT32: _U32,
    FieldDescriptor.TYPE_FIXED32: _U32,
    FieldDescriptor.TYPE_INT64: _S64,
    FieldDescriptor.TYPE_SINT64: _S64,
    FieldDescriptor.TYPE_SFIXED64: _S64,
    FieldDescriptor.TYPE_UINT64: _U64,
    FieldDescriptor.TYPE_FIXED64: _U64,
}
_FLOAT_TYPES = frozenset({FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT})
_DECIMAL = re.compile(r"-?[0-9]+\Z")
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_DROP = object()  # a cleaned value to leave out of the parsed message
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_BASE64 = re.compile(r"[A-Za-z0-9+/\-_]*={0,2}\Z")
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(?:[Zz]|([+-])([0-9]{2}):([0-9]{2}))\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_DEPTH = 100  # upb's default nesting limit
_MIN_MS = calendar.timegm((1, 1, 1, 0, 0, 0, 0, 0, 0)) * 1000
_MAX_MS = calendar.timegm((9999, 12, 31, 23, 59, 59, 0, 0, 0)) * 1000 + 999

_default_registry: ProtoRegistry | None = None


def _registry(registry: ProtoRegistry | None) -> ProtoRegistry:
    global _default_registry
    if registry is not None:
        return registry
    if _default_registry is None:
        _default_registry = ProtoRegistry()
    return _default_registry


def _payload_class(
    mode: str, message_type: str, registry: ProtoRegistry | None
) -> type[Message] | None:
    """The payload message selected by ``(mode, message_type)``, or ``None``
    when the payload travels as ``payload_b64``."""
    core = _CORE_PAYLOADS.get(message_type)
    if core is not None:
        return core
    if mode not in _STANDARD_MODES:
        return None
    type_name = _registry(registry).get_known_type_name(mode, message_type)
    if type_name is None or type_name == "__json__":
        return None
    try:
        cls: type[Message] = symbol_database.Default().GetSymbol(type_name)
    except KeyError as exc:
        raise CanonicalJsonError(
            f"registry names {type_name!r} for ({mode!r}, {message_type!r}), "
            "which is not a loaded message type"
        ) from exc
    return cls


# ── encode ────────────────────────────────────────────────────────────


def envelope_to_canonical_json(
    env: envelope_pb2.Envelope,
    *,
    payload_encoding: Literal["auto", "b64"] = "auto",
    registry: ProtoRegistry | None = None,
) -> dict[str, Any]:
    """Encode *env* as its canonical JSON object (RFC-MACP-0001 §10).

    ``payload_encoding="auto"`` picks ``payload`` or ``payload_b64`` by the
    rules above; ``"b64"`` always sends ``payload_b64``, which keeps the exact
    bytes (RFC-MACP-0014 §5.3). *registry* selects the payload schema for
    standard-mode message types (default: the SDK's own). Raises
    :class:`CanonicalJsonError` for an unknown *payload_encoding*, a payload
    that must travel as ``payload`` but does not parse as its schema, or a
    timestamp outside the years 0001 to 9999.
    """
    if payload_encoding not in ("auto", "b64"):
        raise CanonicalJsonError(
            f"payload_encoding must be 'auto' or 'b64', not {payload_encoding!r}"
        )
    out: dict[str, Any] = {name: getattr(env, name) for name in _ENVELOPE_STRINGS}
    out["timestamp"] = _format_timestamp(env.timestamp_unix_ms)
    cls = (
        None if payload_encoding == "b64" else _payload_class(env.mode, env.message_type, registry)
    )
    if cls is None:
        out["payload_b64"] = base64.b64encode(env.payload).decode("ascii")
        return out
    msg = cls()
    try:
        _check_varints(env.payload, cls.DESCRIPTOR)
        msg.ParseFromString(env.payload)
    except DecodeError as exc:
        raise CanonicalJsonError(
            f"{env.message_type} payload does not parse as {cls.DESCRIPTOR.full_name}"
        ) from exc
    out["payload"] = _message_to_json(msg)
    return out


def ack_to_canonical_json(ack: envelope_pb2.Ack) -> dict[str, Any]:
    """Encode *ack* as its canonical JSON object. ``error`` appears only when set."""
    return _message_to_json(ack)


def _message_to_json(msg: Message) -> dict[str, Any]:
    raw = json_format.MessageToDict(
        msg, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    return _canonical_message(raw, msg.DESCRIPTOR)


def _canonical_message(obj: dict[str, Any], desc: Descriptor) -> dict[str, Any]:
    # Rebuilt in field-number order, with 64-bit integers unquoted where safe.
    return {
        f.name: _canonical_field(obj[f.name], f)
        for f in sorted(desc.fields, key=lambda f: f.number)
        if f.name in obj
    }


def _canonical_field(value: Any, field: FieldDescriptor) -> Any:
    if _is_map(field):
        value_field = field.message_type.fields_by_name["value"]
        return {k: _canonical_scalar(v, value_field) for k, v in value.items()}
    if field.is_repeated:
        return [_canonical_scalar(v, field) for v in value]
    return _canonical_scalar(value, field)


def _canonical_scalar(value: Any, field: FieldDescriptor) -> Any:
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        if field.message_type.full_name.startswith("google.protobuf."):
            return value  # well-known types keep their proto3 JSON form
        return _canonical_message(value, field.message_type)
    if field.type in _INT64_TYPES and isinstance(value, str):
        number = int(value)
        return number if -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER else value
    if field.type in _FLOAT_TYPES and value == 0 and math.copysign(1.0, value) < 0:
        # JSON.stringify cannot write -0.0, so both SDKs send the proto3 JSON
        # string form, as for "NaN" (RFC-MACP-0014 §5.3).
        return "-0"
    return value


def _format_timestamp(unix_ms: int) -> str:
    seconds, millis = divmod(unix_ms, 1000)
    try:
        dt = _EPOCH + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise CanonicalJsonError(f"timestamp_unix_ms {unix_ms} is outside RFC 3339") from exc
    # Formatted by hand: strftime's %Y does not zero-pad years below 1000 on glibc.
    text = (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )
    return f"{text}Z" if millis == 0 else f"{text}.{millis:03d}Z"


# ── decode ────────────────────────────────────────────────────────────


def envelope_from_canonical_json(
    obj: Mapping[str, Any], *, registry: ProtoRegistry | None = None
) -> envelope_pb2.Envelope:
    """Decode a canonical JSON Envelope object into a protobuf Envelope.

    Exactly one of ``payload`` and ``payload_b64`` must be present (a JSON
    ``null`` counts as absent). A ``payload`` object whose ``(mode,
    message_type)`` has no known schema is rejected, as RFC-MACP-0001 §10.12
    requires. Unknown and lowerCamelCase keys are ignored. Raises
    :class:`CanonicalJsonError` on any violation.
    """
    if not isinstance(obj, Mapping):
        raise CanonicalJsonError("an Envelope must be a JSON object")
    fields: dict[str, Any] = {}
    for name in _ENVELOPE_STRINGS:
        value = obj.get(name)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise CanonicalJsonError(f"{name} must be a string")
        fields[name] = _check_utf8(value, name)
    timestamp = obj.get("timestamp")
    if timestamp is None:
        unix_ms = 0
    elif isinstance(timestamp, str):
        unix_ms = _parse_timestamp(timestamp)
    else:
        raise CanonicalJsonError("timestamp must be an RFC 3339 string")

    payload, payload_b64 = obj.get("payload"), obj.get("payload_b64")
    if payload is not None and payload_b64 is not None:
        raise CanonicalJsonError("payload and payload_b64 are both present")
    if payload is None and payload_b64 is None:
        raise CanonicalJsonError("one of payload or payload_b64 is required")
    if payload_b64 is not None:
        if not isinstance(payload_b64, str):
            raise CanonicalJsonError("payload_b64 must be a string")
        data = _decode_base64(payload_b64)
    else:
        if not isinstance(payload, Mapping):
            raise CanonicalJsonError("payload must be a JSON object")
        cls = _payload_class(fields["mode"], fields["message_type"], registry)
        if cls is None:
            raise CanonicalJsonError(
                f"no payload schema for ({fields['mode']!r}, {fields['message_type']!r}); "
                "send payload_b64"
            )
        data = _wire(_message_from_json(payload, cls))
    return envelope_pb2.Envelope(**fields, timestamp_unix_ms=unix_ms, payload=data)


def ack_from_canonical_json(obj: Mapping[str, Any]) -> envelope_pb2.Ack:
    """Decode a canonical JSON Ack object. Raises :class:`CanonicalJsonError`."""
    if not isinstance(obj, Mapping):
        raise CanonicalJsonError("an Ack must be a JSON object")
    ack: envelope_pb2.Ack = _message_from_json(obj, envelope_pb2.Ack)
    if envelope_pb2.SessionState.DESCRIPTOR.values_by_number.get(ack.session_state) is None:
        # Matches macp-sdk-typescript, whose SessionState is a closed union.
        ack.session_state = envelope_pb2.SESSION_STATE_UNSPECIFIED
    return ack


def _message_from_json(obj: Mapping[str, Any], cls: type[Message]) -> Message:
    cleaned = _clean_message(obj, cls.DESCRIPTOR)
    try:
        return json_format.ParseDict(cleaned, cls(), ignore_unknown_fields=True)
    except (json_format.ParseError, ValueError, TypeError, OverflowError) as exc:
        raise CanonicalJsonError(f"invalid {cls.DESCRIPTOR.full_name}: {exc}") from exc


def _clean_message(obj: Mapping[str, Any], desc: Descriptor, depth: int = 0) -> dict[str, Any]:
    """Keep only original field names (§10.8 rule 1), drop nulls (§10.10) and
    check every scalar against its JSON type, since the stock parser accepts
    several forms loosely (bools as numbers, ``"1_0"``, lenient base64)."""
    if depth > _MAX_DEPTH:
        raise CanonicalJsonError("payload nesting is too deep")
    out: dict[str, Any] = {}
    for name, value in obj.items():
        field = desc.fields_by_name.get(name)
        if field is None or value is None:
            continue
        cleaned = _clean_field(value, field, depth)
        if cleaned is not _DROP:
            out[name] = cleaned
    return out


def _clean_field(value: Any, field: FieldDescriptor, depth: int) -> Any:
    if _is_map(field):
        if not isinstance(value, Mapping):
            raise CanonicalJsonError(f"{field.name} must be a JSON object")
        key_field = field.message_type.fields_by_name["key"]
        value_field = field.message_type.fields_by_name["value"]
        entries = {
            _clean_map_key(k, key_field, field.name): _clean_scalar(v, value_field, depth)
            for k, v in value.items()
        }
        return {k: v for k, v in entries.items() if v is not _DROP}
    if field.is_repeated:
        if not isinstance(value, list):
            raise CanonicalJsonError(f"{field.name} must be a JSON array")
        items = [_clean_scalar(v, field, depth) for v in value]
        return [v for v in items if v is not _DROP]
    return _clean_scalar(value, field, depth)


def _clean_map_key(key: Any, key_field: FieldDescriptor, name: str) -> str:
    """JSON map keys are strings; integer and bool keys must be canonical."""
    if not isinstance(key, str):
        raise CanonicalJsonError(f"{name} keys must be strings")
    if key_field.type == FieldDescriptor.TYPE_BOOL:
        if key not in ("true", "false"):
            raise CanonicalJsonError(f"{name} keys must be true or false")
    elif key_field.type in _INTEGER_RANGES:
        if not _DECIMAL.match(key):
            raise CanonicalJsonError(f"{name} keys must be decimal integers")
        _clean_integer(key, key_field)
    return _check_utf8(key, name)


def _clean_scalar(value: Any, field: FieldDescriptor, depth: int = 0) -> Any:
    if value is None:
        raise CanonicalJsonError(f"{field.name} holds a null element")
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        if field.message_type.full_name.startswith("google.protobuf."):
            return value
        if not isinstance(value, Mapping):
            raise CanonicalJsonError(f"{field.name} must be a JSON object")
        return _clean_message(value, field.message_type, depth + 1)
    if field.type in _INTEGER_RANGES:
        return _clean_integer(value, field)
    if field.type == FieldDescriptor.TYPE_ENUM:
        return _clean_enum(value, field)
    if field.type in _FLOAT_TYPES:
        return _clean_float(value, field)
    if field.type == FieldDescriptor.TYPE_BOOL:
        if not isinstance(value, bool):
            raise CanonicalJsonError(f"{field.name} must be true or false")
        return value
    if field.type == FieldDescriptor.TYPE_BYTES:
        if not isinstance(value, str):
            raise CanonicalJsonError(f"{field.name} must be a base64 string")
        return base64.b64encode(_decode_base64(value, field.name)).decode("ascii")
    if not isinstance(value, str):
        raise CanonicalJsonError(f"{field.name} must be a string")
    return _check_utf8(value, field.name)


def _check_utf8(text: str, name: str) -> str:
    """Refuse a lone surrogate, which has no UTF-8 encoding."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalJsonError(f"{name} holds a lone surrogate") from exc
    return text


def _integral(value: Any) -> int | None:
    """A JSON number that is an integer (``5`` or ``5.0``, as JSON.parse sees
    both), or ``None``. Bools are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _clean_integer(value: Any, field: FieldDescriptor) -> int:
    number = _integral(value)
    if number is not None:
        if abs(number) > MAX_SAFE_INTEGER:
            # A number this large may already have been rounded (§10.9).
            raise CanonicalJsonError(f"{field.name}: integers above 2^53-1 must be quoted")
    elif isinstance(value, str) and _DECIMAL.match(value):
        number = int(value)
    else:
        raise CanonicalJsonError(f"{field.name} must be an integer or a quoted decimal")
    low, high = _INTEGER_RANGES[field.type]
    if not low <= number <= high:
        raise CanonicalJsonError(f"{field.name} is out of range")
    return number


def _clean_enum(value: Any, field: FieldDescriptor) -> Any:
    if isinstance(value, str):
        if value in field.enum_type.values_by_name:
            return value
        # §10.4: an unknown name (including "2") leaves the field at its
        # default, or drops the element, as macp-sdk-typescript does.
        return _DROP
    number = _integral(value)
    if number is None or not _INT32_MIN <= number <= _INT32_MAX:
        raise CanonicalJsonError(f"{field.name} must be an enum name or int32")
    return number


def _clean_float(value: Any, field: FieldDescriptor) -> Any:
    if isinstance(value, str):
        if value in ("NaN", "Infinity", "-Infinity"):
            return value
        if not _JSON_NUMBER.match(value):
            raise CanonicalJsonError(f"{field.name} must be a number")
        value = float(value)
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise CanonicalJsonError(f"{field.name} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise CanonicalJsonError(f"{field.name} is out of range") from exc
    if not math.isfinite(number):
        raise CanonicalJsonError(f'{field.name} is out of range; use "Infinity"')
    return number


def _is_map(field: FieldDescriptor) -> bool:
    return bool(
        field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.GetOptions().map_entry
    )


def _decode_base64(text: str, name: str = "payload_b64") -> bytes:
    """Standard or URL-safe alphabet, padded or not (§10.3)."""
    if not _BASE64.match(text):
        raise CanonicalJsonError(f"{name} is not base64")
    if ("+" in text or "/" in text) and ("-" in text or "_" in text):
        raise CanonicalJsonError(f"{name} mixes the standard and URL-safe alphabets")
    if "=" in text and len(text) % 4:
        raise CanonicalJsonError(f"{name} has padding that does not complete a quartet")
    body = text.rstrip("=").replace("-", "+").replace("_", "/")
    if len(body) % 4 == 1:
        raise CanonicalJsonError(f"{name} is not base64")
    try:
        return base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
    except ValueError as exc:  # pragma: no cover - the regex and length check catch these
        raise CanonicalJsonError(f"{name} is not base64") from exc


def _parse_timestamp(text: str) -> int:
    match = _RFC3339.match(text)
    if match is None:
        raise CanonicalJsonError(f"timestamp {text!r} is not RFC 3339")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    fraction, sign, off_hour, off_minute = match.groups()[6:]
    try:
        datetime(year, month, day, hour, minute, second)  # range check; rejects :60
    except ValueError as exc:
        raise CanonicalJsonError(f"timestamp {text!r} is not a valid date-time") from exc
    offset_minutes = 0
    if sign is not None:
        if int(off_hour) > 23 or int(off_minute) > 59:
            raise CanonicalJsonError(f"timestamp {text!r} has an invalid offset")
        offset_minutes = (int(off_hour) * 60 + int(off_minute)) * (1 if sign == "+" else -1)
    millis = int((fraction or "")[:3].ljust(3, "0"))
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    unix_ms = (seconds - offset_minutes * 60) * 1000 + millis
    if not _MIN_MS <= unix_ms <= _MAX_MS:
        raise CanonicalJsonError(f"timestamp {text!r} is outside the years 0001 to 9999 in UTC")
    return unix_ms


# ── §10.13 wire encoding ──────────────────────────────────────────────

_T = FieldDescriptor
_VARINT_TYPES = frozenset(
    {_T.TYPE_INT32, _T.TYPE_INT64, _T.TYPE_UINT32, _T.TYPE_UINT64, _T.TYPE_BOOL, _T.TYPE_ENUM}
)
_ZIGZAG_TYPES = frozenset({_T.TYPE_SINT32, _T.TYPE_SINT64})
_FIXED32 = {_T.TYPE_FIXED32: "<I", _T.TYPE_SFIXED32: "<i", _T.TYPE_FLOAT: "<f"}
_FIXED64 = {_T.TYPE_FIXED64: "<Q", _T.TYPE_SFIXED64: "<q", _T.TYPE_DOUBLE: "<d"}
_QUIET_NAN = {
    _T.TYPE_FLOAT: b"\x00\x00\xc0\x7f",
    _T.TYPE_DOUBLE: b"\x00\x00\x00\x00\x00\x00\xf8\x7f",
}


def _wire(msg: Message) -> bytes:
    """Encode *msg* by the RFC-MACP-0001 §10.13 rules.

    ``ListFields`` yields set fields in field-number order and already leaves
    out proto3 defaults (rule 2), while keeping set presence fields and
    ``-0.0``. Messages decoded from JSON carry no unknown fields (rule 3).
    """
    out = bytearray()
    for field, value in msg.ListFields():
        if _is_map(field):
            entry = field.message_type
            key_field, value_field = entry.fields_by_name["key"], entry.fields_by_name["value"]
            for key in sorted(value, key=_map_key_order):
                body = _encode(key_field, key) + _encode(value_field, value[key])
                out += _tag(field.number, 2) + _varint(len(body)) + body
        elif field.is_repeated:
            if field.type in (_T.TYPE_STRING, _T.TYPE_BYTES, _T.TYPE_MESSAGE):
                for item in value:
                    out += _encode(field, item)
            else:  # rule 4: packed
                body = b"".join(_raw(field, item) for item in value)
                out += _tag(field.number, 2) + _varint(len(body)) + body
        else:
            out += _encode(field, value)
    return bytes(out)


def _map_key_order(key: Any) -> Any:
    # Rule 5: by the UTF-8 bytes of the key as JSON writes it, so integer
    # keys sort as decimal strings ("-1" < "10" < "2"), as in TypeScript.
    if isinstance(key, bool):
        return b"true" if key else b"false"
    return str(key).encode("utf-8")


def _encode(field: FieldDescriptor, value: Any) -> bytes:
    if field.type in _FIXED32:
        wire_type = 5
    elif field.type in _FIXED64:
        wire_type = 1
    elif field.type in (_T.TYPE_STRING, _T.TYPE_BYTES, _T.TYPE_MESSAGE):
        wire_type = 2
    else:
        wire_type = 0
    return _tag(field.number, wire_type) + _raw(field, value)


def _raw(field: FieldDescriptor, value: Any) -> bytes:
    kind = field.type
    if kind in _VARINT_TYPES:
        return _varint(int(value) & 0xFFFFFFFFFFFFFFFF)
    if kind in _ZIGZAG_TYPES:
        number = int(value)
        return _varint((number << 1) ^ (number >> 63) if number < 0 else number << 1)
    if kind in _FIXED32 or kind in _FIXED64:
        if kind in _QUIET_NAN and math.isnan(value):
            return _QUIET_NAN[kind]  # rule 6
        return struct.pack(_FIXED32.get(kind) or _FIXED64[kind], value)
    if kind == _T.TYPE_STRING:
        data = value.encode("utf-8")
    elif kind == _T.TYPE_BYTES:
        data = bytes(value)
    else:
        data = _wire(value)
    return _varint(len(data)) + data


def _tag(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def _varint(number: int) -> bytes:
    out = bytearray()
    while number > 0x7F:
        out.append(number & 0x7F | 0x80)
        number >>= 7
    out.append(number)
    return bytes(out)


def _check_varints(data: bytes, desc: Descriptor | None, depth: int = 0) -> None:
    """Refuse a varint that overflows 64 bits, as protobuf-go and upb's C
    parser do; Python's upb binding accepts it. Walks known submessages,
    packed fields and unknown groups too. Truncation, stray end-groups and
    bad wire types are left to ``ParseFromString``, which refuses them."""
    if depth > _MAX_DEPTH:
        raise DecodeError("message nesting is too deep")
    _scan(data, 0, desc, None, depth)


def _scan(data: bytes, pos: int, desc: Descriptor | None, group: int | None, depth: int) -> int:
    end = len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 7
        field = desc.fields_by_number.get(number) if desc is not None else None
        if wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            chunk = data[pos : pos + length]
            pos += length
            if field is not None and field.type == _T.TYPE_MESSAGE:
                _check_varints(chunk, field.message_type, depth + 1)
            elif (
                field is not None
                and field.is_repeated
                and (field.type in _VARINT_TYPES or field.type in _ZIGZAG_TYPES)
            ):
                at = 0
                while at < len(chunk):
                    _, at = _read_varint(chunk, at)
        elif wire_type == 3:
            if depth >= _MAX_DEPTH:
                raise DecodeError("group nesting is too deep")
            pos = _scan(data, pos, None, number, depth + 1)
        elif wire_type == 4 and group == number:
            return pos
        else:
            return end  # stray end-group or bad wire type: ParseFromString refuses it
    return end


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    for index in range(10):
        if pos + index >= len(data):
            return result, len(data)  # truncated: ParseFromString refuses it
        byte = data[pos + index]
        if index == 9 and byte > 1:
            raise DecodeError("varint overflows 64 bits")
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, pos + index + 1
    raise DecodeError("varint is longer than 10 bytes")
