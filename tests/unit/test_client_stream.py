"""Tests for ``MacpStream`` — the bidirectional session stream helper.

Covers the RFC-MACP-0006-A1 subscribe frame (``send_subscribe``) added
alongside envelope sends, plus the request-iterator multiplex and
closed-stream guards.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk.client import MacpStream
from macp_sdk.errors import MacpSdkError, MacpTimeoutError


def _make_stub() -> tuple[MagicMock, threading.Event, list[core_pb2.StreamSessionRequest]]:
    """Build a stub whose ``StreamSession`` drains and records outgoing requests.

    Returns ``(stub, drained, captured)`` — ``drained`` fires once the
    request iterator reaches its ``_END`` sentinel, so tests can close
    the stream and deterministically wait for the drain.
    """
    drained = threading.Event()
    captured: list[core_pb2.StreamSessionRequest] = []

    def stream_session(request_iter, metadata=None, timeout=None):
        def _drain() -> None:
            try:
                for req in request_iter:
                    captured.append(req)
            finally:
                drained.set()

        threading.Thread(target=_drain, daemon=True).start()

        # Return an empty response iterator so the response pump exits
        # cleanly; tests here only care about the request side.
        return iter([])

    stub = MagicMock()
    stub.StreamSession.side_effect = stream_session
    return stub, drained, captured


class TestSendSubscribe:
    def test_subscribe_frame_has_session_id_and_sequence(self):
        stub, drained, captured = _make_stub()
        stream = MacpStream(stub, metadata=[])
        try:
            stream.send_subscribe("sess-42", after_sequence=7)
        finally:
            stream.close()
        assert drained.wait(timeout=2.0), "request iterator should drain after close"

        assert len(captured) == 1
        req = captured[0]
        assert isinstance(req, core_pb2.StreamSessionRequest)
        assert req.subscribe_session_id == "sess-42"
        assert req.after_sequence == 7
        # envelope must not be set when subscribing
        assert req.envelope.ByteSize() == 0

    def test_subscribe_default_sequence_is_zero(self):
        stub, drained, captured = _make_stub()
        stream = MacpStream(stub, metadata=[])
        try:
            stream.send_subscribe("sess-0")
        finally:
            stream.close()
        assert drained.wait(timeout=2.0)

        assert captured[0].subscribe_session_id == "sess-0"
        assert captured[0].after_sequence == 0

    def test_subscribe_after_close_raises(self):
        stub, _, _ = _make_stub()
        stream = MacpStream(stub, metadata=[])
        stream.close()
        with pytest.raises(MacpSdkError, match="stream is already closed"):
            stream.send_subscribe("sess-1")


class TestRequestIterMultiplex:
    """``_request_iter`` must forward subscribe frames as-is and wrap envelopes."""

    def test_envelope_gets_wrapped_in_request(self):
        stub, drained, captured = _make_stub()
        stream = MacpStream(stub, metadata=[])
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            message_type="Vote",
            session_id="sess-1",
            sender="alice",
        )
        try:
            stream.send(env)
        finally:
            stream.close()
        assert drained.wait(timeout=2.0)

        assert len(captured) == 1
        req = captured[0]
        assert req.envelope.session_id == "sess-1"
        assert req.subscribe_session_id == ""
        assert req.after_sequence == 0

    def test_subscribe_and_envelope_can_be_interleaved(self):
        """Subscribe first, then send — both frames must reach the server
        in order so non-initiators can observe replay before publishing."""
        stub, drained, captured = _make_stub()
        stream = MacpStream(stub, metadata=[])
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            message_type="Vote",
            session_id="sess-mix",
            sender="bob",
        )
        try:
            stream.send_subscribe("sess-mix", after_sequence=3)
            stream.send(env)
        finally:
            stream.close()
        assert drained.wait(timeout=2.0)

        assert len(captured) == 2
        assert captured[0].subscribe_session_id == "sess-mix"
        assert captured[0].after_sequence == 3
        assert captured[1].envelope.message_type == "Vote"


class TestSubscribeFrameProto:
    """Regression guard: the subscribe frame must serialise with the fields
    on the wire the runtime expects (RFC-MACP-0006-A1)."""

    def test_subscribe_frame_roundtrips_through_proto(self):
        req = core_pb2.StreamSessionRequest(
            subscribe_session_id="sess-roundtrip",
            after_sequence=42,
        )
        buf = req.SerializeToString()
        parsed = core_pb2.StreamSessionRequest()
        parsed.ParseFromString(buf)
        assert parsed.subscribe_session_id == "sess-roundtrip"
        assert parsed.after_sequence == 42
        assert parsed.envelope.ByteSize() == 0


class TestSendAfterCloseStillGuarded:
    """Regression guard: the new subscribe path must not regress the
    existing ``send`` closed-stream contract."""

    def test_send_after_close_raises(self):
        stub, _, _ = _make_stub()
        stream = MacpStream(stub, metadata=[])
        stream.close()
        env = envelope_pb2.Envelope(message_type="Vote", session_id="x")
        with pytest.raises(MacpSdkError, match="stream is already closed"):
            stream.send(env)


class TestResubscribe:
    """The runtime accepts multiple subscribe frames on the same stream
    (e.g. a reconnecting consumer first replays from 0, then re-subscribes
    from a higher sequence after applying snapshot)."""

    def test_two_subscribes_are_both_forwarded(self):
        stub, drained, captured = _make_stub()
        stream = MacpStream(stub, metadata=[])
        try:
            stream.send_subscribe("sess-re", after_sequence=0)
            stream.send_subscribe("sess-re", after_sequence=12)
        finally:
            stream.close()
        assert drained.wait(timeout=2.0)

        assert len(captured) == 2
        assert captured[0].after_sequence == 0
        assert captured[1].after_sequence == 12
        assert all(req.subscribe_session_id == "sess-re" for req in captured)


class TestReadAfterCloseReturnsNone:
    """The response pump must drain cleanly when the stream is closed
    immediately after a subscribe frame — no hang, no spurious envelope."""

    def test_read_drains_after_close(self):
        stub, drained, _ = _make_stub()
        stream = MacpStream(stub, metadata=[])
        stream.send_subscribe("sess-drain")
        stream.close()
        assert drained.wait(timeout=2.0)
        # With an empty upstream response iterator the pump posts _END and
        # ``read`` returns ``None`` without blocking.
        assert stream.read(timeout=2.0) is None

    def test_second_read_after_end_of_stream_also_returns_none(self):
        """Phase 3 item 1: the END sentinel is one-shot in the underlying
        queue, so a naive implementation consumes it on the first read and
        hangs on a second. It must be re-pushed so end-of-stream stays
        durably observable no matter how many times a caller reads past it.
        """
        stub, drained, _ = _make_stub()
        stream = MacpStream(stub, metadata=[])
        stream.close()
        assert drained.wait(timeout=2.0)
        assert stream.read(timeout=2.0) is None
        # A second (and third) read must not hang — each still sees None.
        assert stream.read(timeout=2.0) is None
        assert stream.read(timeout=2.0) is None

    def test_responses_started_after_close_also_terminates(self):
        """A responses() iterator started only after end-of-stream must
        still terminate immediately rather than hanging on a consumed
        sentinel."""
        stub, drained, _ = _make_stub()
        stream = MacpStream(stub, metadata=[])
        stream.close()
        assert drained.wait(timeout=2.0)
        assert stream.read(timeout=2.0) is None  # first read consumes-and-repushes
        assert list(stream.responses(timeout=2.0)) == []


class _NeverYields:
    """A response iterator that blocks forever without producing anything —
    keeps the response pump parked so ``_responses`` stays genuinely empty
    (an ``iter([])`` stub instead would race: the pump immediately posts
    ``_END``, and a 0.01s read could spuriously observe end-of-stream
    instead of timing out)."""

    def __iter__(self) -> _NeverYields:
        return self

    def __next__(self) -> object:
        threading.Event().wait()
        raise StopIteration  # pragma: no cover - unreachable


def _make_cancel_stub() -> tuple[MagicMock, threading.Event, MagicMock]:
    """Like _make_stub(), but the call object is a MagicMock supporting
    ``.cancel()`` — a plain ``iter([])`` (as _make_stub uses) has no such
    method, since these tests need to assert on it."""
    drained = threading.Event()
    call = MagicMock()
    call.__iter__.return_value = iter([])

    def stream_session(request_iter, metadata=None, timeout=None):
        def _drain() -> None:
            try:
                for _ in request_iter:
                    pass
            finally:
                drained.set()

        threading.Thread(target=_drain, daemon=True).start()
        return call

    stub = MagicMock()
    stub.StreamSession.side_effect = stream_session
    return stub, drained, call


class TestCancel:
    """cancel() (added for Phase 2's Participant.stop() fix) forcibly aborts
    the underlying gRPC call, unlike close()'s half-close-and-wait."""

    def test_cancel_calls_underlying_call_cancel(self):
        stub, drained, call = _make_cancel_stub()
        stream = MacpStream(stub, metadata=[])
        stream.cancel()
        assert drained.wait(timeout=2.0)
        call.cancel.assert_called_once()

    def test_cancel_is_idempotent(self):
        stub, drained, call = _make_cancel_stub()
        stream = MacpStream(stub, metadata=[])
        stream.cancel()
        stream.cancel()
        assert drained.wait(timeout=2.0)
        assert call.cancel.call_count == 2  # cancel() itself is a plain pass-through

    def test_cancel_after_close_still_cancels_call(self):
        stub, drained, call = _make_cancel_stub()
        stream = MacpStream(stub, metadata=[])
        stream.close()
        assert drained.wait(timeout=2.0)
        stream.cancel()
        call.cancel.assert_called_once()


class TestReadTimeout:
    """Phase 3 item 2: a timed read on an empty, still-open stream must
    raise a catchable SDK error, not a bare stdlib ``queue.Empty``."""

    def test_read_timeout_raises_macp_timeout_error(self):
        def stream_session(request_iter, metadata=None, timeout=None):
            threading.Thread(target=lambda: list(request_iter), daemon=True).start()
            return _NeverYields()

        stub = MagicMock()
        stub.StreamSession.side_effect = stream_session
        stream = MacpStream(stub, metadata=[])
        try:
            with pytest.raises(MacpTimeoutError):
                stream.read(timeout=0.01)
        finally:
            stream.close()
