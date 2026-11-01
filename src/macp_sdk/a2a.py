"""A2A extension binding codec (RFC-MACP-0014).

Maps MACP Envelopes, Acks and session states to and from A2A 1.0 objects:
Messages, Parts, Artifacts, Task metadata, the AgentCard extension and
``google.rpc.ErrorInfo`` details. A2A objects are plain dicts in the A2A JSON
form (lowerCamelCase keys, ``ROLE_USER``, ``mediaType``), so they go straight to
any A2A SDK or HTTP client. This module does not depend on one; see
``docs/guides/a2a-binding.md``.

This is a pure wire binding: no transport, no sealing, no attestation.
Envelope and Ack JSON is the canonical form of :mod:`macp_sdk.canonical_json`.

Every decode failure raises :class:`~macp_sdk.errors.MacpA2aError`, whose
``layer`` and ``code`` say who answers it (RFC-MACP-0014 §11):

- ``a2a``/``INVALID_REQUEST``: two Envelope Parts in one Message, or an
  Envelope Part whose ``mediaType`` carries parameters (§5.1,
  ``ContentTypeNotSupportedError``);
- ``macp``/``INVALID_ENVELOPE``: an Envelope Part that does not decode, a data
  or raw arm that does not match its ``mediaType``, or a ``taskId`` naming
  another session (§6.1, §11.2);
- ``macp``/``PAYLOAD_TOO_LARGE``: a decoded payload over ``max_payload_bytes``
  (§5.4);
- ``codec``/``NOT_A_MACP_MESSAGE``, ``INVALID_ACK``, ``INVALID_SEQUENCE``,
  ``INVALID_SESSION_STATE``: local codec errors with no wire code.

Deduplicating Artifacts by ``EXT/sequence`` and detecting gaps (§9.2) are the
caller's job: :func:`envelope_from_a2a_artifact` returns the sequence for that.
"""

from __future__ import annotations

import base64
import math
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Literal, Required, TypedDict

from google.protobuf.message import DecodeError  # type: ignore[import-untyped]
from macp.v1 import envelope_pb2

from .canonical_json import (
    _MAX_MS,
    _MIN_MS,
    MAX_SAFE_INTEGER,
    _base64_length,
    _check_varints,
    _decode_base64,
    _wire,
    ack_from_canonical_json,
    ack_to_canonical_json,
    envelope_from_canonical_json,
    envelope_to_canonical_json,
)
from .errors import (
    INVALID_ENVELOPE,
    INVALID_REQUEST,
    PAYLOAD_TOO_LARGE,
    CanonicalJsonError,
    MacpA2aError,
)
from .proto_registry import ProtoRegistry

__all__ = [
    "A2A_VERSION",
    "MACP_A2A_EXTENSION_URI",
    "MEDIA_TYPE_ENVELOPE_JSON",
    "MEDIA_TYPE_ENVELOPE_PROTO",
    "A2aAgentExtension",
    "A2aArtifact",
    "A2aErrorInfo",
    "A2aMessage",
    "A2aPart",
    "A2aTask",
    "ack_from_a2a_message",
    "ack_to_a2a_message",
    "envelope_from_a2a_artifact",
    "envelope_from_a2a_message",
    "envelope_from_a2a_part",
    "envelope_to_a2a_artifact",
    "envelope_to_a2a_message",
    "envelope_to_a2a_part",
    "error_info_from_a2a",
    "macp_agent_extension",
    "session_state_from_a2a",
    "session_state_to_a2a",
]

#: The extension URI (§2), also the prefix of every metadata key (``EXT``).
MACP_A2A_EXTENSION_URI = "https://multiagentcoordinationprotocol.io/a2a/extensions/macp/v1"
#: ``mediaType`` of a ``data`` Part carrying a canonical JSON Envelope (§5.1).
MEDIA_TYPE_ENVELOPE_JSON = "application/macp-envelope+json"
#: ``mediaType`` of a ``raw`` Part carrying a Protobuf Envelope (§5.1).
MEDIA_TYPE_ENVELOPE_PROTO = "application/macp-envelope+proto"
#: The A2A protocol version this binding targets.
A2A_VERSION = "1.0"

