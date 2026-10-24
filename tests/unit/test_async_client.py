"""Unit tests for ``AsyncMacpClient`` / ``AsyncMacpStream`` (``grpc.aio``).

The gRPC stub is replaced with mocks whose unary methods are ``AsyncMock``s
and whose streaming methods return fake ``grpc.aio`` call objects, so every
test runs without a runtime. Each test drives its coroutine with
``asyncio.run`` (the suite has no pytest-asyncio dependency).

``TestSurfaceParity`` is the drift guard: every public ``MacpClient`` method
must exist on ``AsyncMacpClient`` with an identical signature, as a
coroutine function (unary RPCs) or async-generator function (``watch_*``).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from grpc import aio
from macp.v1 import core_pb2, envelope_pb2, policy_pb2

from macp_sdk import (
    AsyncMacpClient,
    AsyncMacpStream,
    AuthConfig,
    MacpAckError,
    MacpClient,
    MacpSdkError,
    MacpTimeoutError,
    MacpTransportError,
)
from macp_sdk.client import UNBOUNDED
from macp_sdk.errors import MacpIdentityMismatchError
from tests.conftest import VALID_SESSION_ID, FakeRpcError, make_ack

T = TypeVar("T")


def run(factory: Callable[[], Awaitable[T]]) -> T:
    async def wrapper() -> T:
        return await factory()

    return asyncio.run(wrapper())


class AioRpcError(FakeRpcError):
    """FakeRpcError whose trailing metadata iterates as plain tuples, like grpc.aio."""

    def trailing_metadata(self) -> list[tuple[str, str | bytes]]:  # type: ignore[override]
        return [(m.key, m.value) for m in self._trailing]


async def _client(
    *, auth: AuthConfig | None = None, **kwargs: Any
) -> tuple[AsyncMacpClient, MagicMock]:
    auth = auth or AuthConfig.for_bearer("tok", sender_hint="alice")
    client = AsyncMacpClient(target="localhost:0", allow_insecure=True, auth=auth, **kwargs)
    stub = MagicMock()
    client.stub = stub
    return client, stub


def _envelope(session_id: str = VALID_SESSION_ID) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode="macp.mode.decision.v1",
        message_type="Proposal",
        message_id="m1",
        session_id=session_id,
        sender="alice",
    )


class FakeStreamCall:
    """A grpc.aio unary-stream call: async-iterable, with ``cancel()``."""

    def __init__(self, items: list[Any], error: Exception | None = None) -> None:
        self._items = list(items)
        self._error = error
        self.cancelled = False

    def __aiter__(self) -> FakeStreamCall:
        return self

    async def __anext__(self) -> Any:
        if self._items:
            return self._items.pop(0)
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class FakeBidiCall:
    """A grpc.aio stream-stream call driven by ``read``/``write``/``done_writing``."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.written: list[core_pb2.StreamSessionRequest] = []
        self.write_error: BaseException | None = None
        self.done_writing_error: BaseException | None = None
        self.half_closed = False
        self.cancelled = False

    async def read(self) -> Any:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def write(self, request: core_pb2.StreamSessionRequest) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append(request)

    async def done_writing(self) -> None:
        if self.done_writing_error is not None:
            raise self.done_writing_error
        self.half_closed = True

    def cancel(self) -> bool:
        self.cancelled = True
        self.inbox.put_nowait(asyncio.CancelledError())
        return True


def _stream_response(envelope: envelope_pb2.Envelope) -> core_pb2.StreamSessionResponse:
    return core_pb2.StreamSessionResponse(envelope=envelope)


# ── Surface parity ────────────────────────────────────────────────────


def _public_methods(cls: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in vars(cls).items()
        if callable(member) and not name.startswith("_")
    }


