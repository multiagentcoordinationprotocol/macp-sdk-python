"""Asyncio client for the MACP runtime, built on ``grpc.aio``.

:class:`AsyncMacpClient` mirrors :class:`macp_sdk.client.MacpClient` method
for method: the same constructor (``target``, ``auth``, ``allow_insecure``,
``secure``, ``root_certificates``, ``default_timeout``), the same per-call
``auth=`` / ``timeout=`` overrides, and the same error mapping. Unary RPCs are
coroutines, server-streaming ``watch_*`` RPCs are async iterators, and
:meth:`AsyncMacpClient.open_stream` returns an :class:`AsyncMacpStream`.

Request construction, auth resolution, timeout semantics and the
``grpc.RpcError`` → SDK-error mapping live in the shared
:class:`macp_sdk.client._ClientCore`, so the sync and async clients cannot
drift apart on any of them.

Create the client inside a running event loop (``async with
AsyncMacpClient(...)``): ``grpc.aio`` binds a channel to the loop that is
current when it is created.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any

import grpc
from grpc import aio
from macp.v1 import core_pb2, core_pb2_grpc, envelope_pb2, policy_pb2

from ._logging import logger
from .auth import AuthConfig
from .client import (
    InlineErrorCallback,
    TimeoutValue,
    _classify_stream_response,
    _ClientCore,
    _rpc_details,
    _rpc_status_name,
    macp_error_code,
)
from .errors import MacpSdkError, MacpTimeoutError, MacpTransportError
from .validation import validate_progress_scope, validate_signal_type

__all__ = ["AsyncMacpClient", "AsyncMacpStream"]

_END = object()


class AsyncMacpStream:
    """Asyncio counterpart of :class:`macp_sdk.client.MacpStream`.

    Wraps one bidirectional ``StreamSession`` call. A background task reads
    the server side into a queue, so :meth:`read` can time out without
    cancelling the underlying call (``grpc.aio`` cancels the whole RPC when
    an in-flight ``read()`` is cancelled). Must be constructed inside a
    running event loop; :meth:`AsyncMacpClient.open_stream` does that for you.

    Use it as an async context manager to guarantee the call is torn down::

        async with client.open_stream() as stream:
            await stream.send_subscribe(session_id)
            async for envelope in stream.responses(timeout=5.0):
                ...
    """

    def __init__(
        self,
        stub: core_pb2_grpc.MACPRuntimeServiceStub,
        *,
        metadata: Sequence[tuple[str, str]],
        timeout: float | None = None,
    ) -> None:
        self._responses: asyncio.Queue[object] = asyncio.Queue()
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._inline_error_callbacks: list[InlineErrorCallback] = []
        self._call: Any = stub.StreamSession(metadata=metadata, timeout=timeout)
        self._pump = asyncio.get_running_loop().create_task(self._pump_responses())

    async def _pump_responses(self) -> None:
        try:
            while True:
                response = await self._call.read()
                if response is aio.EOF:  # type: ignore[attr-defined]  # stubs omit EOF
                    return
                envelope, error = _classify_stream_response(response)
                if error is not None:
                    for cb in self._inline_error_callbacks:
                        cb(error)
                    logger.warning("inline stream error: %s", error)
                elif envelope is not None:
                    self._responses.put_nowait(envelope)
        except grpc.RpcError as exc:
            self._responses.put_nowait(exc)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the pump task itself was cancelled (loop shutdown)
            # grpc.aio reports a locally cancelled call as CancelledError;
            # surface it the way MacpStream does: MacpTransportError(CANCELLED).
            self._responses.put_nowait(
                MacpTransportError("stream cancelled", code="CANCELLED", macp_code=None)
            )
        finally:
            self._responses.put_nowait(_END)

    def on_inline_error(self, callback: InlineErrorCallback) -> None:
        """Register a callback for inline application-level stream errors.

        Same contract as :meth:`macp_sdk.client.MacpStream.on_inline_error`:
        the callback receives the ``macp.v1.MACPError`` frame, and the stream
        stays open.
        """
        self._inline_error_callbacks.append(callback)

    async def _write(self, request: core_pb2.StreamSessionRequest) -> None:
        if self._closed:
            raise MacpSdkError("stream is already closed")
        async with self._write_lock:
            try:
                await self._call.write(request)
            except asyncio.InvalidStateError as exc:
                raise MacpSdkError(f"stream is no longer writable: {exc}") from exc
            except grpc.RpcError as exc:
                raise _ClientCore._transport_error_from_rpc(exc) from exc

    async def send(self, envelope: envelope_pb2.Envelope) -> None:
        """Write an envelope to the stream."""
        await self._write(core_pb2.StreamSessionRequest(envelope=envelope))

    async def send_subscribe(self, session_id: str, after_sequence: int = 0) -> None:
        """Send a subscribe-only frame (RFC-MACP-0006-A1).

        Same semantics as :meth:`macp_sdk.client.MacpStream.send_subscribe`:
        ``after_sequence`` is exclusive, ``0`` replays from the start.
        """
        await self._write(
            core_pb2.StreamSessionRequest(
                subscribe_session_id=session_id,
                after_sequence=after_sequence,
            )
        )

    async def read(self, timeout: float | None = None) -> envelope_pb2.Envelope | None:
        """Return the next envelope, or ``None`` once the stream has ended.

        Raises :class:`MacpTimeoutError` if nothing arrives within
        ``timeout`` seconds (the stream stays open), and
        :class:`MacpTransportError` if the call failed or was cancelled.
        """
        try:
            item = await asyncio.wait_for(self._responses.get(), timeout)
        except TimeoutError:
            raise MacpTimeoutError(f"stream read timed out after {timeout}s") from None
        if item is _END:
            # Keep end-of-stream durably observable for repeated reads.
            self._responses.put_nowait(_END)
            return None
        if isinstance(item, MacpTransportError):
            raise item
        if isinstance(item, grpc.RpcError):
            raise MacpTransportError(
                _rpc_details(item) or str(item),
                code=_rpc_status_name(item),
                macp_code=macp_error_code(item),
            )
        assert isinstance(item, envelope_pb2.Envelope)
        return item

    async def responses(self, timeout: float | None = None) -> AsyncIterator[envelope_pb2.Envelope]:
        """Async-iterate envelopes until the stream ends."""
        while True:
            envelope = await self.read(timeout=timeout)
            if envelope is None:
                return
            yield envelope

    async def close(self) -> None:
        """Half-close the request side; the server ends the stream when done."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._call.done_writing()
        except (grpc.RpcError, asyncio.InvalidStateError):
            logger.debug("stream done_writing failed", exc_info=True)

    def cancel(self) -> None:
        """Abort the call immediately.

        The next :meth:`read` raises ``MacpTransportError(code="CANCELLED")``
        (as with :meth:`macp_sdk.client.MacpStream.cancel`), then reads return
        ``None``. Safe to call more than once.
        """
        self._closed = True
        self._call.cancel()

    async def __aenter__(self) -> AsyncMacpStream:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.cancel()
        # Let the pump observe the cancellation and finish.
        await asyncio.gather(self._pump, return_exceptions=True)


