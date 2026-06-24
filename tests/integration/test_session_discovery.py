"""Integration tests for SDK-PY-2 / SDK-PY-3: session discovery.

Exercises ``MacpClient.list_sessions`` and ``SessionLifecycleWatcher``
end-to-end against a live runtime.

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from macp_sdk import (
    AuthConfig,
    DecisionSession,
    MacpClient,
    SessionLifecycle,
    SessionLifecycleWatcher,
    new_session_id,
)

RUNTIME_TARGET = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")

pytestmark = pytest.mark.integration


def _client(agent: str) -> MacpClient:
    return MacpClient(
        target=RUNTIME_TARGET,
        allow_insecure=True,
        auth=AuthConfig.for_dev_agent(agent),
    )


class TestListSessions:
    def test_list_sessions_includes_just_started_session(self) -> None:
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = DecisionSession(client=initiator, session_id=session_id)
            ack = session.start(
                intent="list_sessions smoke",
                participants=["coordinator", "alice"],
                ttl_ms=30_000,
            )
            assert ack.ok

            observer = _client("coordinator")
            try:
                observer.initialize()
                sessions = observer.list_sessions()
                matching = [s for s in sessions if s.session_id == session_id]
                assert matching, (
                    f"list_sessions did not include just-started {session_id!r}; "
                    f"saw {[s.session_id for s in sessions]!r}"
                )
                meta = matching[0]
                assert meta.mode == "macp.mode.decision.v1"
                assert "coordinator" in list(meta.participants)
            finally:
                observer.close()

            session.cancel(reason="test cleanup")
        finally:
            initiator.close()


class TestWatchSessions:
    def test_created_and_expired_events_observed(self) -> None:
        """Start a session in one client, consume CREATED then EXPIRED
        lifecycle events from a SessionLifecycleWatcher in another."""
        session_id = new_session_id()
        observer = _client("coordinator")
        observer.initialize()
        watcher = SessionLifecycleWatcher(observer)

        seen: list[SessionLifecycle] = []
        ready = threading.Event()

        def consume() -> None:
            deadline = time.time() + 10.0
            for ev in watcher.changes():
                if ev.session and ev.session.session_id == session_id:
                    seen.append(ev)
                    if not ready.is_set():
                        ready.set()
                if ev.is_terminal and ev.session.session_id == session_id:
                    break
                if time.time() > deadline:
                    break

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        # Give the server time to register the watch before the initiator starts.
        time.sleep(0.2)

        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = DecisionSession(client=initiator, session_id=session_id)
            ack = session.start(
                intent="watch_sessions smoke",
                participants=["coordinator", "alice"],
                ttl_ms=30_000,
            )
            assert ack.ok

            assert ready.wait(timeout=5.0), "observer did not see any lifecycle event"

            session.cancel(reason="watch_sessions test cleanup")
        finally:
            initiator.close()

        consumer.join(timeout=10.0)
        observer.close()

        event_types = [ev.event_type for ev in seen]
        assert "CREATED" in event_types, f"no CREATED event; saw {event_types!r}"
        # Since macp-proto 0.1.3 an accepted CancelSession surfaces as CANCELLED
        # (previously EXPIRED); accept any terminal event here.
        assert {"CANCELLED", "EXPIRED", "RESOLVED"} & set(event_types), (
            f"no terminal event; saw {event_types!r}"
        )
