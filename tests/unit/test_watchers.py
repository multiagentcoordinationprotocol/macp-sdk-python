"""Unit coverage for the watcher wrappers (Q-5).

The watchers are thin adapters over ``MacpClient`` server-streaming RPCs.
These tests drive each watcher against a ``MagicMock`` client and verify:

- ``changes()`` / ``signals()`` yields everything the stream produced, in order,
- ``watch(handler)`` invokes the handler once per stream item,
- ``next_change()`` / ``next_signal()`` returns the first item,
- empty streams raise ``RuntimeError`` from ``next_*`` helpers,
- ``SignalWatcher`` ignores frames whose envelope is empty (``ByteSize == 0``),
- ``PolicyWatcher`` maps responses into the typed ``PolicyChange`` dataclass.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from macp_sdk.errors import MacpTransportError
from macp_sdk.watchers import (
    ModeRegistryWatcher,
    PolicyChange,
    PolicyWatcher,
    RootsWatcher,
    SessionLifecycleWatcher,
    SignalWatcher,
)
from tests.conftest import FakeRpcError
from tests.conftest import client_with_stub as _client_with_stub


def _client_with_stream(method_name: str, items: list[object]) -> MagicMock:
    # A generator expression (unlike iter(list)) has a real .close() method,
    # matching what the production watch_* methods actually return -- now
    # required since the watchers wrap this return value in contextlib.closing().
    client = MagicMock()
    getattr(client, method_name).return_value = (item for item in items)
    return client


# ── ModeRegistryWatcher ───────────────────────────────────────────────


class TestModeRegistryWatcher:
    def test_changes_yields_every_item(self):
        a, b = MagicMock(), MagicMock()
        client = _client_with_stream("watch_mode_registry", [a, b])
        watcher = ModeRegistryWatcher(client)
        assert list(watcher.changes()) == [a, b]

    def test_watch_invokes_handler_per_item(self):
        a, b, c = MagicMock(), MagicMock(), MagicMock()
        client = _client_with_stream("watch_mode_registry", [a, b, c])
        watcher = ModeRegistryWatcher(client)
        seen: list[object] = []
        watcher.watch(seen.append)
        assert seen == [a, b, c]

    def test_next_change_returns_first(self):
        first = MagicMock()
        client = _client_with_stream("watch_mode_registry", [first, MagicMock()])
        watcher = ModeRegistryWatcher(client)
        assert watcher.next_change() is first

    def test_next_change_empty_raises(self):
        client = _client_with_stream("watch_mode_registry", [])
        watcher = ModeRegistryWatcher(client)
        with pytest.raises(RuntimeError, match="stream ended"):
            watcher.next_change()


# ── RootsWatcher ──────────────────────────────────────────────────────


class TestRootsWatcher:
    def test_changes_yields_and_watch_dispatches(self):
        a, b = MagicMock(), MagicMock()
        client = _client_with_stream("watch_roots", [a, b])
        watcher = RootsWatcher(client)
        assert list(watcher.changes()) == [a, b]

    def test_next_change_empty_raises(self):
        client = _client_with_stream("watch_roots", [])
        watcher = RootsWatcher(client)
        with pytest.raises(RuntimeError, match="stream ended"):
            watcher.next_change()


# ── SignalWatcher ─────────────────────────────────────────────────────


def _fake_signal_response(envelope_bytesize: int) -> MagicMock:
    resp = MagicMock()
    resp.envelope.ByteSize.return_value = envelope_bytesize
    return resp


class TestSignalWatcher:
    def test_signals_yields_only_populated_envelopes(self):
        empty = _fake_signal_response(0)
        full1 = _fake_signal_response(10)
        full2 = _fake_signal_response(20)
        client = _client_with_stream("watch_signals", [empty, full1, empty, full2])
        watcher = SignalWatcher(client)
        emitted = [env for env in watcher.signals()]
        # empty frames are dropped; payload frames surface via ``.envelope``
        assert emitted == [full1.envelope, full2.envelope]

    def test_next_signal_skips_empties(self):
        empty = _fake_signal_response(0)
        full = _fake_signal_response(5)
        client = _client_with_stream("watch_signals", [empty, full])
        watcher = SignalWatcher(client)
        assert watcher.next_signal() is full.envelope

    def test_next_signal_all_empty_raises(self):
        client = _client_with_stream(
            "watch_signals", [_fake_signal_response(0), _fake_signal_response(0)]
        )
        watcher = SignalWatcher(client)
        with pytest.raises(RuntimeError, match="stream ended"):
            watcher.next_signal()

    def test_watch_invokes_handler(self):
        full = _fake_signal_response(10)
        client = _client_with_stream("watch_signals", [full])
        watcher = SignalWatcher(client)
        seen: list[object] = []
        watcher.watch(seen.append)
        assert seen == [full.envelope]


# ── PolicyWatcher ─────────────────────────────────────────────────────


def _fake_policy_response(descriptors: list[object], observed_at_unix_ms: int = 0) -> MagicMock:
    resp = MagicMock()
    resp.descriptors = descriptors
    resp.observed_at_unix_ms = observed_at_unix_ms
    return resp


class TestPolicyWatcher:
    def test_changes_maps_to_policy_change(self):
        d1 = MagicMock()
        d2 = MagicMock()
        r1 = _fake_policy_response([d1], observed_at_unix_ms=111)
        r2 = _fake_policy_response([d2, d2], observed_at_unix_ms=222)
        client = _client_with_stream("watch_policies", [r1, r2])
        watcher = PolicyWatcher(client)
        out = list(watcher.changes())
        assert out == [
            PolicyChange(descriptors=[d1], observed_at_unix_ms=111),
            PolicyChange(descriptors=[d2, d2], observed_at_unix_ms=222),
        ]

    def test_next_change_returns_first(self):
        r1 = _fake_policy_response([MagicMock()], observed_at_unix_ms=1)
        client = _client_with_stream("watch_policies", [r1, _fake_policy_response([])])
        watcher = PolicyWatcher(client)
        first = watcher.next_change()
        assert isinstance(first, PolicyChange)
        assert first.observed_at_unix_ms == 1

    def test_next_change_empty_raises(self):
        client = _client_with_stream("watch_policies", [])
        watcher = PolicyWatcher(client)
        with pytest.raises(RuntimeError, match="stream ended"):
            watcher.next_change()

    def test_missing_descriptors_attribute_yields_empty_list(self):
        resp = MagicMock(spec=[])  # no descriptors attribute at all
        client = _client_with_stream("watch_policies", [resp])
        watcher = PolicyWatcher(client)
        (only,) = list(watcher.changes())
        assert only.descriptors == []
        assert only.observed_at_unix_ms == 0


class TestSessionLifecycleWatcherExport:
    def test_exported_from_package_root(self):
        import macp_sdk

        assert macp_sdk.SessionLifecycleWatcher is SessionLifecycleWatcher


# ── Phase 10: stream cancellation on abandonment ─────────────────────
#
# These drive the raw MacpClient.watch_*() generators (not just the
# watcher wrappers) against a FakeCall standing in for the gRPC
# streaming call object, since that is where _cancel_quietly actually
# lives (client.py's finally, not watchers.py's closing()).


class FakeCall:
    """A fake grpc server-streaming call: an iterator yielding *items*,
    with a cancel() counter. An item that is itself an exception
    instance is raised instead of yielded, standing in for a mid-stream
    RpcError."""

    def __init__(self, items: list[object], *, cancel_raises: bool = False) -> None:
        self._items = list(items)
        self._index = 0
        self.cancel_calls = 0
        self.cancel_raises = cancel_raises

    def __iter__(self) -> FakeCall:
        return self

    def __next__(self) -> object:
        if self._index >= len(self._items):
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_raises:
            raise RuntimeError("cancel() boom")


class TestStreamCancellation:
    """Criteria 1-5, driven through PolicyWatcher / client.watch_policies()
    -- the behavior is identical across all five RPCs (criterion 6, in
    TestStreamCancellationAllFiveRpcs below) since client.py's five
    watch_* bodies are byte-identical."""

    def test_next_change_returns_first_and_cancels_once(self):
        """Criterion 1."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([MagicMock()], observed_at_unix_ms=1),
                _fake_policy_response([MagicMock()], observed_at_unix_ms=2),
                _fake_policy_response([MagicMock()], observed_at_unix_ms=3),
            ]
        )
        stub.WatchPolicies.return_value = call
        watcher = PolicyWatcher(client)

        first = watcher.next_change()

        assert first.observed_at_unix_ms == 1
        assert call.cancel_calls == 1

    def test_abandoning_raw_generator_after_one_item_cancels_once(self):
        """Criterion 2: calling next() once on the raw
        client.watch_policies() generator, then closing it, cancels the
        underlying call exactly once."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([], observed_at_unix_ms=1),
                _fake_policy_response([], observed_at_unix_ms=2),
            ]
        )
        stub.WatchPolicies.return_value = call

        gen = client.watch_policies()
        next(gen)
        gen.close()

        assert call.cancel_calls == 1

    def test_watch_handler_raising_propagates_and_cancels_once(self):
        """Criterion 3."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([], observed_at_unix_ms=1),
                _fake_policy_response([], observed_at_unix_ms=2),
            ]
        )
        stub.WatchPolicies.return_value = call
        watcher = PolicyWatcher(client)

        def handler(change: object) -> None:
            raise ValueError("handler boom")

        with pytest.raises(ValueError, match="handler boom"):
            watcher.watch(handler)

        assert call.cancel_calls == 1

    def test_normal_exhaustion_cancels_once_and_raises_nothing(self):
        """Criterion 4."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([], observed_at_unix_ms=1),
                _fake_policy_response([], observed_at_unix_ms=2),
            ]
        )
        stub.WatchPolicies.return_value = call

        results = list(client.watch_policies())

        assert len(results) == 2
        assert call.cancel_calls == 1

    def test_cancel_raising_does_not_mask_mid_stream_rpc_error(self):
        """Criterion 5 (part 1): a cancel() that raises does not replace
        the MacpTransportError a mid-stream grpc.RpcError produced."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([], observed_at_unix_ms=1),
                FakeRpcError(grpc.StatusCode.UNAVAILABLE, "boom"),
            ],
            cancel_raises=True,
        )
        stub.WatchPolicies.return_value = call

        with pytest.raises(MacpTransportError):
            list(client.watch_policies())

        assert call.cancel_calls == 1

    def test_cancel_raising_does_not_mask_normal_exhaustion(self):
        """Criterion 5 (part 2): a cancel() that raises does not turn a
        clean exhaustion into an error."""
        client, stub = _client_with_stub()
        call = FakeCall(
            [
                _fake_policy_response([], observed_at_unix_ms=1),
                _fake_policy_response([], observed_at_unix_ms=2),
            ],
            cancel_raises=True,
        )
        stub.WatchPolicies.return_value = call

        results = list(client.watch_policies())

        assert len(results) == 2
        assert call.cancel_calls == 1


