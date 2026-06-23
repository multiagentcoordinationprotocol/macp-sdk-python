"""Integration tests for macp-proto 0.1.3 suspend / resume / cancel.

Exercises ``MacpClient.suspend_session`` / ``resume_session`` and the new
``CANCELLED`` lifecycle semantics end-to-end against a live runtime.

Requires a running MACP runtime (>= 0.4.0) on localhost:50051 started with
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
from macp_sdk.errors import MacpAckError

RUNTIME_TARGET = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")

pytestmark = pytest.mark.integration


def _client(agent: str) -> MacpClient:
    return MacpClient(
        target=RUNTIME_TARGET,
        allow_insecure=True,
        auth=AuthConfig.for_dev_agent(agent),
    )


def _start_session(initiator: MacpClient, session_id: str) -> DecisionSession:
    session = DecisionSession(client=initiator, session_id=session_id)
    ack = session.start(
        intent="suspend/cancel smoke",
        participants=["coordinator", "alice"],
        ttl_ms=30_000,
    )
    assert ack.ok
    return session


class TestSuspendResume:
    def test_suspend_then_resume_round_trip(self) -> None:
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = _start_session(initiator, session_id)

            # Exercise the session-level helpers (delegate to the client RPCs).
            assert session.suspend(reason="maintenance").ok
            assert session.resume(reason="back online").ok

            session.cancel(reason="test cleanup")
        finally:
            initiator.close()

    def test_message_to_suspended_session_is_rejected(self) -> None:
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = _start_session(initiator, session_id)

            assert initiator.suspend_session(session_id, reason="freeze").ok

            # A suspended session is not OPEN, so the runtime rejects sends.
            with pytest.raises(MacpAckError):
                session.propose(
                    proposal_id="p-1",
                    option="ship it",
                    rationale="suspended session should reject this",
                )

            # Resuming restores OPEN so sends are accepted again.
            assert initiator.resume_session(session_id, reason="thaw").ok
            ack = session.propose(
                proposal_id="p-2",
                option="ship it",
                rationale="now that we are open again",
            )
            assert ack.ok

            initiator.cancel_session(session_id, reason="test cleanup")
        finally:
            initiator.close()


class TestCancelEmitsCancelled:
    def test_cancel_surfaces_cancelled_event(self) -> None:
        """An accepted CancelSession now terminates as CANCELLED (was EXPIRED)."""
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
                    ready.set()
                    if ev.is_terminal:
                        break
                if time.time() > deadline:
                    break

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        time.sleep(0.2)

        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = _start_session(initiator, session_id)
            assert ready.wait(timeout=5.0), "observer did not see any lifecycle event"
            session.cancel(reason="cancel emits CANCELLED")
        finally:
            initiator.close()

        consumer.join(timeout=10.0)
        observer.close()

        event_types = [ev.event_type for ev in seen]
        assert "CANCELLED" in event_types, f"cancel should surface CANCELLED; saw {event_types!r}"
        terminal = [ev for ev in seen if ev.is_terminal]
        assert terminal and terminal[-1].is_cancelled