_KEY_KIND = MACP_A2A_EXTENSION_URI + "/kind"
_KEY_SEQUENCE = MACP_A2A_EXTENSION_URI + "/sequence"
_KEY_SESSION_STATE = MACP_A2A_EXTENSION_URI + "/sessionState"
_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_MACP_DOMAIN = "multiagentcoordinationprotocol.io"
_TRANSPORT = "macp.transport.a2a.v1"
_DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576  # §5.4; the runtime's MACP_MAX_PAYLOAD_BYTES default

# §8: SessionState name -> (TaskState, bare EXT/sessionState value).
_STATE_MAP: dict[str, tuple[str, str]] = {
    "SESSION_STATE_OPEN": ("TASK_STATE_WORKING", "OPEN"),
    "SESSION_STATE_SUSPENDED": ("TASK_STATE_WORKING", "SUSPENDED"),
    "SESSION_STATE_RESOLVED": ("TASK_STATE_COMPLETED", "RESOLVED"),
    "SESSION_STATE_EXPIRED": ("TASK_STATE_FAILED", "EXPIRED"),
    "SESSION_STATE_CANCELLED": ("TASK_STATE_CANCELED", "CANCELLED"),
}
_STATE_FROM_BARE = {bare: name for name, (_, bare) in _STATE_MAP.items()}
# §6.1, §6.2: these Messages carry no taskId; every other names its session.
_NO_TASK_TYPES = frozenset({"SessionStart", "Signal"})


# ── A2A object shapes (A2A 1.0 JSON) ──────────────────────────────────


class A2aPart(TypedDict, total=False):
    """An A2A ``Part``: exactly one of ``text``, ``raw``, ``url`` or ``data``."""

    text: str
    raw: str
    url: str
    data: Any
    mediaType: str
    filename: str
    metadata: dict[str, Any]


class A2aMessage(TypedDict, total=False):
    """An A2A ``Message``."""

    messageId: Required[str]
    role: Required[str]
    parts: Required[list[A2aPart]]
    contextId: str
    taskId: str
    extensions: list[str]
    referenceTaskIds: list[str]
    metadata: dict[str, Any]


class A2aArtifact(TypedDict, total=False):
    """An A2A ``Artifact``."""

    artifactId: Required[str]
    parts: Required[list[A2aPart]]
    name: str
    description: str
    extensions: list[str]
    metadata: dict[str, Any]


class A2aTask(TypedDict, total=False):
    """An A2A ``Task`` (a ``TaskStatusUpdateEvent`` has the same ``metadata``)."""

    id: Required[str]
    contextId: str
    status: dict[str, Any]
    artifacts: list[A2aArtifact]
    history: list[A2aMessage]
    metadata: dict[str, Any]


class A2aAgentExtension(TypedDict, total=False):
    """An A2A ``AgentExtension``, as declared in ``AgentCard.capabilities``."""

    uri: Required[str]
    description: str
    required: bool
    params: dict[str, Any]


class A2aErrorInfo(TypedDict):
    """The MACP ``google.rpc.ErrorInfo`` of an A2A error (§11.1, §12)."""

    reason: str
    domain: str
    metadata: dict[str, Any]
    duplicate: bool


# ── Envelopes (§5.1, §5.3, §5.4) ──────────────────────────────────────


def envelope_to_a2a_part(
    env: envelope_pb2.Envelope,
    *,
    encoding: Literal["json", "proto"] = "json",
    registry: ProtoRegistry | None = None,
) -> A2aPart:
    """Encode *env* as an Envelope Part.

    ``encoding="json"`` gives a ``data`` Part holding the canonical JSON
    Envelope; ``"proto"`` gives a ``raw`` Part holding the Envelope's Protobuf
    bytes (field-number order, defaults omitted), base64-encoded.
    """
    if encoding == "json":
        try:
            data = envelope_to_canonical_json(env, registry=registry)
        except CanonicalJsonError as exc:
            raise _invalid_envelope(str(exc)) from exc
        return {"mediaType": MEDIA_TYPE_ENVELOPE_JSON, "data": data}
    if encoding == "proto":
        if not _MIN_MS <= env.timestamp_unix_ms <= _MAX_MS:
            raise _invalid_envelope(
                f"timestamp_unix_ms {env.timestamp_unix_ms} is outside years 0001 to 9999"
            )
        raw = base64.b64encode(_wire(env)).decode("ascii")
        return {"mediaType": MEDIA_TYPE_ENVELOPE_PROTO, "raw": raw}
    raise ValueError(f"encoding must be 'json' or 'proto', not {encoding!r}")