def _lifecycle_response(session_id: str = "s") -> MagicMock:
    from macp.v1 import core_pb2

    resp = MagicMock()
    resp.event.event_type = core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED
    resp.event.observed_at_unix_ms = 1
    resp.event.session.session_id = session_id
    return resp


# Maps each stub method name to the watcher class that wraps it, plus a
# factory for a response shaped correctly for that watcher's own mapping
# logic (SessionLifecycleWatcher needs response.event.*, SignalWatcher
# needs a non-empty response.envelope, PolicyWatcher needs
# response.descriptors/.observed_at_unix_ms; ModeRegistryWatcher/
# RootsWatcher pass responses through unchanged).
_WATCHER_FOR_STUB = {
    "WatchSessions": (SessionLifecycleWatcher, _lifecycle_response),
    "WatchPolicies": (PolicyWatcher, lambda: _fake_policy_response([])),
    "WatchModeRegistry": (ModeRegistryWatcher, MagicMock),
    "WatchRoots": (RootsWatcher, MagicMock),
    "WatchSignals": (SignalWatcher, lambda: _fake_signal_response(10)),
}

# SignalWatcher's pull-one method is next_signal(), not next_change() --
# the one genuinely different name among the five.
_NEXT_METHOD_FOR_STUB = {
    "WatchSessions": "next_change",
    "WatchPolicies": "next_change",
    "WatchModeRegistry": "next_change",
    "WatchRoots": "next_change",
    "WatchSignals": "next_signal",
}