class TestSurfaceParity:
    def test_every_sync_method_has_async_twin_with_same_signature(self) -> None:
        sync = _public_methods(MacpClient)
        async_ = _public_methods(AsyncMacpClient)
        assert set(sync) == set(async_)
        for name, fn in sync.items():
            # Return annotations differ by design (Generator vs AsyncIterator).
            sync_params = inspect.signature(fn).parameters
            assert sync_params == inspect.signature(async_[name]).parameters, name

    def test_unary_methods_are_coroutines_and_watches_are_async_generators(self) -> None:
        for name, fn in _public_methods(AsyncMacpClient).items():
            if name == "open_stream":
                assert not inspect.iscoroutinefunction(fn)
            elif name.startswith("watch_"):
                assert inspect.isasyncgenfunction(fn), name
            else:
                assert inspect.iscoroutinefunction(fn), name

    def test_stream_surface_matches_sync_stream(self) -> None:
        from macp_sdk import MacpStream

        sync = set(_public_methods(MacpStream))
        assert sync == set(_public_methods(AsyncMacpStream))


# ── Construction and lifecycle ────────────────────────────────────────


class TestConstruction:
    def test_plaintext_requires_allow_insecure(self) -> None:
        async def go() -> None:
            with pytest.raises(MacpSdkError):
                AsyncMacpClient(target="localhost:0", secure=False)

        run(go)

    def test_secure_channel_by_default(self) -> None:
        async def go() -> None:
            async with AsyncMacpClient(target="localhost:0", secure=True) as client:
                assert client.secure is True
                assert isinstance(client.channel, aio.Channel)

        run(go)

    def test_async_context_manager_closes_channel(self) -> None:
        async def go() -> None:
            client, _ = await _client()
            client.channel = MagicMock()
            client.channel.close = AsyncMock()
            async with client as entered:
                assert entered is client
            client.channel.close.assert_awaited_once_with(None)

        run(go)


# ── Unary RPCs ────────────────────────────────────────────────────────


