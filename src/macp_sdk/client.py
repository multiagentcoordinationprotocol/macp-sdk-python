from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Generator, Iterator, Sequence
from typing import Any

import grpc
from macp.v1 import core_pb2, core_pb2_grpc, envelope_pb2, policy_pb2

from ._logging import logger
from .auth import AuthConfig
from .envelope import (
    build_envelope,
    build_progress_payload,
    build_signal_payload,
    serialize_message,
)
from .errors import (
    AckFailure,
    ErrorDetails,
    MacpAckError,
    MacpIdentityMismatchError,
    MacpSdkError,
    MacpSessionError,
    MacpTimeoutError,
    MacpTransportError,
    parse_error_details,
)
from .validation import validate_progress_scope, validate_signal_type

# Public typing alias for inline stream-error callbacks. Parity with
# typescript-sdk's ``InlineErrorCallback``. Receives the protobuf
# ``StreamError`` (or a legacy error object) for application-level errors
# that do NOT tear down the stream — consumers typically log and continue.
InlineErrorCallback = Callable[[Any], None]


def _parse_ack_details(ack: object) -> ErrorDetails:
    """Decode an ACK error's details into ``reasons`` + ``denials``."""
    error = getattr(ack, "error", None)
    if not error:
        return ErrorDetails()
    return parse_error_details(getattr(error, "details", None) or b"")


def _parse_ack_reasons(ack: object) -> list[str]:
    """Extract structured denial reasons from an ACK error's details."""
    return _parse_ack_details(ack).reasons


def _trailing_metadata_value(rpc_error: grpc.RpcError, key: str) -> str | bytes | None:
    """Return the first trailing-metadata value for *key*, or None."""
    try:
        metadata: Any = rpc_error.trailing_metadata()
        if not metadata:
            return None
        item: Any
        for item in metadata:
            # Sync calls yield ``_Metadatum`` objects (``.key``/``.value``);
            # ``grpc.aio`` yields plain ``(key, value)`` tuples.
            item_key = getattr(item, "key", None)
            if item_key is None:
                item_key, item_value = item[0], item[1]
            else:
                item_value = item.value
            if item_key == key:
                value: str | bytes = item_value
                return value
    except Exception:
        pass
    return None


def _parse_grpc_metadata_details(rpc_error: grpc.RpcError) -> ErrorDetails:
    """Decode ``macp-error-details-bin`` trailing metadata."""
    value = _trailing_metadata_value(rpc_error, "macp-error-details-bin")
    if value is None:
        return ErrorDetails()
    return parse_error_details(value)


def _parse_grpc_metadata_reasons(rpc_error: grpc.RpcError) -> list[str]:
    """Extract structured reasons from gRPC trailing metadata."""
    return _parse_grpc_metadata_details(rpc_error).reasons


def macp_error_code(rpc_error: grpc.RpcError) -> str | None:
    """Return the MACP registry code from ``macp-error-code`` metadata, or None.

    Runtime ≥ 0.9.0 attaches it to every status that carries a MACP error
    (S13). Several registry codes share one gRPC status, so this — not the
    status — is what to branch on. Older runtimes don't send it.

    Most ``MacpClient`` methods already surface the code (``MacpAckError``
    ``failure.code`` or ``MacpTransportError.macp_code``). Use this on the raw
    ``grpc.RpcError`` raised by ``initialize``, ``get_session``,
    ``get_manifest``, ``list_sessions_page``/``list_sessions``,
    ``get_policy`` and ``list_policies``::

        try:
            client.get_session(session_id)
        except grpc.RpcError as e:
            if macp_error_code(e) == SESSION_NOT_FOUND:
                ...
    """
    value = _trailing_metadata_value(rpc_error, "macp-error-code")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value or None


def _rpc_status_name(rpc_err: grpc.RpcError) -> str | None:
    """Return the gRPC status code name for *rpc_err*, or None.

    Defensive against error objects that don't implement ``code()`` (the bare
    ``grpc.RpcError`` base class doesn't; only concrete call errors do).
    """
    code_fn = getattr(rpc_err, "code", None)
    if not callable(code_fn):
        return None
    try:
        status = code_fn()
    except Exception:
        return None
    return status.name if status is not None else None


def _rpc_details(rpc_err: grpc.RpcError) -> str | None:
    """Return ``rpc_err.details()`` if available, else None."""
    details_fn = getattr(rpc_err, "details", None)
    if not callable(details_fn):
        return None
    try:
        return details_fn()
    except Exception:
        return None


def _default_capabilities() -> core_pb2.Capabilities:
    return core_pb2.Capabilities(
        sessions=core_pb2.SessionsCapability(stream=True, list_sessions=True, watch_sessions=True),
        cancellation=core_pb2.CancellationCapability(cancel_session=True),
        progress=core_pb2.ProgressCapability(progress=True),
        manifest=core_pb2.ManifestCapability(get_manifest=True),
        mode_registry=core_pb2.ModeRegistryCapability(list_modes=True, list_changed=True),
        roots=core_pb2.RootsCapability(list_roots=True, list_changed=True),
        policy_registry=policy_pb2.PolicyRegistryCapability(
            register_policy=True, list_policies=True, list_changed=True
        ),
        experimental=core_pb2.ExperimentalCapabilities(features={}),
    )