# SignalWatcher's own iterator method is signals(), not changes().
_OUTER_METHOD_FOR_STUB = {
    "WatchSessions": "changes",
    "WatchPolicies": "changes",
    "WatchModeRegistry": "changes",
    "WatchRoots": "changes",
    "WatchSignals": "signals",
}


@pytest.mark.parametrize(
    "method_name,stub_name",
    [
        ("watch_sessions", "WatchSessions"),
        ("watch_policies", "WatchPolicies"),
        ("watch_mode_registry", "WatchModeRegistry"),
        ("watch_roots", "WatchRoots"),
        ("watch_signals", "WatchSignals"),
    ],
)
class TestStreamCancellationAllFiveRpcs:
    """Criterion 6: the same behaviors hold for all five watch_* RPCs --
    this is what stops a sixth streaming RPC from being added without
    the finally. Three methods (test_abandon_after_one_item_cancels_once,
    test_normal_exhaustion_cancels_once, test_cancel_raising_does_not_mask_rpc_error)
    drive the raw client.<method_name>() generator directly (method- and
    response-shape agnostic: the generator body is just `yield from
    call`); the other three (test_watch_handler_raising_cancels_once,
    test_watch_normal_exhaustion_cancels_once,
    test_next_returns_first_and_cancels_once) go through each RPC's
    corresponding watcher helper instead, since those specifically
    exercise the watcher-level closing() wrapper and cannot be proven
    deterministic on the raw generator alone -- see each method's own
    docstring. A seventh method
    (test_closing_outer_stream_deterministically_closes_inner_generator)
    is a permanent regression guard for a gap a ship-gate review found:
    three of the five watchers used to close their *inner*
    client.watch_*() generator only by refcounting rather than
    deterministically -- see that method's own docstring."""

    def test_abandon_after_one_item_cancels_once(self, method_name, stub_name):
        client, stub = _client_with_stub()
        call = FakeCall([MagicMock(), MagicMock(), MagicMock()])
        getattr(stub, stub_name).return_value = call

        gen = getattr(client, method_name)()
        next(gen)
        gen.close()

        assert call.cancel_calls == 1

    def test_watch_handler_raising_cancels_once(self, method_name, stub_name):
        """Unlike the raw-generator tests in this class, a handler raising
        inside watch() is only deterministically closed via closing() --
        there is no explicit gen.close() call site to drive, and relying
        on GC timing here would be flaky. So this one goes through each
        RPC's corresponding watcher helper, per the plan's own instruction
        for criterion 6, with a response shaped correctly for that
        watcher's mapping logic (_WATCHER_FOR_STUB)."""
        client, stub = _client_with_stub()
        watcher_cls, make_response = _WATCHER_FOR_STUB[stub_name]
        call = FakeCall([make_response(), make_response()])
        getattr(stub, stub_name).return_value = call
        watcher = watcher_cls(client)

        def handler(item: object) -> None:
            raise ValueError("handler boom")

        with pytest.raises(ValueError, match="handler boom"):
            watcher.watch(handler)

        assert call.cancel_calls == 1

    def test_watch_normal_exhaustion_cancels_once(self, method_name, stub_name):
        """Covers watch()'s closing()-wrapped loop completing normally
        (as opposed to being interrupted by a raising handler, above) --
        the other branch of the same wrapper, for all five RPCs."""
        client, stub = _client_with_stub()
        watcher_cls, make_response = _WATCHER_FOR_STUB[stub_name]
        call = FakeCall([make_response(), make_response()])
        getattr(stub, stub_name).return_value = call
        watcher = watcher_cls(client)

        seen: list[object] = []
        watcher.watch(seen.append)

        assert len(seen) == 2
        assert call.cancel_calls == 1

    def test_next_returns_first_and_cancels_once(self, method_name, stub_name):
        """next_change()/next_signal()'s own closing()-wrapped happy
        path, for all five RPCs -- criterion 1, swept."""
        client, stub = _client_with_stub()
        watcher_cls, make_response = _WATCHER_FOR_STUB[stub_name]
        call = FakeCall([make_response(), make_response()])
        getattr(stub, stub_name).return_value = call
        watcher = watcher_cls(client)
        next_method = getattr(watcher, _NEXT_METHOD_FOR_STUB[stub_name])

        next_method()

        assert call.cancel_calls == 1

    def test_normal_exhaustion_cancels_once(self, method_name, stub_name):
        client, stub = _client_with_stub()
        call = FakeCall([MagicMock(), MagicMock()])
        getattr(stub, stub_name).return_value = call

        results = list(getattr(client, method_name)())

        assert len(results) == 2
        assert call.cancel_calls == 1

    def test_cancel_raising_does_not_mask_rpc_error(self, method_name, stub_name):
        client, stub = _client_with_stub()
        call = FakeCall(
            [MagicMock(), FakeRpcError(grpc.StatusCode.UNAVAILABLE, "boom")],
            cancel_raises=True,
        )
        getattr(stub, stub_name).return_value = call

        with pytest.raises(MacpTransportError):
            list(getattr(client, method_name)())

        assert call.cancel_calls == 1

    def test_closing_outer_stream_deterministically_closes_inner_generator(
        self, method_name, stub_name
    ):
        """Regression guard for a gap a ship-gate review found in Phase 10:
        SignalWatcher.signals() / SessionLifecycleWatcher.changes() /
        PolicyWatcher.changes() used to consume their inner
        client.watch_*() generator via a bare ``for`` loop, so closing the
        *outer* watcher-level generator only released the inner one by
        refcounting -- a reference held elsewhere (or a non-refcounting
        runtime) could defer that indefinitely. ModeRegistryWatcher and
        RootsWatcher were never affected: they delegate via ``yield from``,
        which PEP 380 guarantees closes the inner generator explicitly when
        the outer one is closed.

        This test pins an extra reference to the inner generator (via a
        spy on the client method) before closing the outer one, defeating
        CPython's immediate-refcount-drop shortcut -- so it only passes
        when the inner generator is closed through an explicit mechanism
        (``closing()`` or ``yield from``), not by luck of refcounting.
        """
        client, stub = _client_with_stub()
        watcher_cls, make_response = _WATCHER_FOR_STUB[stub_name]
        call = FakeCall([make_response(), make_response(), make_response()])
        getattr(stub, stub_name).return_value = call

        captured: list[object] = []
        original = getattr(client, method_name)

        def spy(*args: object, **kwargs: object) -> object:
            gen = original(*args, **kwargs)
            captured.append(gen)  # extra reference, held for the rest of this test
            return gen

        setattr(client, method_name, spy)

        watcher = watcher_cls(client)
        outer = getattr(watcher, _OUTER_METHOD_FOR_STUB[stub_name])()
        next(outer)  # advance past the first yield so the inner generator exists
        assert captured, "watcher never called the client-level watch_* method"
        # Pin the assertion below to outer.close() specifically -- without this,
        # a fixture change that made the stream exhaust on its own (rather than
        # still being live when closed) would pass vacuously even with the bug
        # restored, since normal exhaustion also runs the inner generator's
        # finally.
        assert call.cancel_calls == 0, "stream should still be live before close()"
        outer.close()

        assert call.cancel_calls == 1