def envelope_from_a2a_part(
    part: Mapping[str, Any],
    *,
    max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
    registry: ProtoRegistry | None = None,
) -> envelope_pb2.Envelope:
    """Decode an Envelope Part.

    *max_payload_bytes* bounds the decoded payload (§5.4): the base64-decoded
    ``payload_b64``, the Protobuf bytes a JSON ``payload`` converts to, or the
    payload of a ``raw`` Envelope. A ``payload_b64`` is measured before it is
    decoded.
    """
    _check_limit(max_payload_bytes)
    media = _envelope_media_type(part)
    if media is None:
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "not an Envelope Part")
    media_type, has_parameters = media
    if has_parameters:
        # Before any decoding, so never INVALID_ENVELOPE (§5.1).
        raise MacpA2aError(
            "a2a",
            INVALID_REQUEST,
            "ContentTypeNotSupportedError: an Envelope Part mediaType carries "
            f"parameters: {part['mediaType']!r} (§5.1)",
        )
    if media_type == MEDIA_TYPE_ENVELOPE_JSON:
        data = part.get("data")
        if not isinstance(data, Mapping):
            raise _invalid_envelope("a JSON Envelope Part carries no data object (§5.1)")
        return _envelope_from_data(data, max_payload_bytes, registry)
    raw = part.get("raw")
    if raw is None:
        raise _invalid_envelope("a Protobuf Envelope Part carries no raw bytes (§5.1)")
    if not isinstance(raw, str):
        raise _invalid_envelope("raw is not a base64 string")
    try:
        buf = _decode_base64(raw, "raw")
        _check_varints(buf, envelope_pb2.Envelope.DESCRIPTOR)
        env = envelope_pb2.Envelope.FromString(buf)
    except (CanonicalJsonError, DecodeError) as exc:
        raise _invalid_envelope(f"raw Envelope does not decode: {exc}") from exc
    if not _MIN_MS <= env.timestamp_unix_ms <= _MAX_MS:
        raise _invalid_envelope(
            f"timestamp_unix_ms {env.timestamp_unix_ms} is outside years 0001 to 9999"
        )
    _check_size(len(env.payload), max_payload_bytes)
    return env


def _envelope_from_data(
    data: Mapping[str, Any], limit: int, registry: ProtoRegistry | None
) -> envelope_pb2.Envelope:
    payload_b64 = data.get("payload_b64")
    try:
        if not isinstance(payload_b64, str):
            env = envelope_from_canonical_json(data, registry=registry)
            _check_size(len(env.payload), limit)
            return env
        # Every other field first, then the size, then the base64 itself, so
        # an oversized payload is refused without being decoded (§5.4).
        env = envelope_from_canonical_json({**data, "payload_b64": ""}, registry=registry)
        _check_size(_base64_length(payload_b64), limit)
        env.payload = _decode_base64(payload_b64)
    except CanonicalJsonError as exc:
        raise _invalid_envelope(str(exc)) from exc
    return env


def envelope_to_a2a_message(
    env: envelope_pb2.Envelope,
    *,
    encoding: Literal["json", "proto"] = "json",
    message_id: str | None = None,
    context_id: str | None = None,
    registry: ProtoRegistry | None = None,
) -> A2aMessage:
    """Encode *env* as a ``ROLE_USER`` Message with one Envelope Part (§5.1, §6).

    ``messageId`` defaults to ``env.message_id``, or a new UUID when that is
    empty. ``taskId`` is the session id,
    except for a SessionStart or a Signal, which carry none (§6.1, §6.2).
    ``contextId`` is set only when *context_id* is non-empty.
    """
    msg: A2aMessage = {
        "messageId": message_id or env.message_id or str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [envelope_to_a2a_part(env, encoding=encoding, registry=registry)],
        "extensions": [MACP_A2A_EXTENSION_URI],
    }
    task_id = _expected_task_id(env)
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    return msg