class _UnboundedTimeout:
    """Sentinel type for :data:`UNBOUNDED`. Not constructed by callers."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNBOUNDED"


UNBOUNDED = _UnboundedTimeout()

# ``timeout=None`` (the default) means "use ``self.default_timeout``"; both
# ``None`` and ``0`` are falsy, so once ``default_timeout`` is set to a
# non-None value, neither could express "no deadline for this one call" (#93
# item 6). Pass ``UNBOUNDED`` for that. Not a plain ``float | None`` because
# an ``Any``-typed default would defeat mypy strict's whole point of catching
# a mistyped timeout argument at every one of this client's ~20 call sites.
TimeoutValue = float | None | _UnboundedTimeout


def _cancel_quietly(call: Any) -> None:
    """Best-effort cancel of a server-streaming gRPC call.

    Called from each ``watch_*`` generator's ``finally``, so it runs on
    normal exhaustion, on an exception, and on ``GeneratorExit`` when a
    consumer abandons the stream. Cancelling an already-finished call is
    a no-op in grpcio; a cancel that raises must not replace whatever
    the generator was already propagating.
    """
    try:
        call.cancel()
    except Exception:
        logger.debug("stream cancel failed", exc_info=True)


def _classify_stream_response(response: Any) -> tuple[envelope_pb2.Envelope | None, Any]:
    """Split a ``StreamSessionResponse`` into ``(envelope, inline_error)``.

    At most one is non-None; both are None for an empty frame. Shared by
    :class:`MacpStream` and the asyncio stream so both read the wire the
    same way, including the legacy nested ``.response`` wrapper.
    """
    envelope = getattr(response, "envelope", None)
    error = getattr(response, "error", None)
    if error is not None and hasattr(error, "ByteSize") and error.ByteSize() > 0:
        return None, error
    if envelope is not None and envelope.ByteSize() > 0:
        return envelope, None
    # Fallback: a nested .response wrapper (legacy proto shape).
    inner = getattr(response, "response", None)
    if inner is not None and hasattr(inner, "ByteSize") and inner.ByteSize() > 0:
        inner_env = getattr(inner, "envelope", None)
        inner_err = getattr(inner, "error", None)
        if inner_env is not None and inner_env.ByteSize() > 0:
            return inner_env, None
        if inner_err is not None:
            return None, inner_err
    return None, None


class MacpStream:
    _END = object()

    def __init__(
        self,
        stub: core_pb2_grpc.MACPRuntimeServiceStub,
        *,
        metadata: Sequence[tuple[str, str]],
        timeout: float | None = None,
    ) -> None:
        self._requests: queue.Queue[object] = queue.Queue()
        self._responses: queue.Queue[object] = queue.Queue()
        self._closed = False
        self._inline_error_callbacks: list[InlineErrorCallback] = []
        self._call = stub.StreamSession(self._request_iter(), metadata=metadata, timeout=timeout)
        self._thread = threading.Thread(target=self._pump_responses, daemon=True)
        self._thread.start()

    def _request_iter(self) -> Iterator[core_pb2.StreamSessionRequest]:
        while True:
            item = self._requests.get()
            if item is self._END:
                return
            # RFC-MACP-0006-A1: support both envelope sends and subscribe frames
            if isinstance(item, core_pb2.StreamSessionRequest):
                yield item
            else:
                assert isinstance(item, envelope_pb2.Envelope)
                yield core_pb2.StreamSessionRequest(envelope=item)

    def _pump_responses(self) -> None:
        try:
            for response in self._call:
                envelope, error = _classify_stream_response(response)
                if error is not None:
                    for cb in self._inline_error_callbacks:
                        cb(error)
                    logger.warning("inline stream error: %s", error)
                elif envelope is not None:
                    self._responses.put(envelope)
        except grpc.RpcError as exc:
            self._responses.put(exc)
        finally:
            self._responses.put(self._END)

    def on_inline_error(self, callback: InlineErrorCallback) -> None:
        """Register a callback for inline application-level stream errors.

        The callback receives the ``macp.v1.MACPError``. Since runtime 0.9.0
        its ``code`` is the registry code (``INVALID_ENVELOPE``,
        ``POLICY_DENIED``, ...; before, it held the status message text),
        ``message_id`` names the rejected envelope, and ``details`` carries
        the same JSON as an ``Ack`` — decode it with
        :func:`macp_sdk.parse_error_details`.
        """
        self._inline_error_callbacks.append(callback)

    def send(self, envelope: envelope_pb2.Envelope) -> None:
        if self._closed:
            raise MacpSdkError("stream is already closed")
        self._requests.put(envelope)

    def send_subscribe(self, session_id: str, after_sequence: int = 0) -> None:
        """RFC-MACP-0006-A1: Send a subscribe-only frame to receive session
        history + live broadcast.

        ``after_sequence`` (RFC-MACP-0006 §3.2, runtime v0.5.0) is the 1-based
        ordinal over *accepted envelopes*, interpreted **exclusively**: ``0``
        (default) replays from the start; ``N`` replays from envelope ``N+1``
        onward, so envelope ``N`` is never re-delivered. Ordinals are
        contiguous and stable across log compaction and runtime restart. To
        resume, track how many envelopes you have consumed and pass that count
        as ``after_sequence`` on reconnect. Resuming below a compacted range
        is refused: runtime ≥ 0.9.0 sends an inline ``INVALID_ENVELOPE``
        error frame (delivered to :meth:`on_inline_error` callbacks; the
        stream stays open), while older runtimes fail the stream with gRPC
        ``FAILED_PRECONDITION`` (surfaced as
        ``MacpTransportError(code="FAILED_PRECONDITION")`` from :meth:`read`).
        Either way the message names the compacted base — resubscribe at or
        above it, or re-read state via ``GetSession``.
        """
        if self._closed:
            raise MacpSdkError("stream is already closed")
        req = core_pb2.StreamSessionRequest(
            subscribe_session_id=session_id,
            after_sequence=after_sequence,
        )
        self._requests.put(req)

    def read(self, timeout: float | None = None) -> envelope_pb2.Envelope | None:
        try:
            item = self._responses.get(timeout=timeout)
        except queue.Empty:
            raise MacpTimeoutError(f"stream read timed out after {timeout}s") from None
        if item is self._END:
            # The sentinel is one-shot in the queue — re-push it so end-of-stream
            # stays durably observable no matter how many times a caller reads
            # past it (a second read(), or responses() started after close).
            self._responses.put(self._END)
            return None
        if isinstance(item, grpc.RpcError):
            raise MacpTransportError(
                _rpc_details(item) or str(item),
                code=_rpc_status_name(item),
                macp_code=macp_error_code(item),
            )
        assert isinstance(item, envelope_pb2.Envelope)
        return item

    def responses(self, timeout: float | None = None) -> Iterator[envelope_pb2.Envelope]:
        while True:
            envelope = self.read(timeout=timeout)
            if envelope is None:
                return
            yield envelope

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._requests.put(self._END)

    def cancel(self) -> None:
        """Forcibly abort the underlying gRPC call.

        Unlike :meth:`close` (which only half-closes the outgoing request
        side and waits for the server to end the stream on its own),
        ``cancel`` aborts the call immediately — unblocking a :meth:`read`
        that is parked on an idle stream with no message pending, instead
        of waiting indefinitely for the next server message or a server-side
        stream end. Safe to call more than once, and safe to call alongside
        :meth:`close`.

        After ``cancel()``, the pump thread observes the abort as a gRPC
        ``CANCELLED`` error and enqueues it — so the *next* :meth:`read`
        raises ``MacpTransportError(code="CANCELLED")`` rather than
        returning ``None`` the way end-of-stream does. A caller that just
        wants to stop reading (e.g. after ``cancel()`` from another thread)
        should treat that ``CANCELLED`` error the same as a clean stop,
        not as an unexpected failure.
        """
        if not self._closed:
            self._closed = True
            self._requests.put(self._END)
        self._call.cancel()


class _ClientCore:
    """Transport-independent state and request/response logic.

    Shared by :class:`MacpClient` (sync ``grpc``) and
    :class:`macp_sdk.async_client.AsyncMacpClient` (``grpc.aio``) so both
    apply the same transport-security rule, auth resolution, timeout
    semantics, request construction and error mapping. Subclasses only own
    the channel and the (sync or awaitable) stub calls.
    """

    def __init__(
        self,
        *,
        target: str,
        secure: bool | None,
        allow_insecure: bool,
        auth: AuthConfig | None,
        default_timeout: float | None,
        client_name: str,
        client_version: str,
    ) -> None:
        if secure is None:
            secure = not allow_insecure
        if not secure and not allow_insecure:
            raise MacpSdkError(
                "secure=False requires allow_insecure=True; "
                "encrypted transport is required by RFC-MACP-0004 §2 "
                "'Transport Security' in production. "
                "For local dev only, pass allow_insecure=True."
            )
        self.target = target
        self.secure = secure
        self.auth = auth
        self.default_timeout = default_timeout
        self.client_name = client_name
        self.client_version = client_version
        #: Capabilities from the most recent :meth:`initialize`; ``None``
        #: until the client has initialized.
        self.server_capabilities: core_pb2.Capabilities | None = None

    def _metadata(self, auth: AuthConfig | None = None) -> Sequence[tuple[str, str]]:
        selected = auth or self.auth
        return selected.metadata() if selected else []

    def _require_auth(self, auth: AuthConfig | None = None) -> AuthConfig:
        selected = auth or self.auth
        if selected is None:
            raise MacpSdkError("this operation requires auth; pass auth= or configure client.auth")
        return selected

    def _resolve_timeout(self, timeout: TimeoutValue) -> float | None:
        """Resolve a per-call ``timeout`` against ``self.default_timeout``.

        ``None`` (the default) means "use ``self.default_timeout``".
        :data:`UNBOUNDED` explicitly requests no deadline for this call,
        overriding a configured ``default_timeout`` -- see :data:`UNBOUNDED`
        for why that could not be expressed with a plain ``float | None``.
        """
        if isinstance(timeout, _UnboundedTimeout):
            return None
        if timeout is None:
            return self.default_timeout
        return timeout

    @staticmethod
    def _transport_error_from_rpc(rpc_err: grpc.RpcError) -> MacpTransportError:
        """Build a :class:`MacpTransportError` preserving the gRPC status code.

        Watch/stream RPCs surface consumer lag as ``RESOURCE_EXHAUSTED`` and a
        missing-auth ``WatchSignals`` as ``UNAUTHENTICATED``; attaching the
        code lets callers decide to reconnect (lag) vs. fix auth vs. give up.
        """
        return MacpTransportError(
            _rpc_details(rpc_err) or str(rpc_err),
            code=_rpc_status_name(rpc_err),
            macp_code=macp_error_code(rpc_err),
        )

    @staticmethod
    def _map_registry_mutation_error(rpc_err: grpc.RpcError, *, read_only_hint: str) -> Exception:
        """Translate a registry-mutation ``RpcError`` into a typed SDK error.

        A runtime configured with a read-only registry (e.g.
        ``MACP_POLICIES_DIR`` for policies) advertises the corresponding
        capability as ``false`` in ``Initialize`` and rejects every mutating
        RPC with gRPC ``FAILED_PRECONDITION`` (``macp-error-code:
        REGISTRY_READ_ONLY`` on runtime ≥ 0.9.0). Surface that as a
        :class:`MacpAckError` with ``code="FAILED_PRECONDITION"`` and the
        registry code on ``macp_code``, mirroring how ``send``/``cancel_session``
        surface NACKs (and macp-sdk-typescript). Any other status stays a
        :class:`MacpTransportError` with the codes attached.
        """
        name = _rpc_status_name(rpc_err)
        macp_code = macp_error_code(rpc_err)
        if name == "FAILED_PRECONDITION":
            failure = AckFailure(
                code="FAILED_PRECONDITION",
                message=_rpc_details(rpc_err) or read_only_hint,
            )
            return MacpAckError(failure, macp_code=macp_code)
        return MacpTransportError(
            _rpc_details(rpc_err) or str(rpc_err),
            code=name,
            macp_code=macp_code,
        )

    @staticmethod
    def _resolve_sender(auth_cfg: AuthConfig, sender: str) -> str:
        """Resolve and validate the envelope sender against auth.expected_sender.

        Raises :class:`MacpIdentityMismatchError` when the resolved sender —
        whether explicitly passed or falling back to ``auth_cfg.sender_hint``
        — contradicts ``auth_cfg.expected_sender``. The check runs against
        whatever sender is actually resolved, not only the explicit-``sender``
        branch, so a ``sender_hint``/``expected_sender`` mismatch can't bypass
        the guard just by omitting ``sender``.
        """
        expected = auth_cfg.expected_sender
        resolved = sender or auth_cfg.sender or ""
        if expected is not None and resolved != expected:
            raise MacpIdentityMismatchError(expected=expected, actual=resolved)
        return resolved

    @staticmethod
    def _failure_from_ack(ack: envelope_pb2.Ack) -> AckFailure:
        """Build an :class:`AckFailure` from a NACK envelope, including reasons.

        Used by every RPC that returns an ``Ack`` (``send``, ``cancel_session``)
        so structured denial reasons (``POLICY_DENIED`` rule IDs, etc.) surface
        uniformly in ``MacpAckError.reasons`` no matter which call produced
        them.
        """
        error = ack.error
        details = _parse_ack_details(ack)
        return AckFailure(
            code=(error.code if error else "UNKNOWN"),
            message=(error.message if error else "runtime returned nack"),
            session_id=ack.session_id,
            message_id=ack.message_id,
            reasons=details.reasons,
            denials=details.denials,
        )

    @staticmethod
    def _ack_or_raise(ack: envelope_pb2.Ack, raise_on_nack: bool) -> envelope_pb2.Ack:
        """Return *ack*, raising :class:`MacpAckError` on a NACK when asked."""
        if raise_on_nack and not ack.ok:
            raise MacpAckError(_ClientCore._failure_from_ack(ack))
        return ack

    @staticmethod
    def _send_ack_or_raise(ack: envelope_pb2.Ack, raise_on_nack: bool) -> envelope_pb2.Ack:
        """Like :meth:`_ack_or_raise`, but a duplicate ack is success.

        Duplicate acks are idempotent success — the message was already
        accepted. This matches TypeScript SDK behaviour and is correct for
        retry scenarios.
        """
        if ack.duplicate:
            return ack
        return _ClientCore._ack_or_raise(ack, raise_on_nack)

    @staticmethod
    def _send_error_from_rpc(
        rpc_err: grpc.RpcError, envelope: envelope_pb2.Envelope
    ) -> MacpSdkError:
        """Map a ``Send`` RPC failure to the SDK error to raise."""
        # Runtime ≥ 0.9.0: the registry code rides `macp-error-code`, and
        # the gRPC status no longer identifies it (S13) — trust the key.
        # UNAUTHENTICATED included: a permanent code, so retry_send won't
        # retry it (macp-sdk-typescript parity).
        macp_code = macp_error_code(rpc_err)
        if macp_code is not None:
            details = _parse_grpc_metadata_details(rpc_err)
            failure = AckFailure(
                code=macp_code,
                message=_rpc_details(rpc_err) or macp_code,
                session_id=envelope.session_id,
                message_id=envelope.message_id,
                reasons=details.reasons,
                denials=details.denials,
            )
            return MacpAckError(failure, macp_code=macp_code)
        # Pre-0.9 runtimes: infer the MACP code from the gRPC status.
        code = rpc_err.code()
        if code == grpc.StatusCode.ALREADY_EXISTS:
            failure = AckFailure(
                code="SESSION_ALREADY_EXISTS",
                message=rpc_err.details() or "session already exists",
            )
            return MacpAckError(failure)
        if code == grpc.StatusCode.FAILED_PRECONDITION:
            details = _parse_grpc_metadata_details(rpc_err)
            failure = AckFailure(
                code="POLICY_DENIED",
                message=rpc_err.details() or "policy denied",
                reasons=details.reasons,
                denials=details.denials,
            )
            return MacpAckError(failure)
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            return MacpTransportError(
                _rpc_details(rpc_err) or "invalid argument",
                code=_rpc_status_name(rpc_err),
            )
        return _ClientCore._transport_error_from_rpc(rpc_err)

    def _initialize_request(self) -> core_pb2.InitializeRequest:
        return core_pb2.InitializeRequest(
            supported_protocol_versions=["1.0"],
            client_info=core_pb2.ClientInfo(
                name=self.client_name,
                title=self.client_name,
                version=self.client_version,
                description="Python SDK for the MACP runtime",
                website_url="",
            ),
            capabilities=_default_capabilities(),
        )

    @staticmethod
    def _what_if_replay_request(
        session_id: str, candidate: policy_pb2.PolicyDescriptor
    ) -> policy_pb2.WhatIfReplayRequest:
        return policy_pb2.WhatIfReplayRequest(session_id=session_id, candidate=candidate)

    def _require_capability(
        self, group: str, flag: str, *, rpc: str, ref: str, min_proto: str
    ) -> None:
        """Refuse locally unless the runtime advertised ``<group>.<flag>``.

        For RPCs a client MUST NOT call unless advertised. Uses the cached
        :attr:`server_capabilities`; callers initialize first if none are
        cached. Bindings older than ``min_proto`` lack the field, which reads
        as false, so the gate refuses there too.
        """
        caps = self.server_capabilities
        advertised = caps is not None and getattr(getattr(caps, group, None), flag, False)
        if not advertised:
            raise MacpSdkError(
                f"{rpc} refused: the runtime does not advertise "
                f"{group}.{flag} ({ref}); "
                f"it needs macp-proto >= {min_proto} and a runtime that implements it"
            )

    def _require_what_if_replay(self) -> None:
        """RFC-MACP-0012 §8.1: ``WhatIfReplay`` needs ``policy_registry.what_if_replay``."""
        self._require_capability(
            "policy_registry",
            "what_if_replay",
            rpc="WhatIfReplay",
            ref="RFC-MACP-0012 §8.1",
            min_proto="0.1.12",
        )

    @staticmethod
    def _cancel_session_request(
        session_id: str, reason: str, cancelled_by: str
    ) -> core_pb2.CancelSessionRequest:
        request_kwargs: dict[str, object] = {
            "session_id": session_id,
            "reason": reason,
        }
        # Forward-compatible: add cancelled_by if proto supports it
        if cancelled_by:
            has_field = any(
                f.name == "cancelled_by" for f in core_pb2.CancelSessionRequest.DESCRIPTOR.fields
            )
            if has_field:
                request_kwargs["cancelled_by"] = cancelled_by
        return core_pb2.CancelSessionRequest(**request_kwargs)

    @staticmethod
    def _check_ext_mode_descriptor(descriptor: core_pb2.ModeDescriptor) -> None:
        if "Commitment" not in descriptor.terminal_message_types:
            raise MacpSessionError(
                "ext-mode descriptor must declare 'Commitment' in "
                "terminal_message_types (runtime v0.5.0 rejects descriptors "
                "without a Commitment terminal type)"
            )

    def _signal_envelope(
        self,
        auth_cfg: AuthConfig,
        *,
        signal_type: str,
        data: bytes,
        confidence: float,
        correlation_session_id: str,
        sender: str,
    ) -> envelope_pb2.Envelope:
        payload = build_signal_payload(
            signal_type=signal_type,
            data=data,
            confidence=confidence,
            correlation_session_id=correlation_session_id,
        )
        return build_envelope(
            mode="",
            message_type="Signal",
            session_id="",
            payload=serialize_message(payload),
            sender=self._resolve_sender(auth_cfg, sender),
        )

    def _progress_envelope(
        self,
        auth_cfg: AuthConfig,
        *,
        session_id: str,
        mode: str,
        progress_token: str,
        progress: float,
        total: float,
        message: str,
        target_message_id: str,
        sender: str,
    ) -> envelope_pb2.Envelope:
        payload = build_progress_payload(
            progress_token=progress_token,
            progress=progress,
            total=total,
            message=message,
            target_message_id=target_message_id,
        )
        return build_envelope(
            mode=mode,
            message_type="Progress",
            session_id=session_id,
            payload=serialize_message(payload),
            sender=self._resolve_sender(auth_cfg, sender),
        )


class MacpClient(_ClientCore):
    """gRPC client for the MACP runtime.

    Transport security follows RFC-MACP-0004 §2 "Transport Security": "All
    MACP deployments MUST use encrypted transport" and "For gRPC over
    HTTP/2, TLS 1.2 or higher is REQUIRED", so ``secure`` defaults to
    ``True``. Plaintext gRPC is only available via the explicit
    ``allow_insecure=True`` opt-in, which is intended for local development
    against a runtime started with ``MACP_ALLOW_INSECURE=1``.

    ``secure`` and ``allow_insecure`` are deliberately not fully independent:
    when ``secure`` is left at its default (``None``), it is derived as
    ``not allow_insecure`` -- so passing ``allow_insecure=True`` alone is
    sufficient to select a plaintext channel, without also having to pass
    ``secure=False``. This is intentional, not a gap (issue #93 item 3): the
    common local-dev call is ``MacpClient(target=..., allow_insecure=True)``,
    and requiring a second, redundant flag to actually get the insecure
    channel that name asks for would be surprising. ``secure`` remains
    settable independently for the cases that need it explicit -- e.g.
    ``secure=False`` without ``allow_insecure=True`` still raises below, and
    ``secure=True`` with ``allow_insecure=True`` still builds a TLS channel
    (``allow_insecure`` only lifts the *requirement*, it never forces
    plaintext).
    """

    def __init__(
        self,
        *,
        target: str,
        secure: bool | None = None,
        allow_insecure: bool = False,
        auth: AuthConfig | None = None,
        root_certificates: bytes | None = None,
        default_timeout: float | None = None,
        client_name: str = "macp-sdk-python",
        client_version: str = "0.5.0",
    ) -> None:
        super().__init__(
            target=target,
            secure=secure,
            allow_insecure=allow_insecure,
            auth=auth,
            default_timeout=default_timeout,
            client_name=client_name,
            client_version=client_version,
        )
        if self.secure:
            creds = grpc.ssl_channel_credentials(root_certificates=root_certificates)
            self.channel = grpc.secure_channel(target, creds)
        else:
            logger.warning("MacpClient insecure channel to %s — allowed only for local dev", target)
            self.channel = grpc.insecure_channel(target)
        self.stub = core_pb2_grpc.MACPRuntimeServiceStub(self.channel)

    def close(self) -> None:
        self.channel.close()

    def __enter__(self) -> MacpClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def initialize(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.InitializeResponse:
        request = self._initialize_request()
        response = self.stub.Initialize(
            request,
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        self.server_capabilities = response.capabilities
        return response

    def send(
        self,
        envelope: envelope_pb2.Envelope,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        auth_cfg = self._require_auth(auth)
        try:
            response = self.stub.Send(
                core_pb2.SendRequest(envelope=envelope),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._send_error_from_rpc(rpc_err, envelope) from rpc_err
        return self._send_ack_or_raise(response.ack, raise_on_nack)

    def get_session(
        self,
        session_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.GetSessionResponse:
        auth_cfg = self._require_auth(auth)
        return self.stub.GetSession(
            core_pb2.GetSessionRequest(session_id=session_id),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    def cancel_session(
        self,
        session_id: str,
        *,
        reason: str,
        cancelled_by: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Terminate a session.

        Since runtime 0.4.0 / macp-proto 0.1.3 an accepted cancellation
        moves the session to ``SESSION_STATE_CANCELLED`` (previously
        ``EXPIRED``) and surfaces an ``EVENT_TYPE_CANCELLED`` lifecycle
        event. The returned ``Ack.session_state`` reflects ``CANCELLED``.
        """
        auth_cfg = self._require_auth(auth)
        try:
            response = self.stub.CancelSession(
                self._cancel_session_request(session_id, reason, cancelled_by),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    def suspend_session(
        self,
        session_id: str,
        *,
        reason: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Suspend an open session (macp-proto 0.1.3 / runtime 0.4.0).

        Moves the session to the non-terminal ``SESSION_STATE_SUSPENDED``
        state and emits an ``EVENT_TYPE_SUSPENDED`` lifecycle event. While
        suspended the runtime rejects messages sent to the session with a
        non-OPEN error; call :meth:`resume_session` to return it to OPEN.
        The returned ``Ack.session_state`` reflects ``SUSPENDED``.

        A suspension that outlasts the session-bound ``max_suspend_ms`` cap
        (set at ``SessionStart``; runtime v0.5.0) expires the session
        (``SUSPENDED`` → ``EXPIRED``), observed as an ``EVENT_TYPE_EXPIRED``
        lifecycle event. ``max_suspend_ms=0`` uses the runtime default
        (currently 7 days).
        """
        auth_cfg = self._require_auth(auth)
        try:
            response = self.stub.SuspendSession(
                core_pb2.SuspendSessionRequest(session_id=session_id, reason=reason),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    def resume_session(
        self,
        session_id: str,
        *,
        reason: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Resume a suspended session (macp-proto 0.1.3 / runtime 0.4.0).

        Returns a ``SESSION_STATE_SUSPENDED`` session to
        ``SESSION_STATE_OPEN`` and emits an ``EVENT_TYPE_RESUMED`` lifecycle
        event. The returned ``Ack.session_state`` reflects ``OPEN``.
        """
        auth_cfg = self._require_auth(auth)
        try:
            response = self.stub.ResumeSession(
                core_pb2.ResumeSessionRequest(session_id=session_id, reason=reason),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    def get_manifest(
        self,
        agent_id: str = "",
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.GetManifestResponse:
        return self.stub.GetManifest(
            core_pb2.GetManifestRequest(agent_id=agent_id),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    def list_modes(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListModesResponse:
        return self.stub.ListModes(
            core_pb2.ListModesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    def list_ext_modes(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListExtModesResponse:
        return self.stub.ListExtModes(
            core_pb2.ListExtModesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    def list_roots(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListRootsResponse:
        """List the roots the runtime exposes.

        The runtime does not populate roots yet and returns an empty list;
        since runtime v0.5.0 it advertises this up front via
        ``capabilities.roots.list_changed: false`` in ``Initialize``. The RPC
        is wired and forward-compatible -- callers should treat an empty list
        as "no roots advertised," not as an error.
        """
        return self.stub.ListRoots(
            core_pb2.ListRootsRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    def list_sessions(
        self,
        *,
        page_size: int = 0,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> list[core_pb2.SessionMetadata]:
        """List all active sessions known to the runtime.

        Auto-paginates (macp-proto >= 0.1.6 / runtime v0.5.0): repeatedly
        requests ``ListSessions`` with the runtime's ``next_page_token`` until
        it is empty, so the returned ``list`` is always the *complete* set
        regardless of the runtime's page size. Each entry includes
        ``context_id`` and ``extension_keys``. ``page_size`` (0 = server
        default) tunes the per-request batch. Use :meth:`list_sessions_page`
        for manual, single-page control.
        """
        auth_cfg = self._require_auth(auth)
        sessions: list[core_pb2.SessionMetadata] = []
        page_token = ""
        while True:
            batch, page_token = self.list_sessions_page(
                page_size=page_size,
                page_token=page_token,
                auth=auth_cfg,
                timeout=timeout,
            )
            sessions.extend(batch)
            if not page_token:
                return sessions

    def list_sessions_page(
        self,
        *,
        page_size: int = 0,
        page_token: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> tuple[list[core_pb2.SessionMetadata], str]:
        """Fetch a single page of sessions (macp-proto >= 0.1.6).

        Returns ``(sessions, next_page_token)``. An empty ``next_page_token``
        means the last page. Callers that want to drain all pages should use
        :meth:`list_sessions`, which loops this for them.
        """
        auth_cfg = self._require_auth(auth)
        resp = self.stub.ListSessions(
            core_pb2.ListSessionsRequest(page_size=page_size, page_token=page_token),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        return list(resp.sessions), resp.next_page_token

    def watch_sessions(
        self,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> Generator[core_pb2.WatchSessionsResponse, None, None]:
        """Server-streaming RPC: yields session lifecycle events.

        The runtime emits an initial ``EVENT_TYPE_CREATED`` frame for every
        currently-open session, then live events as sessions transition
        through ``CREATED``, ``RESOLVED``, ``EXPIRED``, ``CANCELLED``, and
        the non-terminal ``SUSPENDED`` / ``RESUMED`` pair (the latter three
        since macp-proto 0.1.3). Each event carries a full
        ``SessionMetadata`` (including ``context_id`` and ``extension_keys``),
        so callers can project run state without a follow-up ``get_session``.
        """
        logger.debug("watch_sessions starting")
        auth_cfg = self._require_auth(auth)
        call = self.stub.WatchSessions(
            core_pb2.WatchSessionsRequest(),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        try:
            yield from call
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            _cancel_quietly(call)

    def register_ext_mode(
        self,
        descriptor: core_pb2.ModeDescriptor,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.RegisterExtModeResponse:
        """Register an extension-mode descriptor with the runtime.

        Descriptors must declare ``Commitment`` among their
        ``terminal_message_types`` (runtime v0.5.0 rejects those that don't),
        and an ext session started without ``mode_version`` binds the
        registered descriptor's version. Against a read-only mode registry the
        runtime rejects this with gRPC ``FAILED_PRECONDITION``, surfaced here
        as :class:`MacpAckError`.

        Raises :class:`MacpSessionError` client-side if the descriptor omits
        ``Commitment`` from ``terminal_message_types`` (runtime v0.5.0 requires
        it), giving a clearer error than the runtime's rejection.
        """
        self._check_ext_mode_descriptor(descriptor)
        auth_cfg = self._require_auth(auth)
        try:
            return self.stub.RegisterExtMode(
                core_pb2.RegisterExtModeRequest(mode_descriptor=descriptor),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="RegisterExtMode refused: the mode registry is read-only",
            ) from rpc_err

    def unregister_ext_mode(
        self,
        mode: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.UnregisterExtModeResponse:
        auth_cfg = self._require_auth(auth)
        try:
            return self.stub.UnregisterExtMode(
                core_pb2.UnregisterExtModeRequest(mode=mode),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="UnregisterExtMode refused: the mode registry is read-only",
            ) from rpc_err

    def promote_mode(
        self,
        mode: str,
        promoted_mode_name: str = "",
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.PromoteModeResponse:
        """Promote a registered extension mode to a first-class mode.

        Runtime v0.5.0 rejects promotion into the reserved ``macp.mode.*``
        namespace. Against a read-only mode registry the runtime rejects this
        with gRPC ``FAILED_PRECONDITION``, surfaced here as
        :class:`MacpAckError`.
        """
        auth_cfg = self._require_auth(auth)
        try:
            return self.stub.PromoteMode(
                core_pb2.PromoteModeRequest(mode=mode, promoted_mode_name=promoted_mode_name),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="PromoteMode refused: the mode registry is read-only",
            ) from rpc_err

    # ── Governance policy lifecycle ───────────────────────────────────

    def register_policy(
        self,
        descriptor: policy_pb2.PolicyDescriptor,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.RegisterPolicyResponse:
        """Register a governance policy with the runtime.

        Raises :class:`MacpAckError` with ``code="FAILED_PRECONDITION"`` when
        the runtime's policy registry is read-only — a runtime started with
        ``MACP_POLICIES_DIR`` advertises ``policy_registry.register_policy:
        false`` in :meth:`initialize` and refuses all mutating policy RPCs.
        Check that capability before registering to avoid the round-trip.
        """
        auth_cfg = self._require_auth(auth)
        try:
            return self.stub.RegisterPolicy(
                policy_pb2.RegisterPolicyRequest(policy_descriptor=descriptor),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="RegisterPolicy refused: the policy registry is read-only "
                "(runtime configured with MACP_POLICIES_DIR; "
                "Initialize advertises policy_registry.register_policy=false)",
            ) from rpc_err

    def unregister_policy(
        self,
        policy_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.UnregisterPolicyResponse:
        """Unregister a governance policy from the runtime.

        Like :meth:`register_policy`, raises :class:`MacpAckError` with
        ``code="FAILED_PRECONDITION"`` against a read-only registry.
        """
        auth_cfg = self._require_auth(auth)
        try:
            return self.stub.UnregisterPolicy(
                policy_pb2.UnregisterPolicyRequest(policy_id=policy_id),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="UnregisterPolicy refused: the policy registry is read-only "
                "(runtime configured with MACP_POLICIES_DIR)",
            ) from rpc_err

    def get_policy(
        self,
        policy_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.GetPolicyResponse:
        """Retrieve a single governance policy by ID."""
        auth_cfg = self._require_auth(auth)
        return self.stub.GetPolicy(
            policy_pb2.GetPolicyRequest(policy_id=policy_id),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    def list_policies(
        self,
        mode: str | None = None,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.ListPoliciesResponse:
        """List registered governance policies, optionally filtered by mode."""
        auth_cfg = self._require_auth(auth)
        return self.stub.ListPolicies(
            policy_pb2.ListPoliciesRequest(mode=mode or ""),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    def what_if_replay(
        self,
        session_id: str,
        candidate: policy_pb2.PolicyDescriptor,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.WhatIfReplayResponse:
        """Replay a session's history under a candidate policy (RFC-MACP-0012 §8.1).

        Read-only and advisory: the runtime replays the session's accepted
        history under its bound policy and under ``candidate``, and reports
        both outcomes, ``first_divergence`` (check ``HasField``: index 0 is
        the ``SessionStart``), every candidate ``POLICY_DENIED`` and one entry
        per history entry. The candidate is admission-validated but never
        registered. Byte fields stay raw: decode a denial's ``details`` with
        :func:`macp_sdk.errors.parse_error_details`.

        Gated on ``policy_registry.what_if_replay``: if :meth:`initialize`
        has not run, it runs once first, and if the flag is false this raises
        :class:`MacpSdkError` without sending the RPC. RPC failures raise
        :class:`MacpTransportError` with ``macp_code`` set, e.g.
        ``HISTORY_UNAVAILABLE`` (compacted history), ``SESSION_NOT_FOUND``,
        ``FORBIDDEN`` or ``INVALID_POLICY_DEFINITION``.
        """
        auth_cfg = self._require_auth(auth)
        try:
            if self.server_capabilities is None:
                self.initialize(auth=auth_cfg, timeout=timeout)
            self._require_what_if_replay()
            return self.stub.WhatIfReplay(
                self._what_if_replay_request(session_id, candidate),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err

    def watch_policies(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> Generator[policy_pb2.WatchPoliciesResponse, None, None]:
        """Server-streaming RPC: yields governance policy change events.

        Auth is forwarded when available (``auth`` arg or ``client.auth``) but
        not required. A lagging consumer is terminated with
        ``RESOURCE_EXHAUSTED`` (``macp_code="STREAM_LAGGED"`` on runtime
        ≥ 0.9.0); reconnect to resume.
        """
        logger.debug("watch_policies starting")
        call = self.stub.WatchPolicies(
            policy_pb2.WatchPoliciesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        try:
            yield from call
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            _cancel_quietly(call)

    def open_stream(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> MacpStream:
        auth_cfg = self._require_auth(auth)
        return MacpStream(
            self.stub,
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    def watch_mode_registry(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> Generator[core_pb2.WatchModeRegistryResponse, None, None]:
        """Server-streaming RPC: yields mode registry change events.

        Auth is forwarded when available but not required.
        """
        logger.debug("watch_mode_registry starting")
        call = self.stub.WatchModeRegistry(
            core_pb2.WatchModeRegistryRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        try:
            yield from call
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            _cancel_quietly(call)

    def watch_roots(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> Generator[core_pb2.WatchRootsResponse, None, None]:
        """Server-streaming RPC: yields root change events.

        The runtime advertises ``roots.list_changed: false`` and does not yet
        populate roots, so this stream idles. Auth is forwarded when available
        but not required.
        """
        logger.debug("watch_roots starting")
        call = self.stub.WatchRoots(
            core_pb2.WatchRootsRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        try:
            yield from call
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            _cancel_quietly(call)

    def watch_signals(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> Generator[core_pb2.WatchSignalsResponse, None, None]:
        """Server-streaming RPC: yields ambient signal envelopes.

        Requires authentication since runtime v0.5.0 — an unauthenticated
        ``WatchSignals`` is rejected with gRPC ``UNAUTHENTICATED`` (surfaced
        as ``MacpTransportError(code="UNAUTHENTICATED")``). A lagging consumer
        is terminated with ``RESOURCE_EXHAUSTED`` (``macp_code="STREAM_LAGGED"``
        on runtime ≥ 0.9.0); reconnect to resume.
        """
        logger.debug("watch_signals starting")
        auth_cfg = self._require_auth(auth)
        call = self.stub.WatchSignals(
            core_pb2.WatchSignalsRequest(),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        try:
            yield from call
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            _cancel_quietly(call)

    def send_signal(
        self,
        *,
        signal_type: str,
        data: bytes = b"",
        confidence: float = 0.0,
        correlation_session_id: str = "",
        sender: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> envelope_pb2.Ack:
        """Send an ambient (non-session) signal to the runtime."""
        validate_signal_type(signal_type, data)
        auth_cfg = self._require_auth(auth)
        envelope = self._signal_envelope(
            auth_cfg,
            signal_type=signal_type,
            data=data,
            confidence=confidence,
            correlation_session_id=correlation_session_id,
            sender=sender,
        )
        return self.send(envelope, auth=auth_cfg, timeout=timeout)

    def send_progress(
        self,
        *,
        session_id: str = "",
        mode: str = "",
        progress_token: str,
        progress: float,
        total: float,
        message: str = "",
        target_message_id: str = "",
        sender: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> envelope_pb2.Ack:
        """Send a progress update.

        Per RFC-MACP-0001 §6, ``Progress`` is legal in exactly two shapes:

        * *ambient* -- ``session_id`` and ``mode`` both empty, routed through
          the signal broadcast path; or
        * *session-scoped* -- both non-empty.

        Supplying exactly one of the two raises ``MacpSessionError`` naming
        the mismatched field. The runtime rejects that mixed envelope with
        ``INVALID_ENVELOPE``, so this only surfaces the failure earlier and
        more clearly.
        """
        validate_progress_scope(session_id, mode)
        auth_cfg = self._require_auth(auth)
        envelope = self._progress_envelope(
            auth_cfg,
            session_id=session_id,
            mode=mode,
            progress_token=progress_token,
            progress=progress,
            total=total,
            message=message,
            target_message_id=target_message_id,
            sender=sender,
        )
        return self.send(envelope, auth=auth_cfg, timeout=timeout)