class TestSend:
    def test_ok_ack_returned(self) -> None:
        async def go() -> envelope_pb2.Ack:
            client, stub = await _client()
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=make_ack(True)))
            ack = await client.send(_envelope(), timeout=2.0)
            _, kwargs = stub.Send.call_args
            assert kwargs["timeout"] == 2.0
            assert ("authorization", "Bearer tok") in kwargs["metadata"]
            return ack

        assert run(go).ok

    def test_nack_raises(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=make_ack(False)))
            with pytest.raises(MacpAckError) as exc:
                await client.send(_envelope())
            assert exc.value.failure.code == "TEST_ERROR"

        run(go)

    def test_nack_returned_when_not_raising(self) -> None:
        async def go() -> envelope_pb2.Ack:
            client, stub = await _client()
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=make_ack(False)))
            return await client.send(_envelope(), raise_on_nack=False)

        assert not run(go).ok

    def test_rpc_error_with_aio_tuple_metadata_maps_registry_code(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.Send = AsyncMock(
                side_effect=AioRpcError(
                    grpc.StatusCode.PERMISSION_DENIED,
                    "nope",
                    trailing_metadata=[("macp-error-code", "FORBIDDEN")],
                )
            )
            with pytest.raises(MacpAckError) as exc:
                await client.send(_envelope())
            assert exc.value.macp_code == "FORBIDDEN"
            assert exc.value.failure.session_id == VALID_SESSION_ID

        run(go)

    def test_rpc_error_without_code_is_transport_error(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.Send = AsyncMock(side_effect=AioRpcError(grpc.StatusCode.UNAVAILABLE, "down"))
            with pytest.raises(MacpTransportError) as exc:
                await client.send(_envelope())
            assert exc.value.code == "UNAVAILABLE"

        run(go)

    def test_send_requires_auth(self) -> None:
        async def go() -> None:
            async with AsyncMacpClient(target="localhost:0", allow_insecure=True) as client:
                with pytest.raises(MacpSdkError):
                    await client.send(_envelope())

        run(go)


class TestSessionLifecycleRpcs:
    @pytest.mark.parametrize(
        ("method", "rpc", "response_cls", "kwargs"),
        [
            ("cancel_session", "CancelSession", core_pb2.CancelSessionResponse, {"reason": "r"}),
            ("suspend_session", "SuspendSession", core_pb2.SuspendSessionResponse, {}),
            ("resume_session", "ResumeSession", core_pb2.ResumeSessionResponse, {}),
        ],
    )
    def test_ack_and_errors(
        self, method: str, rpc: str, response_cls: Any, kwargs: dict[str, Any]
    ) -> None:
        async def go() -> None:
            client, stub = await _client()
            setattr(stub, rpc, AsyncMock(return_value=response_cls(ack=make_ack(True))))
            ack = await getattr(client, method)(VALID_SESSION_ID, **kwargs)
            assert ack.ok

            setattr(stub, rpc, AsyncMock(return_value=response_cls(ack=make_ack(False))))
            with pytest.raises(MacpAckError):
                await getattr(client, method)(VALID_SESSION_ID, **kwargs)
            nack = await getattr(client, method)(VALID_SESSION_ID, raise_on_nack=False, **kwargs)
            assert not nack.ok

            setattr(
                stub,
                rpc,
                AsyncMock(side_effect=AioRpcError(grpc.StatusCode.NOT_FOUND, "gone")),
            )
            with pytest.raises(MacpTransportError) as exc:
                await getattr(client, method)(VALID_SESSION_ID, **kwargs)
            assert exc.value.code == "NOT_FOUND"

        run(go)

    def test_cancel_session_request_fields(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.CancelSession = AsyncMock(
                return_value=core_pb2.CancelSessionResponse(ack=make_ack(True))
            )
            await client.cancel_session(VALID_SESSION_ID, reason="done", cancelled_by="bob")
            request = stub.CancelSession.call_args.args[0]
            assert request.session_id == VALID_SESSION_ID
            assert request.reason == "done"

        run(go)

    def test_get_session(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.GetSession = AsyncMock(return_value=core_pb2.GetSessionResponse())
            await client.get_session(VALID_SESSION_ID)
            assert stub.GetSession.call_args.args[0].session_id == VALID_SESSION_ID

        run(go)


class TestDiscoveryRpcs:
    @pytest.mark.parametrize(
        ("method", "rpc", "response", "args"),
        [
            ("initialize", "Initialize", core_pb2.InitializeResponse(), ()),
            ("get_manifest", "GetManifest", core_pb2.GetManifestResponse(), ("agent-a",)),
            ("list_modes", "ListModes", core_pb2.ListModesResponse(), ()),
            ("list_ext_modes", "ListExtModes", core_pb2.ListExtModesResponse(), ()),
            ("list_roots", "ListRoots", core_pb2.ListRootsResponse(), ()),
            ("get_policy", "GetPolicy", policy_pb2.GetPolicyResponse(), ("pol",)),
            ("list_policies", "ListPolicies", policy_pb2.ListPoliciesResponse(), ("m",)),
        ],
    )
    def test_unary_passthrough(self, method: str, rpc: str, response: Any, args: tuple) -> None:
        async def go() -> None:
            client, stub = await _client(default_timeout=7.0)
            setattr(stub, rpc, AsyncMock(return_value=response))
            got = await getattr(client, method)(*args)
            assert got is response
            assert getattr(stub, rpc).call_args.kwargs["timeout"] == 7.0

        run(go)

    def test_unbounded_timeout_overrides_default(self) -> None:
        async def go() -> None:
            client, stub = await _client(default_timeout=7.0)
            stub.ListModes = AsyncMock(return_value=core_pb2.ListModesResponse())
            await client.list_modes(timeout=UNBOUNDED)
            assert stub.ListModes.call_args.kwargs["timeout"] is None

        run(go)

    def test_initialize_request_carries_client_info(self) -> None:
        async def go() -> None:
            client, stub = await _client(client_name="demo")
            stub.Initialize = AsyncMock(return_value=core_pb2.InitializeResponse())
            await client.initialize()
            assert stub.Initialize.call_args.args[0].client_info.name == "demo"

        run(go)


class TestListSessions:
    def test_drains_every_page(self) -> None:
        async def go() -> list[core_pb2.SessionMetadata]:
            client, stub = await _client()
            stub.ListSessions = AsyncMock(
                side_effect=[
                    core_pb2.ListSessionsResponse(
                        sessions=[core_pb2.SessionMetadata(session_id="a")],
                        next_page_token="t2",
                    ),
                    core_pb2.ListSessionsResponse(
                        sessions=[core_pb2.SessionMetadata(session_id="b")]
                    ),
                ]
            )
            sessions = await client.list_sessions(page_size=1)
            assert stub.ListSessions.call_args_list[1].args[0].page_token == "t2"
            return sessions

        assert [s.session_id for s in run(go)] == ["a", "b"]

    def test_single_page(self) -> None:
        async def go() -> tuple[list[core_pb2.SessionMetadata], str]:
            client, stub = await _client()
            stub.ListSessions = AsyncMock(
                return_value=core_pb2.ListSessionsResponse(next_page_token="next")
            )
            return await client.list_sessions_page(page_token="cur")

        assert run(go) == ([], "next")


class TestRegistryMutations:
    @pytest.mark.parametrize(
        ("method", "rpc", "response", "arg"),
        [
            (
                "register_ext_mode",
                "RegisterExtMode",
                core_pb2.RegisterExtModeResponse(ok=True),
                core_pb2.ModeDescriptor(
                    mode="ext.demo.v1",
                    mode_version="1.0.0",
                    terminal_message_types=["Commitment"],
                ),
            ),
            (
                "unregister_ext_mode",
                "UnregisterExtMode",
                core_pb2.UnregisterExtModeResponse(ok=True),
                "ext.demo.v1",
            ),
            ("promote_mode", "PromoteMode", core_pb2.PromoteModeResponse(ok=True), "ext.demo.v1"),
            (
                "register_policy",
                "RegisterPolicy",
                policy_pb2.RegisterPolicyResponse(ok=True),
                policy_pb2.PolicyDescriptor(policy_id="p"),
            ),
            (
                "unregister_policy",
                "UnregisterPolicy",
                policy_pb2.UnregisterPolicyResponse(ok=True),
                "p",
            ),
        ],
    )
    def test_ok_read_only_and_transport(
        self, method: str, rpc: str, response: Any, arg: Any
    ) -> None:
        async def go() -> None:
            client, stub = await _client()
            setattr(stub, rpc, AsyncMock(return_value=response))
            assert await getattr(client, method)(arg) is response

            setattr(
                stub,
                rpc,
                AsyncMock(
                    side_effect=AioRpcError(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "",
                        trailing_metadata=[("macp-error-code", "REGISTRY_READ_ONLY")],
                    )
                ),
            )
            with pytest.raises(MacpAckError) as exc:
                await getattr(client, method)(arg)
            assert exc.value.failure.code == "FAILED_PRECONDITION"
            assert exc.value.macp_code == "REGISTRY_READ_ONLY"
            assert "read-only" in exc.value.failure.message

            setattr(stub, rpc, AsyncMock(side_effect=AioRpcError(grpc.StatusCode.INTERNAL, "x")))
            with pytest.raises(MacpTransportError):
                await getattr(client, method)(arg)

        run(go)

    def test_register_ext_mode_rejects_standard_mode(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            stub.RegisterExtMode = AsyncMock()
            with pytest.raises(MacpSdkError):
                await client.register_ext_mode(
                    core_pb2.ModeDescriptor(mode="macp.mode.decision.v1")
                )
            stub.RegisterExtMode.assert_not_called()

        run(go)


class TestSignalsAndProgress:
    def test_send_signal_builds_ambient_envelope(self) -> None:
        async def go() -> envelope_pb2.Envelope:
            client, stub = await _client()
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=make_ack(True)))
            await client.send_signal(signal_type="heartbeat", confidence=0.5)
            envelope: envelope_pb2.Envelope = stub.Send.call_args.args[0].envelope
            return envelope

        envelope = run(go)
        assert envelope.message_type == "Signal"
        assert envelope.session_id == ""
        assert envelope.sender == "alice"

    def test_send_signal_rejects_sender_mismatch(self) -> None:
        async def go() -> None:
            client, stub = await _client(auth=AuthConfig.for_dev_agent("alice"))
            stub.Send = AsyncMock()
            with pytest.raises(MacpIdentityMismatchError):
                await client.send_signal(signal_type="heartbeat", sender="mallory")

        run(go)

    def test_send_progress_session_scoped(self) -> None:
        async def go() -> envelope_pb2.Envelope:
            client, stub = await _client()
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=make_ack(True)))
            await client.send_progress(
                session_id=VALID_SESSION_ID,
                mode="macp.mode.task.v1",
                progress_token="tok",
                progress=1,
                total=2,
            )
            envelope: envelope_pb2.Envelope = stub.Send.call_args.args[0].envelope
            return envelope

        envelope = run(go)
        assert envelope.message_type == "Progress"
        assert envelope.session_id == VALID_SESSION_ID

    def test_send_progress_validates_scope(self) -> None:
        async def go() -> None:
            client, _ = await _client()
            with pytest.raises(MacpSdkError):
                await client.send_progress(
                    session_id=VALID_SESSION_ID, progress_token="t", progress=0, total=1
                )

        run(go)


# ── Server-streaming watch RPCs ───────────────────────────────────────


WATCHES = [
    ("watch_sessions", "WatchSessions", core_pb2.WatchSessionsResponse()),
    ("watch_policies", "WatchPolicies", policy_pb2.WatchPoliciesResponse()),
    ("watch_mode_registry", "WatchModeRegistry", core_pb2.WatchModeRegistryResponse()),
    ("watch_roots", "WatchRoots", core_pb2.WatchRootsResponse()),
    ("watch_signals", "WatchSignals", core_pb2.WatchSignalsResponse()),
]


class TestWatches:
    @pytest.mark.parametrize(("method", "rpc", "item"), WATCHES)
    def test_yields_items_and_cancels_call(self, method: str, rpc: str, item: Any) -> None:
        async def go() -> None:
            client, stub = await _client()
            call = FakeStreamCall([item, item])
            getattr(stub, rpc).return_value = call
            got = [x async for x in getattr(client, method)()]
            assert got == [item, item]
            assert call.cancelled

        run(go)

    @pytest.mark.parametrize(("method", "rpc", "item"), WATCHES)
    def test_rpc_error_maps_to_transport_error(self, method: str, rpc: str, item: Any) -> None:
        async def go() -> None:
            client, stub = await _client()
            call = FakeStreamCall(
                [item],
                error=AioRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "lagged"),
            )
            getattr(stub, rpc).return_value = call
            seen = []
            with pytest.raises(MacpTransportError) as exc:
                async for x in getattr(client, method)():
                    seen.append(x)
            assert seen == [item]
            assert exc.value.code == "RESOURCE_EXHAUSTED"
            assert call.cancelled

        run(go)

    def test_breaking_out_early_cancels_call(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            call = FakeStreamCall([core_pb2.WatchSessionsResponse()] * 3)
            stub.WatchSessions.return_value = call
            gen = client.watch_sessions()
            async for _ in gen:
                break
            await gen.aclose()
            assert call.cancelled

        run(go)

    def test_cancel_failure_is_swallowed(self) -> None:
        async def go() -> None:
            client, stub = await _client()
            call = FakeStreamCall([])
            call.cancel = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
            stub.WatchRoots.return_value = call
            assert [x async for x in client.watch_roots()] == []

        run(go)


# ── Bidirectional stream ──────────────────────────────────────────────


async def _open(**kwargs: Any) -> tuple[AsyncMacpStream, FakeBidiCall, MagicMock]:
    client, stub = await _client(**kwargs)
    call = FakeBidiCall()
    stub.StreamSession.return_value = call
    stream = client.open_stream(timeout=30.0)
    return stream, call, stub


class TestAsyncMacpStream:
    def test_open_passes_metadata_and_timeout(self) -> None:
        async def go() -> None:
            stream, call, stub = await _open()
            kwargs = stub.StreamSession.call_args.kwargs
            assert kwargs["timeout"] == 30.0
            assert ("authorization", "Bearer tok") in kwargs["metadata"]
            call.inbox.put_nowait(aio.EOF)
            async with stream:
                pass

        run(go)

    def test_send_and_subscribe_write_requests(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                await stream.send(_envelope())
                await stream.send_subscribe(VALID_SESSION_ID, after_sequence=4)
            assert call.written[0].envelope.message_id == "m1"
            assert call.written[1].subscribe_session_id == VALID_SESSION_ID
            assert call.written[1].after_sequence == 4

        run(go)

    def test_read_envelopes_then_end_of_stream(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                call.inbox.put_nowait(_stream_response(_envelope()))
                call.inbox.put_nowait(_stream_response(_envelope()))
                call.inbox.put_nowait(aio.EOF)
                got = [env async for env in stream.responses(timeout=1.0)]
                assert len(got) == 2
                # End of stream stays observable on repeated reads.
                assert await stream.read(timeout=1.0) is None
                assert await stream.read(timeout=1.0) is None

        run(go)

    def test_read_timeout_keeps_stream_open(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                with pytest.raises(MacpTimeoutError):
                    await stream.read(timeout=0.01)
                assert not call.cancelled
                call.inbox.put_nowait(_stream_response(_envelope()))
                env = await stream.read(timeout=1.0)
                assert env is not None

        run(go)

    def test_inline_error_invokes_callbacks_and_keeps_stream(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            errors: list[envelope_pb2.MACPError] = []
            stream.on_inline_error(errors.append)
            async with stream:
                call.inbox.put_nowait(
                    core_pb2.StreamSessionResponse(
                        error=envelope_pb2.MACPError(code="FORBIDDEN", message="no")
                    )
                )
                call.inbox.put_nowait(_stream_response(_envelope()))
                env = await stream.read(timeout=1.0)
                assert env is not None
            assert [e.code for e in errors] == ["FORBIDDEN"]

        run(go)

    def test_rpc_error_surfaces_as_transport_error(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                call.inbox.put_nowait(
                    AioRpcError(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "who",
                        trailing_metadata=[("macp-error-code", "UNAUTHENTICATED")],
                    )
                )
                with pytest.raises(MacpTransportError) as exc:
                    await stream.read(timeout=1.0)
                assert exc.value.code == "UNAUTHENTICATED"
                assert exc.value.macp_code == "UNAUTHENTICATED"
                assert await stream.read(timeout=1.0) is None

        run(go)

    def test_cancel_surfaces_cancelled_then_none(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            stream.cancel()
            assert call.cancelled
            with pytest.raises(MacpTransportError) as exc:
                await stream.read(timeout=1.0)
            assert exc.value.code == "CANCELLED"
            assert await stream.read(timeout=1.0) is None
            with pytest.raises(MacpSdkError):
                await stream.send(_envelope())
            async with stream:
                pass

        run(go)

    def test_close_half_closes_once_and_blocks_writes(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                await stream.close()
                await stream.close()
                assert call.half_closed
                with pytest.raises(MacpSdkError):
                    await stream.send_subscribe(VALID_SESSION_ID)

        run(go)

    def test_close_swallows_done_writing_failure(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            call.done_writing_error = asyncio.InvalidStateError("done")
            async with stream:
                await stream.close()

        run(go)

    def test_write_on_finished_call_maps_to_sdk_error(self) -> None:
        async def go() -> None:
            stream, call, _ = await _open()
            async with stream:
                call.write_error = asyncio.InvalidStateError("finished")
                with pytest.raises(MacpSdkError, match="no longer writable"):
                    await stream.send(_envelope())
                call.write_error = AioRpcError(grpc.StatusCode.UNAVAILABLE, "down")
                with pytest.raises(MacpTransportError) as exc:
                    await stream.send(_envelope())
                assert exc.value.code == "UNAVAILABLE"

        run(go)

    def test_pump_task_cancellation_propagates(self) -> None:
        async def go() -> None:
            stream, _call, _ = await _open()
            await asyncio.sleep(0)  # let the pump start awaiting read()
            stream._pump.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream._pump
            # The end sentinel is still published for readers.
            assert await stream.read(timeout=1.0) is None

        run(go)

    def test_open_stream_requires_auth(self) -> None:
        async def go() -> None:
            async with AsyncMacpClient(target="localhost:0", allow_insecure=True) as client:
                with pytest.raises(MacpSdkError):
                    client.open_stream()

        run(go)
