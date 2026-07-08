"""Integration tests for the runtime v0.5.0 / macp-proto 0.1.6 surface.

Requires a running MACP runtime v0.5.0 on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``. Run with ``make test-integration`` or
``pytest tests/integration -m integration``.

The read-only-policy-registry test needs a *second* runtime configured with
``MACP_POLICIES_DIR``; it is skipped unless ``MACP_TEST_READ_ONLY_REGISTRY``
points at that runtime's target.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time

import pytest

from macp_sdk import (
    AuthConfig,
    MacpClient,
    MacpTransportError,
    TaskSession,
    build_contribute_payload,
    build_envelope,
    build_session_start_payload,
    new_session_id,
    serialize_message,
)
from tests.integration.conftest import make_client as _client

pytestmark = pytest.mark.integration


# ── Subscribe resume exclusivity (C-1) ────────────────────────────────


class TestSubscribeResumeExclusivity:
    def test_after_sequence_n_does_not_redeliver_envelope_n(self) -> None:
        """Resuming with ``after_sequence=N`` must deliver envelope N+1 onward
        and never re-deliver envelope N (exclusive contract)."""
        session_id = new_session_id()
        initiator = _client("coordinator")
        try:
            initiator.initialize()
            from macp_sdk import DecisionSession

            session = DecisionSession(client=initiator, session_id=session_id)
            session.start(
                intent="exclusive resume",
                participants=["coordinator", "observer"],
                ttl_ms=30_000,
            )  # accepted envelope #1
            session.propose("p1", "option-a")  # accepted envelope #2
            session.propose("p2", "option-b")  # accepted envelope #3
            time.sleep(0.3)

            observer = _client("observer")
            try:
                stream = observer.open_stream()
                try:
                    # Resume after envelope #2 (exclusive): must deliver only
                    # p2 (#3), never re-deliver SessionStart (#1) or p1 (#2).
                    stream.send_subscribe(session_id, after_sequence=2)
                    seen: list[str] = []
                    deadline = time.time() + 4.0
                    while time.time() < deadline:
                        try:
                            env = stream.read(timeout=0.5)
                        except queue.Empty:
                            if seen:  # got the replayed tail; stream is quiet
                                break
                            continue
                        if env is None:
                            break
                        if env.session_id == session_id:
                            seen.append(env.message_type)
                    # Exclusive contract: exactly one Proposal (p2) replays,
                    # proving envelope #2 (p1) and #1 (SessionStart) were not
                    # re-delivered.
                    assert seen.count("SessionStart") == 0, seen
                    assert seen.count("Proposal") == 1, seen
                finally:
                    stream.close()
            finally:
                observer.close()
            session.cancel(reason="test cleanup")
        finally:
            initiator.close()


# ── WatchSignals authentication (C-3) ─────────────────────────────────


class TestWatchSignalsAuth:
    """Positive path only: the insecure dev runtime accepts any bearer, so the
    unauthenticated-rejection direction cannot be exercised here — the SDK-side
    auth guard for ``watch_signals`` is unit-tested instead."""

    def test_authenticated_watch_signals_receives_signal(self) -> None:
        watcher_client = _client("watcher")
        sender_client = _client("emitter")
        try:
            watcher_client.initialize()
            received: list[object] = []
            errors: list[Exception] = []

            def _watch() -> None:
                try:
                    for resp in watcher_client.watch_signals(timeout=6.0):
                        env = getattr(resp, "envelope", None)
                        if env is not None and env.ByteSize() > 0:
                            received.append(env)
                            return
                except MacpTransportError as exc:  # timeout ends the stream
                    errors.append(exc)

            t = threading.Thread(target=_watch, daemon=True)
            t.start()
            time.sleep(0.5)  # let the watch stream establish
            sender_client.send_signal(signal_type="ping", data=b"hi", sender="emitter")
            t.join(timeout=6.0)

            assert received, f"watcher received no signal (errors={errors!r})"
        finally:
            watcher_client.close()
            sender_client.close()


# ── Task external orchestrator (D-2) ──────────────────────────────────


class TestTaskExternalOrchestrator:
    def test_initiator_not_in_participants_accepted(self) -> None:
        """Task mode allows the initiator to sit outside ``participants``
        (RFC-MACP-0009), provided the pool has an eligible assignee."""
        orchestrator = _client("orchestrator")
        try:
            orchestrator.initialize()
            session = TaskSession(client=orchestrator)
            ack = session.start(
                intent="external orchestrator task",
                participants=["worker-a", "worker-b"],  # orchestrator excluded
                ttl_ms=30_000,
            )
            assert ack.ok, f"SessionStart rejected: {ack}"
            session.cancel(reason="test cleanup")
        finally:
            orchestrator.close()


# ── multi_round Contribute: proto + legacy JSON (B-1) ─────────────────


class TestMultiRoundContribute:
    def _start_multi_round(self, client: MacpClient, session_id: str) -> None:
        payload = build_session_start_payload(
            intent="multi_round smoke",
            participants=["coordinator", "alice"],
            ttl_ms=30_000,
        )
        env = build_envelope(
            mode="ext.multi_round.v1",
            message_type="SessionStart",
            session_id=session_id,
            payload=serialize_message(payload),
            sender="coordinator",
        )
        ack = client.send(env)
        assert ack.ok, f"multi_round SessionStart rejected: {ack}"

    def test_proto_contribute_accepted(self) -> None:
        session_id = new_session_id()
        coordinator = _client("coordinator")
        alice = _client("alice")
        try:
            coordinator.initialize()
            self._start_multi_round(coordinator, session_id)
            env = build_envelope(
                mode="ext.multi_round.v1",
                message_type="Contribute",
                session_id=session_id,
                payload=serialize_message(build_contribute_payload("option_a")),
                sender="alice",
            )
            ack = alice.send(env)
            assert ack.ok, f"proto Contribute rejected: {ack}"
            coordinator.cancel_session(session_id, reason="test cleanup")
        finally:
            coordinator.close()
            alice.close()

    def test_legacy_json_contribute_accepted(self) -> None:
        session_id = new_session_id()
        coordinator = _client("coordinator")
        alice = _client("alice")
        try:
            coordinator.initialize()
            self._start_multi_round(coordinator, session_id)
            env = build_envelope(
                mode="ext.multi_round.v1",
                message_type="Contribute",
                session_id=session_id,
                payload=json.dumps({"value": "option_a"}).encode("utf-8"),
                sender="alice",
            )
            ack = alice.send(env)
            assert ack.ok, f"legacy JSON Contribute rejected: {ack}"
            coordinator.cancel_session(session_id, reason="test cleanup")
        finally:
            coordinator.close()
            alice.close()


# ── ListSessions pagination (B-4) ─────────────────────────────────────


class TestListSessionsPagination:
    def test_pagination_drains_all_pages(self) -> None:
        client = _client("pager")
        created: list[str] = []
        try:
            client.initialize()
            from macp_sdk import DecisionSession

            for i in range(3):
                sid = new_session_id()
                session = DecisionSession(client=client, session_id=sid)
                session.start(
                    intent=f"page-{i}",
                    participants=["pager", "peer"],
                    ttl_ms=30_000,
                )
                created.append(sid)
            time.sleep(0.3)

            # page_size=1 forces multiple pages; auto-pagination must return
            # every session we created (the runtime honours page_size >= 1).
            all_ids = {s.session_id for s in client.list_sessions(page_size=1)}
            for sid in created:
                assert sid in all_ids, f"{sid} missing from paginated list"
        finally:
            for sid in created:
                with contextlib.suppress(Exception):
                    client.cancel_session(sid, reason="test cleanup")
            client.close()


# ── max_suspend_ms binding (B-2) ──────────────────────────────────────


class TestMaxSuspendMs:
    def test_start_with_small_cap_accepted(self) -> None:
        """A small ``max_suspend_ms`` is accepted at SessionStart. (Full
        SUSPENDED→EXPIRED-after-cap timing is a runtime-internal concern and
        left to the runtime's own suite.)"""
        session_id = new_session_id()
        client = _client("coordinator")
        try:
            client.initialize()
            from macp_sdk import DecisionSession

            session = DecisionSession(client=client, session_id=session_id)
            ack = session.start(
                intent="max_suspend_ms bind",
                participants=["coordinator", "peer"],
                ttl_ms=30_000,
                max_suspend_ms=2000,
            )
            assert ack.ok, f"SessionStart with max_suspend_ms rejected: {ack}"
            session.cancel(reason="test cleanup")
        finally:
            client.close()


# ── Read-only policy registry (B-6) — needs a second runtime ──────────


class TestReadOnlyPolicyRegistry:
    def test_register_policy_refused_when_read_only(self) -> None:
        target = os.environ.get("MACP_TEST_READ_ONLY_REGISTRY")
        if not target:
            pytest.skip("set MACP_TEST_READ_ONLY_REGISTRY to a MACP_POLICIES_DIR runtime target")
        from macp_sdk import MacpAckError, build_quorum_policy

        client = MacpClient(
            target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("admin")
        )
        try:
            resp = client.initialize()
            assert resp.capabilities.policy_registry.register_policy is False
            with pytest.raises(MacpAckError) as excinfo:
                client.register_policy(build_quorum_policy("q.ro", "read-only test"))
            assert excinfo.value.failure.code == "FAILED_PRECONDITION"
        finally:
            client.close()