class AsyncMacpClient(_ClientCore):
    """Asyncio gRPC client for the MACP runtime (``grpc.aio``).

    Same constructor, auth model and transport-security rule as
    :class:`macp_sdk.client.MacpClient` — TLS by default, plaintext only with
    ``allow_insecure=True`` (local development against a runtime started with
    ``MACP_ALLOW_INSECURE=1``). Every RPC method has the same name and
    arguments as on ``MacpClient``; unary methods are coroutines and
    ``watch_*`` methods are async iterators::

        async with AsyncMacpClient(
            target="127.0.0.1:50051",
            allow_insecure=True,
            auth=AuthConfig.for_dev_agent("coordinator"),
        ) as client:
            await client.initialize()
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
            self.channel = aio.secure_channel(target, creds)
        else:
            logger.warning(
                "AsyncMacpClient insecure channel to %s — allowed only for local dev", target
            )
            self.channel = aio.insecure_channel(target)
        self.stub = core_pb2_grpc.MACPRuntimeServiceStub(self.channel)

    async def close(self) -> None:
        await self.channel.close(None)

    async def __aenter__(self) -> AsyncMacpClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def initialize(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.InitializeResponse:
        return await self.stub.Initialize(
            self._initialize_request(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    async def send(
        self,
        envelope: envelope_pb2.Envelope,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Send one envelope. Same error contract as :meth:`MacpClient.send`."""
        auth_cfg = self._require_auth(auth)
        try:
            response = await self.stub.Send(
                core_pb2.SendRequest(envelope=envelope),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._send_error_from_rpc(rpc_err, envelope) from rpc_err
        return self._send_ack_or_raise(response.ack, raise_on_nack)

    async def get_session(
        self,
        session_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.GetSessionResponse:
        auth_cfg = self._require_auth(auth)
        return await self.stub.GetSession(
            core_pb2.GetSessionRequest(session_id=session_id),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    async def cancel_session(
        self,
        session_id: str,
        *,
        reason: str,
        cancelled_by: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Terminate a session (``SESSION_STATE_CANCELLED``)."""
        auth_cfg = self._require_auth(auth)
        try:
            response = await self.stub.CancelSession(
                self._cancel_session_request(session_id, reason, cancelled_by),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    async def suspend_session(
        self,
        session_id: str,
        *,
        reason: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Suspend an open session (see :meth:`MacpClient.suspend_session`)."""
        auth_cfg = self._require_auth(auth)
        try:
            response = await self.stub.SuspendSession(
                core_pb2.SuspendSessionRequest(session_id=session_id, reason=reason),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    async def resume_session(
        self,
        session_id: str,
        *,
        reason: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
        raise_on_nack: bool = True,
    ) -> envelope_pb2.Ack:
        """Resume a suspended session (see :meth:`MacpClient.resume_session`)."""
        auth_cfg = self._require_auth(auth)
        try:
            response = await self.stub.ResumeSession(
                core_pb2.ResumeSessionRequest(session_id=session_id, reason=reason),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._transport_error_from_rpc(rpc_err) from rpc_err
        return self._ack_or_raise(response.ack, raise_on_nack)

    async def get_manifest(
        self,
        agent_id: str = "",
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.GetManifestResponse:
        return await self.stub.GetManifest(
            core_pb2.GetManifestRequest(agent_id=agent_id),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    async def list_modes(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListModesResponse:
        return await self.stub.ListModes(
            core_pb2.ListModesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    async def list_ext_modes(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListExtModesResponse:
        return await self.stub.ListExtModes(
            core_pb2.ListExtModesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    async def list_roots(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> core_pb2.ListRootsResponse:
        """List roots (the runtime returns an empty list today)."""
        return await self.stub.ListRoots(
            core_pb2.ListRootsRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )

    async def list_sessions(
        self,
        *,
        page_size: int = 0,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> list[core_pb2.SessionMetadata]:
        """List all sessions, draining every page (see :meth:`MacpClient.list_sessions`)."""
        auth_cfg = self._require_auth(auth)
        sessions: list[core_pb2.SessionMetadata] = []
        page_token = ""
        while True:
            batch, page_token = await self.list_sessions_page(
                page_size=page_size,
                page_token=page_token,
                auth=auth_cfg,
                timeout=timeout,
            )
            sessions.extend(batch)
            if not page_token:
                return sessions

    async def list_sessions_page(
        self,
        *,
        page_size: int = 0,
        page_token: str = "",
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> tuple[list[core_pb2.SessionMetadata], str]:
        """Fetch one page of sessions; returns ``(sessions, next_page_token)``."""
        auth_cfg = self._require_auth(auth)
        resp = await self.stub.ListSessions(
            core_pb2.ListSessionsRequest(page_size=page_size, page_token=page_token),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        return list(resp.sessions), resp.next_page_token

    async def _stream(self, call: Any) -> AsyncGenerator[Any, None]:
        """Drive a server-streaming call, mapping errors and always cancelling."""
        try:
            async for item in call:
                yield item
        except grpc.RpcError as exc:
            raise self._transport_error_from_rpc(exc) from exc
        finally:
            try:
                call.cancel()
            except Exception:
                logger.debug("stream cancel failed", exc_info=True)

    async def watch_sessions(
        self,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> AsyncIterator[core_pb2.WatchSessionsResponse]:
        """Server-streaming RPC: async-iterate session lifecycle events."""
        logger.debug("watch_sessions starting")
        auth_cfg = self._require_auth(auth)
        call = self.stub.WatchSessions(
            core_pb2.WatchSessionsRequest(),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        async with contextlib.aclosing(self._stream(call)) as items:
            async for item in items:
                yield item

    async def register_ext_mode(
        self,
        descriptor: core_pb2.ModeDescriptor,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.RegisterExtModeResponse:
        """Register an extension-mode descriptor (see :meth:`MacpClient.register_ext_mode`)."""
        self._check_ext_mode_descriptor(descriptor)
        auth_cfg = self._require_auth(auth)
        try:
            return await self.stub.RegisterExtMode(
                core_pb2.RegisterExtModeRequest(mode_descriptor=descriptor),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="RegisterExtMode refused: the mode registry is read-only",
            ) from rpc_err

    async def unregister_ext_mode(
        self,
        mode: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.UnregisterExtModeResponse:
        auth_cfg = self._require_auth(auth)
        try:
            return await self.stub.UnregisterExtMode(
                core_pb2.UnregisterExtModeRequest(mode=mode),
                metadata=self._metadata(auth_cfg),
                timeout=self._resolve_timeout(timeout),
            )
        except grpc.RpcError as rpc_err:
            raise self._map_registry_mutation_error(
                rpc_err,
                read_only_hint="UnregisterExtMode refused: the mode registry is read-only",
            ) from rpc_err

    async def promote_mode(
        self,
        mode: str,
        promoted_mode_name: str = "",
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> core_pb2.PromoteModeResponse:
        """Promote a registered extension mode (see :meth:`MacpClient.promote_mode`)."""
        auth_cfg = self._require_auth(auth)
        try:
            return await self.stub.PromoteMode(
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

    async def register_policy(
        self,
        descriptor: policy_pb2.PolicyDescriptor,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.RegisterPolicyResponse:
        """Register a governance policy (see :meth:`MacpClient.register_policy`)."""
        auth_cfg = self._require_auth(auth)
        try:
            return await self.stub.RegisterPolicy(
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

    async def unregister_policy(
        self,
        policy_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.UnregisterPolicyResponse:
        """Unregister a governance policy (see :meth:`MacpClient.unregister_policy`)."""
        auth_cfg = self._require_auth(auth)
        try:
            return await self.stub.UnregisterPolicy(
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

    async def get_policy(
        self,
        policy_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.GetPolicyResponse:
        """Retrieve a single governance policy by ID."""
        auth_cfg = self._require_auth(auth)
        return await self.stub.GetPolicy(
            policy_pb2.GetPolicyRequest(policy_id=policy_id),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    async def list_policies(
        self,
        mode: str | None = None,
        *,
        auth: AuthConfig | None = None,
        timeout: TimeoutValue = None,
    ) -> policy_pb2.ListPoliciesResponse:
        """List registered governance policies, optionally filtered by mode."""
        auth_cfg = self._require_auth(auth)
        return await self.stub.ListPolicies(
            policy_pb2.ListPoliciesRequest(mode=mode or ""),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    async def watch_policies(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> AsyncIterator[policy_pb2.WatchPoliciesResponse]:
        """Server-streaming RPC: async-iterate governance policy change events."""
        logger.debug("watch_policies starting")
        call = self.stub.WatchPolicies(
            policy_pb2.WatchPoliciesRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        async with contextlib.aclosing(self._stream(call)) as items:
            async for item in items:
                yield item

    def open_stream(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> AsyncMacpStream:
        """Open a bidirectional ``StreamSession``. Call from a running loop."""
        auth_cfg = self._require_auth(auth)
        return AsyncMacpStream(
            self.stub,
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )

    async def watch_mode_registry(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> AsyncIterator[core_pb2.WatchModeRegistryResponse]:
        """Server-streaming RPC: async-iterate mode registry change events."""
        logger.debug("watch_mode_registry starting")
        call = self.stub.WatchModeRegistry(
            core_pb2.WatchModeRegistryRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        async with contextlib.aclosing(self._stream(call)) as items:
            async for item in items:
                yield item

    async def watch_roots(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> AsyncIterator[core_pb2.WatchRootsResponse]:
        """Server-streaming RPC: async-iterate root change events (idles today)."""
        logger.debug("watch_roots starting")
        call = self.stub.WatchRoots(
            core_pb2.WatchRootsRequest(),
            metadata=self._metadata(auth),
            timeout=self._resolve_timeout(timeout),
        )
        async with contextlib.aclosing(self._stream(call)) as items:
            async for item in items:
                yield item

    async def watch_signals(
        self, *, auth: AuthConfig | None = None, timeout: TimeoutValue = None
    ) -> AsyncIterator[core_pb2.WatchSignalsResponse]:
        """Server-streaming RPC: async-iterate ambient signal envelopes (auth required)."""
        logger.debug("watch_signals starting")
        auth_cfg = self._require_auth(auth)
        call = self.stub.WatchSignals(
            core_pb2.WatchSignalsRequest(),
            metadata=self._metadata(auth_cfg),
            timeout=self._resolve_timeout(timeout),
        )
        async with contextlib.aclosing(self._stream(call)) as items:
            async for item in items:
                yield item

    async def send_signal(
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
        return await self.send(envelope, auth=auth_cfg, timeout=timeout)

    async def send_progress(
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
        """Send a progress update (see :meth:`MacpClient.send_progress`)."""
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
        return await self.send(envelope, auth=auth_cfg, timeout=timeout)