def envelope_from_a2a_message(
    msg: Mapping[str, Any],
    *,
    max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
    registry: ProtoRegistry | None = None,
) -> envelope_pb2.Envelope:
    """Decode the one Envelope Part of a Message.

    Parts with another ``mediaType`` are ignored (§5.1); every Envelope Part,
    with or without parameters, counts toward the one-Envelope rule. A session-scoped
    Envelope must arrive with ``taskId`` equal to its ``session_id`` (§6.1). A
    non-empty ``taskId`` on a SessionStart or Signal is left to the server,
    which answers it from its Task state (§11.3).
    """
    if not isinstance(msg, Mapping):
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "not a Message")
    part = _single_envelope_part(msg.get("parts"))
    env = envelope_from_a2a_part(part, max_payload_bytes=max_payload_bytes, registry=registry)
    expected = _expected_task_id(env)
    if expected and (msg.get("taskId") or "") != expected:
        raise _invalid_envelope("Message.taskId does not match the Envelope's session_id (§6.1)")
    return env


# ── Acks (§5.2) ───────────────────────────────────────────────────────


def ack_to_a2a_message(
    ack: envelope_pb2.Ack,
    *,
    message_id: str | None = None,
    task_id: str = "",
    context_id: str | None = None,
) -> A2aMessage:
    """Encode *ack* as a ``ROLE_AGENT`` Message with one Ack Part (§5.2).

    ``messageId`` is a new UUID when *message_id* is not given. Raises
    ``codec``/``INVALID_ACK`` unless ``error`` is set exactly when ``ok`` is false.
    A ``session_state`` with no name is written as ``SESSION_STATE_UNSPECIFIED``,
    as the decoder reads it.
    """
    _check_ack(ack)
    if envelope_pb2.SessionState.DESCRIPTOR.values_by_number.get(ack.session_state) is None:
        named = envelope_pb2.Ack()
        named.CopyFrom(ack)
        named.session_state = envelope_pb2.SESSION_STATE_UNSPECIFIED
        ack = named
    msg: A2aMessage = {
        "messageId": message_id or str(uuid.uuid4()),
        "role": "ROLE_AGENT",
        "extensions": [MACP_A2A_EXTENSION_URI],
        "parts": [
            {
                "mediaType": "application/json",
                "data": ack_to_canonical_json(ack),
                "metadata": {_KEY_KIND: "ack"},
            }
        ],
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    return msg


def ack_from_a2a_message(msg: Mapping[str, Any]) -> envelope_pb2.Ack:
    """Decode the Ack Part (``EXT/kind == "ack"``) of a Message.

    Raises ``codec``/``NOT_A_MACP_MESSAGE`` when there is none and
    ``codec``/``INVALID_ACK`` for two, for an Ack that does not decode, or
    when ``error`` is not present exactly when ``ok`` is false.
    """
    parts = msg.get("parts") if isinstance(msg, Mapping) else None
    acks = [
        p
        for p in (parts if isinstance(parts, list) else [])
        if isinstance(p, Mapping)
        and isinstance(p.get("metadata"), Mapping)
        and p["metadata"].get(_KEY_KIND) == "ack"
    ]
    if not acks:
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "no Ack Part")
    if len(acks) > 1:
        raise _invalid_ack("two Ack Parts")
    try:
        ack = ack_from_canonical_json(acks[0].get("data"))  # type: ignore[arg-type]
    except CanonicalJsonError as exc:
        raise _invalid_ack(str(exc)) from exc
    _check_ack(ack)
    return ack


def _check_ack(ack: envelope_pb2.Ack) -> None:
    if ack.HasField("error") == ack.ok:
        raise _invalid_ack("error must be present exactly when ok is false (§5.2)")


# ── Artifacts (§9.1) ──────────────────────────────────────────────────


