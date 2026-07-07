"""Integration tests for RFC-MACP-0006-A1 session subscription + replay.

A non-initiator agent that opens its ``StreamSession`` *after* the
initiator has already sent ``SessionStart`` and ``Proposal`` must still
see both envelopes, because the runtime replays accepted history
following a subscribe frame and then switches to live broadcast.

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import os
import queue
import time

import pytest

from macp_sdk import AuthConfig, DecisionSession, MacpClient, new_session_id

RUNTIME_TARGET = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")

pytestmark = pytest.mark.integration


def _client(agent: str) -> MacpClient:
    return MacpClient(
        target=RUNTIME_TARGET,
        allow_insecure=True,
        auth=AuthConfig.for_dev_agent(agent),
    )


class TestSessionSubscribeReplay:
    def test_late_joiner_receives_session_start_and_proposal(self) -> None:
        """Observer connects after SessionStart + Proposal — must still
        see both via the runtime's replay-then-live broadcast path."""
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = DecisionSession(client=initiator, session_id=session_id)
            ack = session.start(
                intent="subscribe-replay smoke",
                participants=["coordinator", "observer"],
                ttl_ms=30_000,
            )
            assert ack.ok
            ack = session.propose("p1", "option-a", rationale="replay me")
            assert ack.ok

            # Give the runtime a beat to finish broadcasting; if replay is
            # working, this delay doesn't matter — the observer will still
            # get both envelopes after subscribe.
            time.sleep(0.2)

            # Late joiner: observer opens its stream only now.
            observer = _client("observer")
            try:
                stream = observer.open_stream()
                try:
                    stream.send_subscribe(session_id)

                    seen_types: list[str] = []
                    deadline = time.time() + 5.0
                    while time.time() < deadline and not {
                        "SessionStart",
                        "Proposal",
                    }.issubset(seen_types):
                        env = stream.read(timeout=1.0)
                        if env is None:
                            break
                        if env.session_id != session_id:
                            continue
                        seen_types.append(env.message_type)

                    assert "SessionStart" in seen_types, (
                        f"late joiner did not receive SessionStart via replay; "
                        f"saw only {seen_types!r}"
                    )
                    assert "Proposal" in seen_types, (
                        f"late joiner did not receive Proposal via replay; saw only {seen_types!r}"
                    )
                finally:
                    stream.close()
            finally:
                observer.close()

            # Clean up the session so the runtime doesn't hold TTL state.
            session.cancel(reason="test cleanup")
        finally:
            initiator.close()

    def test_after_sequence_skips_history_replay(self) -> None:
        """``after_sequence`` at the current end-of-history suppresses replay:
        a reconnecting observer sees only new envelopes, not the existing
        SessionStart + prior proposals.

        Under the runtime v0.5.0 exclusive contract, ``after_sequence`` is the
        1-based accepted-envelope ordinal and only envelopes *above* it are
        delivered. Here SessionStart (#1) + p1 (#2) are the history, so
        resuming at ``after_sequence=2`` replays nothing and delivers only the
        live p2 (#3). (A number *past* the end would also suppress the live p2,
        since its ordinal would be ≤ the cursor — that is the corrected
        behaviour, and why this test resumes at the exact end rather than at a
        huge value as the pre-0.5.0 inclusive semantics allowed.)"""
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            session = DecisionSession(client=initiator, session_id=session_id)
            session.start(
                intent="after_sequence smoke",
                participants=["coordinator", "observer"],
                ttl_ms=30_000,
            )  # accepted envelope #1
            session.propose("p1", "option-a", rationale="historical")  # #2
            time.sleep(0.2)

            observer = _client("observer")
            try:
                stream = observer.open_stream()
                try:
                    # Resume at the exact end of history (2 accepted envelopes):
                    # replay yields nothing; only the live p2 (#3) is delivered.
                    stream.send_subscribe(session_id, after_sequence=2)

                    # Publisher emits one new envelope *after* subscribe.
                    session.propose("p2", "option-b", rationale="new")

                    seen_types: list[str] = []
                    deadline = time.time() + 5.0
                    while time.time() < deadline:
                        # read(timeout) raises queue.Empty on a quiet stream;
                        # treat that as "nothing yet" and keep polling.
                        try:
                            env = stream.read(timeout=0.5)
                        except queue.Empty:
                            continue
                        if env is None:
                            break
                        if env.session_id != session_id:
                            continue
                        seen_types.append(env.message_type)
                        if "Proposal" in seen_types:
                            # Brief tail window to catch any erroneous
                            # replay of SessionStart / p1.
                            tail = time.time() + 0.3
                            while time.time() < tail:
                                try:
                                    env = stream.read(timeout=0.2)
                                except queue.Empty:
                                    continue
                                if env is None or env.session_id != session_id:
                                    continue
                                seen_types.append(env.message_type)
                            break

                    assert "Proposal" in seen_types, (
                        f"observer should see the live Proposal; got {seen_types!r}"
                    )
                    assert "SessionStart" not in seen_types, (
                        f"after_sequence past end-of-history must skip "
                        f"SessionStart replay; got {seen_types!r}"
                    )
                finally:
                    stream.close()
            finally:
                observer.close()

            session.cancel(reason="test cleanup")
        finally:
            initiator.close()
