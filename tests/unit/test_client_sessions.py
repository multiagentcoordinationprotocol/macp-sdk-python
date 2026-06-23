"""Unit tests for SDK-PY-2 / SDK-PY-3 / SDK-PY-4.

Covers ``MacpClient.list_sessions`` + ``watch_sessions``, the
``SessionLifecycleWatcher`` wrapper, and the corrected ``Capabilities``
the client advertises during ``Initialize`` so the runtime does not see
a misleading handshake.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from macp.v1 import core_pb2

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient, _default_capabilities
from macp_sdk.errors import MacpSdkError, MacpTransportError
from macp_sdk.watchers import SessionLifecycle, SessionLifecycleWatcher


def _client_with_stub() -> tuple[MacpClient, MagicMock]:
    auth = AuthConfig.for_bearer("tok", sender_hint="alice")
    client = MacpClient(target="localhost:0", allow_insecure=True, auth=auth)
    stub = MagicMock()
    client.stub = stub
    return client, stub


def _lifecycle_response(event_type: int, session_id: str = "sess") -> MagicMock:
    resp = MagicMock()
    resp.event.event_type = event_type
    resp.event.observed_at_unix_ms = 42
    resp.event.session.session_id = session_id
    return resp


class TestListSessions:
    def test_returns_list_of_metadata(self):
        client, stub = _client_with_stub()
        s1 = core_pb2.SessionMetadata(session_id="a")
        s2 = core_pb2.SessionMetadata(session_id="b", context_id="ctx")
        stub.ListSessions.return_value = core_pb2.ListSessionsResponse(sessions=[s1, s2])

        out = client.list_sessions()

        stub.ListSessions.assert_called_once()
        assert isinstance(out, list)
        assert [s.session_id for s in out] == ["a", "b"]
        assert out[1].context_id == "ctx"

    def test_passes_auth_metadata(self):
        client, stub = _client_with_stub()
        stub.ListSessions.return_value = core_pb2.ListSessionsResponse(sessions=[])

        client.list_sessions()

        kwargs = stub.ListSessions.call_args.kwargs
        assert ("authorization", "Bearer tok") in list(kwargs["metadata"])

    def test_requires_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        with pytest.raises(MacpSdkError, match="requires auth"):
            client.list_sessions()


class TestWatchSessions:
    def test_yields_raw_responses(self):
        client, stub = _client_with_stub()
        r1 = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED, "s1")
        r2 = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_RESOLVED, "s1")
        stub.WatchSessions.return_value = iter([r1, r2])

        out = list(client.watch_sessions())

        stub.WatchSessions.assert_called_once()
        assert out == [r1, r2]

    def test_requires_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        with pytest.raises(MacpSdkError, match="requires auth"):
            list(client.watch_sessions())

    def test_grpc_error_wrapped_as_transport_error(self):
        import grpc

        class FakeRpcError(grpc.RpcError):
            def __str__(self) -> str:
                return "boom"

        client, stub = _client_with_stub()

        def _raise_iter():
            raise FakeRpcError()
            yield  # pragma: no cover

        stub.WatchSessions.return_value = _raise_iter()
        with pytest.raises(MacpTransportError):
            list(client.watch_sessions())


class TestSuspendResumeSession:
    def test_suspend_sends_request_and_returns_ack(self):
        client, stub = _client_with_stub()
        ack = core_pb2.SuspendSessionResponse().ack
        ack.ok = True
        stub.SuspendSession.return_value = core_pb2.SuspendSessionResponse(ack=ack)

        out = client.suspend_session("s1", reason="maintenance")

        req = stub.SuspendSession.call_args.args[0]
        assert isinstance(req, core_pb2.SuspendSessionRequest)
        assert req.session_id == "s1"
        assert req.reason == "maintenance"
        assert out.ok

    def test_resume_sends_request_and_returns_ack(self):
        client, stub = _client_with_stub()
        ack = core_pb2.ResumeSessionResponse().ack
        ack.ok = True
        stub.ResumeSession.return_value = core_pb2.ResumeSessionResponse(ack=ack)

        out = client.resume_session("s1", reason="back online")

        req = stub.ResumeSession.call_args.args[0]
        assert isinstance(req, core_pb2.ResumeSessionRequest)
        assert req.session_id == "s1"
        assert req.reason == "back online"
        assert out.ok

    def test_suspend_nack_raises(self):
        from macp_sdk.errors import MacpAckError

        client, stub = _client_with_stub()
        resp = core_pb2.SuspendSessionResponse()
        resp.ack.ok = False
        stub.SuspendSession.return_value = resp
        with pytest.raises(MacpAckError):
            client.suspend_session("s1", reason="x")

    def test_resume_grpc_error_wrapped(self):
        import grpc

        class FakeRpcError(grpc.RpcError):
            def details(self) -> str:
                return "boom"

        client, stub = _client_with_stub()
        stub.ResumeSession.side_effect = FakeRpcError()
        with pytest.raises(MacpTransportError):
            client.resume_session("s1")

    def test_suspend_requires_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        with pytest.raises(MacpSdkError, match="requires auth"):
            client.suspend_session("s1")


class TestSessionLifecycleWatcher:
    def test_maps_event_types_to_short_names(self):
        client = MagicMock()
        r1 = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED, "s1")
        r2 = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_RESOLVED, "s1")
        r3 = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_EXPIRED, "s2")
        client.watch_sessions.return_value = iter([r1, r2, r3])

        watcher = SessionLifecycleWatcher(client)
        out = list(watcher.changes())

        assert [ev.event_type for ev in out] == ["CREATED", "RESOLVED", "EXPIRED"]
        assert out[0].is_created and not out[0].is_terminal
        assert out[1].is_resolved and out[1].is_terminal
        assert out[2].is_expired and out[2].is_terminal
        assert all(ev.observed_at_unix_ms == 42 for ev in out)

    def test_maps_suspend_resume_cancel_event_types(self):
        """macp-proto 0.1.3 events normalise to short names and predicates."""
        client = MagicMock()
        rs = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_SUSPENDED, "s1")
        rr = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_RESUMED, "s1")
        rc = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CANCELLED, "s1")
        client.watch_sessions.return_value = iter([rs, rr, rc])

        suspended, resumed, cancelled = list(SessionLifecycleWatcher(client).changes())

        assert suspended.event_type == "SUSPENDED"
        assert suspended.is_suspended and not suspended.is_terminal
        assert resumed.event_type == "RESUMED"
        assert resumed.is_resumed and not resumed.is_terminal
        # CANCELLED is terminal and distinct from EXPIRED (the latent-bug fix).
        assert cancelled.event_type == "CANCELLED"
        assert cancelled.is_cancelled and cancelled.is_terminal
        assert not cancelled.is_expired

    def test_watch_invokes_handler_per_event(self):
        client = MagicMock()
        r = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED, "s1")
        client.watch_sessions.return_value = iter([r])
        watcher = SessionLifecycleWatcher(client)
        seen: list[SessionLifecycle] = []
        watcher.watch(seen.append)
        assert len(seen) == 1 and seen[0].is_created

    def test_skips_responses_without_event(self):
        client = MagicMock()
        bad = MagicMock(spec=[])  # no ``event`` attribute
        ok = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED, "s1")
        client.watch_sessions.return_value = iter([bad, ok])
        watcher = SessionLifecycleWatcher(client)
        out = list(watcher.changes())
        assert len(out) == 1 and out[0].is_created

    def test_next_change_returns_first(self):
        client = MagicMock()
        r = _lifecycle_response(core_pb2.SessionLifecycleEvent.EVENT_TYPE_CREATED, "s1")
        client.watch_sessions.return_value = iter([r])
        assert SessionLifecycleWatcher(client).next_change().is_created

    def test_next_change_empty_raises(self):
        client = MagicMock()
        client.watch_sessions.return_value = iter([])
        with pytest.raises(RuntimeError, match="stream ended"):
            SessionLifecycleWatcher(client).next_change()

    def test_auth_override_passed_to_client(self):
        client = MagicMock()
        client.watch_sessions.return_value = iter([])
        auth = AuthConfig.for_bearer("tok-override")
        list(SessionLifecycleWatcher(client, auth=auth).changes())
        client.watch_sessions.assert_called_once_with(auth=auth)


class TestDefaultCapabilities:
    """SDK-PY-4: the client must advertise every sessions capability it
    actually implements, so runtime diagnostics / policy routing are
    correct. ``stream`` was the only field set before 0.2.4."""

    def test_sessions_capability_advertises_list_and_watch(self):
        caps = _default_capabilities()
        assert caps.sessions.stream is True
        assert caps.sessions.list_sessions is True
        assert caps.sessions.watch_sessions is True