def envelope_to_a2a_artifact(
    env: envelope_pb2.Envelope, sequence: int, *, registry: ProtoRegistry | None = None
) -> A2aArtifact:
    """Encode an accepted Envelope as an Observation Artifact (§9.1).

    *sequence* is the session's acceptance sequence, from 1.
    """
    seq = _check_sequence(sequence)
    return {
        "artifactId": "macp:" + env.message_id,
        "name": env.message_type,
        "parts": [envelope_to_a2a_part(env, registry=registry)],
        "metadata": {_KEY_SEQUENCE: seq},
        "extensions": [MACP_A2A_EXTENSION_URI],
    }


def envelope_from_a2a_artifact(
    artifact: Mapping[str, Any],
    *,
    max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
    registry: ProtoRegistry | None = None,
) -> tuple[envelope_pb2.Envelope, int]:
    """Decode an Observation Artifact into ``(envelope, sequence)``.

    ``EXT/sequence`` may arrive as an integral float (``2.0``). Discarding
    repeats and recovering gaps by sequence is the caller's job (§9.2).
    """
    if not isinstance(artifact, Mapping):
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "not an Artifact")
    meta = artifact.get("metadata")
    if not isinstance(meta, Mapping) or _KEY_SEQUENCE not in meta:
        raise MacpA2aError("codec", "INVALID_SEQUENCE", "EXT/sequence is missing")
    sequence = _check_sequence(meta[_KEY_SEQUENCE])
    part = _single_envelope_part(artifact.get("parts"))
    env = envelope_from_a2a_part(part, max_payload_bytes=max_payload_bytes, registry=registry)
    return env, sequence


def _check_sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MacpA2aError("codec", "INVALID_SEQUENCE", "EXT/sequence is not a number")
    if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise MacpA2aError("codec", "INVALID_SEQUENCE", "EXT/sequence is not an integer")
    if not 1 <= value <= MAX_SAFE_INTEGER:
        raise MacpA2aError(
            "codec", "INVALID_SEQUENCE", f"EXT/sequence {value} is outside 1 to 2^53-1"
        )
    return int(value)


# ── Session state (§8) ────────────────────────────────────────────────


def session_state_to_a2a(state: str | int) -> tuple[str, dict[str, str]]:
    """Map a ``SessionState`` (name or number) to ``(task_state, metadata)``.

    ``metadata`` holds ``EXT/sessionState``, for ``Task.metadata`` or a
    ``TaskStatusUpdateEvent``. ``SESSION_STATE_UNSPECIFIED`` has no TaskState.
    """
    name = state
    if isinstance(state, int) and not isinstance(state, bool):
        name = envelope_pb2.SessionState.DESCRIPTOR.values_by_number.get(state)
        name = name.name if name is not None else state
    mapped = _STATE_MAP.get(name) if isinstance(name, str) else None
    if mapped is None:
        raise MacpA2aError(
            "codec", "INVALID_SESSION_STATE", f"session state {state!r} has no A2A mapping"
        )
    task_state, bare = mapped
    return task_state, {_KEY_SESSION_STATE: bare}


def session_state_from_a2a(task_or_event: Mapping[str, Any]) -> str:
    """Read the ``SessionState`` name from a Task or a ``TaskStatusUpdateEvent``.

    Reads ``EXT/sessionState`` from the object's ``metadata``. Raises
    ``codec``/``INVALID_SESSION_STATE`` when it is missing or not a bare state
    name (``"RESOLVED"``); it never falls back to the TaskState (§8).
    """
    meta = task_or_event.get("metadata") if isinstance(task_or_event, Mapping) else None
    value = meta.get(_KEY_SESSION_STATE) if isinstance(meta, Mapping) else None
    name = _STATE_FROM_BARE.get(value) if isinstance(value, str) else None
    if name is None:
        raise MacpA2aError("codec", "INVALID_SESSION_STATE", f"EXT/sessionState is {value!r}")
    return name


# ── AgentCard (§3) and errors (§11.1, §12) ────────────────────────────


def macp_agent_extension(
    *,
    macp_versions: Iterable[str],
    modes: Iterable[str],
    required: bool = False,
    manifest_url: str | None = None,
) -> A2aAgentExtension:
    """The ``AgentExtension`` an A2A MACP server declares in its AgentCard (§3).

    Raises ``ValueError`` for a bare string where a list is expected, or a
    non-bool *required*.
    """
    if isinstance(macp_versions, str) or isinstance(modes, str):
        raise ValueError("macp_versions and modes are lists of strings, not a string")
    if not isinstance(required, bool):
        raise ValueError(f"required must be a bool, not {required!r}")
    params: dict[str, Any] = {
        "macpVersions": list(macp_versions),
        "modes": list(modes),
        "transport": _TRANSPORT,
    }
    if manifest_url:
        params["manifestUrl"] = manifest_url
    return {"uri": MACP_A2A_EXTENSION_URI, "required": required, "params": params}


def error_info_from_a2a(error_or_details: Any) -> A2aErrorInfo | None:
    """Find the MACP ``ErrorInfo`` in an A2A error, or ``None``.

    Takes a JSON-RPC ``error`` object (details in ``data``) or a bare details
    array (HTTP+JSON, gRPC), and picks the first entry whose ``@type`` is
    ``google.rpc.ErrorInfo`` and whose ``domain`` is MACP's. ``duplicate`` is
    true only for the metadata string ``"true"`` (§12).
    """
    if isinstance(error_or_details, list):
        details: Any = error_or_details
    elif isinstance(error_or_details, Mapping):
        details = error_or_details.get("data")
    else:
        details = None
    for item in details if isinstance(details, list) else []:
        if (
            isinstance(item, Mapping)
            and item.get("@type") == _ERROR_INFO_TYPE
            and item.get("domain") == _MACP_DOMAIN
        ):
            meta = item.get("metadata")
            metadata = dict(meta) if isinstance(meta, Mapping) else {}
            reason = item.get("reason")
            return {
                "reason": reason if isinstance(reason, str) else "",
                "domain": _MACP_DOMAIN,
                "metadata": metadata,
                "duplicate": metadata.get("duplicate") == "true",
            }
    return None


# ── helpers ───────────────────────────────────────────────────────────


def _envelope_media_type(part: Any) -> tuple[str, bool] | None:
    """``(media type, has parameters)`` for an Envelope Part, else ``None`` (§5.1).

    RFC 9110 §8.3.1 comparison: type and subtype ASCII case-insensitive, with
    spaces and tabs around them ignored. A non-ASCII type or subtype is never
    an Envelope Part: the guard keeps this true for any future change to
    Unicode-aware folding (``"j\u017fon".casefold() == "json"``). A ``;`` after the subtype
    marks parameters even with nothing after it.
    """
    if not isinstance(part, Mapping) or not isinstance(part.get("mediaType"), str):
        return None
    head, sep, _ = part["mediaType"].partition(";")
    base = head.strip(" \t")
    if not base.isascii():
        return None
    base = base.lower()
    if base not in (MEDIA_TYPE_ENVELOPE_JSON, MEDIA_TYPE_ENVELOPE_PROTO):
        return None
    return base, bool(sep)


def _is_envelope_part(part: Any) -> bool:
    return _envelope_media_type(part) is not None


def _single_envelope_part(parts: Any) -> Mapping[str, Any]:
    if not isinstance(parts, list):
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "no parts")
    found = [p for p in parts if _is_envelope_part(p)]
    if len(found) > 1:
        raise MacpA2aError("a2a", INVALID_REQUEST, "two or more Envelope Parts (§5.1)")
    if not found:
        raise MacpA2aError("codec", "NOT_A_MACP_MESSAGE", "no Envelope Part")
    part: Mapping[str, Any] = found[0]
    return part


def _expected_task_id(env: envelope_pb2.Envelope) -> str:
    return "" if env.message_type in _NO_TASK_TYPES else env.session_id


def _check_limit(limit: Any) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError(f"max_payload_bytes must be a non-negative int, not {limit!r}")


def _check_size(size: int, limit: int) -> None:
    if size > limit:
        raise MacpA2aError(
            "macp", PAYLOAD_TOO_LARGE, f"payload is {size} bytes, over the {limit}-byte bound"
        )


def _invalid_envelope(message: str) -> MacpA2aError:
    return MacpA2aError("macp", INVALID_ENVELOPE, message)


def _invalid_ack(message: str) -> MacpA2aError:
    return MacpA2aError("codec", "INVALID_ACK", message)
