"""Integration tests for the asyncio client (``AsyncMacpClient``).

Drives real sessions through ``grpc.aio`` against the live runtime: a
Decision session to ``SESSION_STATE_RESOLVED`` (the async quickstart), a
Quorum session, a late-joiner ``StreamSession`` subscribe/replay, a
``WatchSessions`` async iterator, and the async error mapping.

Each test owns its event loop via ``asyncio.run`` and creates its clients
inside it, because ``grpc.aio`` channels bind to the loop they are created
on.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from macp.v1.envelope_pb2 import SessionState

from macp_sdk import (
    AsyncDecisionSession,
    AsyncMacpClient,
    AsyncQuorumSession,
    AuthConfig,
    MacpAckError,
    new_session_id,
)
from tests.integration.conftest import RUNTIME_TARGET

pytestmark = pytest.mark.integration


def _auth(agent: str) -> AuthConfig:
    return AuthConfig.for_dev_agent(agent)


def _client(agent: str) -> AsyncMacpClient:
    return AsyncMacpClient(target=RUNTIME_TARGET, allow_insecure=True, auth=_auth(agent))


class TestAsyncDecision:
    def test_decision_session_resolves(self) -> None:
        async def run() -> None:
            async with _client("coordinator") as client:
                init = await client.initialize()
                assert init.selected_protocol_version

                session = AsyncDecisionSession(client)
                ack = await session.start(
                    intent="async decision",
                    participants=["coordinator", "alice", "bob"],
                    ttl_ms=30_000,
                )
                assert ack.ok
                assert (await session.propose("p1", "deploy v2.1", rationale="green")).ok
                ack = await session.evaluate(
                    "p1",
                    "APPROVE",
                    confidence=0.9,
                    reason="low risk",
                    sender="alice",
                    auth=_auth("alice"),
                )
                assert ack.ok
                ack = await session.vote("p1", "APPROVE", sender="bob", auth=_auth("bob"))
                assert ack.ok

                winner = session.decision_projection.majority_winner()
                assert winner == "p1"
                ack = await session.commit(
                    action="deployment.approved",
                    authority_scope="release-management",
                    reason=f"winner={winner}",
                )
                assert ack.ok

                meta = (await session.metadata()).metadata
                assert meta.state == SessionState.SESSION_STATE_RESOLVED
                assert meta.mode == "macp.mode.decision.v1"
                assert session.projection.commitment is not None

                listed = await client.list_sessions()
                assert session.session_id in {s.session_id for s in listed}

        asyncio.run(run())

    def test_nack_raises_ack_error(self) -> None:
        async def run() -> None:
            async with _client("coordinator") as client:
                session = AsyncDecisionSession(client)
                # Proposal before SessionStart: the runtime rejects it.
                with pytest.raises(MacpAckError):
                    await session.propose("p1", "too early")

        asyncio.run(run())


class TestAsyncQuorum:
    def test_quorum_session_resolves(self) -> None:
        async def run() -> None:
            async with _client("coordinator") as client:
                session = AsyncQuorumSession(client)
                ack = await session.start(
                    intent="async quorum",
                    participants=["coordinator", "alice", "bob", "charlie"],
                    ttl_ms=30_000,
                )
                assert ack.ok
                assert (
                    await session.request_approval(
                        "r1", "deploy v2", summary="prod", required_approvals=2
                    )
                ).ok
                assert (await session.approve("r1", sender="alice", auth=_auth("alice"))).ok
                assert (await session.approve("r1", sender="bob", auth=_auth("bob"))).ok
                ack = await session.commit(
                    action="deploy.approved", authority_scope="ops", reason="quorum reached"
                )
                assert ack.ok
                assert session.quorum_projection.has_quorum("r1")
                meta = (await session.metadata()).metadata
                assert meta.state == SessionState.SESSION_STATE_RESOLVED

        asyncio.run(run())


class TestAsyncStreaming:
    def test_late_joiner_stream_replays_history(self) -> None:
        async def run() -> None:
            session_id = new_session_id()
            async with _client("coordinator") as initiator, _client("observer") as observer:
                session = AsyncDecisionSession(initiator, session_id=session_id)
                assert (
                    await session.start(
                        intent="async subscribe replay",
                        participants=["coordinator", "observer"],
                        ttl_ms=30_000,
                    )
                ).ok
                assert (await session.propose("p1", "option-a")).ok

                seen: list[str] = []
                async with observer.open_stream() as stream:
                    await stream.send_subscribe(session_id)
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(5.0):
                            async for env in stream.responses(timeout=5.0):
                                if env.session_id == session_id:
                                    seen.append(env.message_type)
                                if {"SessionStart", "Proposal"} <= set(seen):
                                    break
                assert "SessionStart" in seen, seen
                assert "Proposal" in seen, seen
                await session.cancel(reason="test cleanup")

        asyncio.run(run())

    def test_watch_sessions_sees_new_session(self) -> None:
        async def run() -> None:
            session_id = new_session_id()
            async with _client("observer") as observer, _client("coordinator") as initiator:
                seen = asyncio.Event()

                async def consume() -> None:
                    async for resp in observer.watch_sessions():
                        if resp.event.session.session_id == session_id:
                            seen.set()
                            return

                consumer = asyncio.create_task(consume())
                await asyncio.sleep(0.2)  # let the watch register server-side
                session = AsyncDecisionSession(initiator, session_id=session_id)
                assert (
                    await session.start(
                        intent="async watch_sessions",
                        participants=["coordinator", "alice"],
                        ttl_ms=30_000,
                    )
                ).ok
                try:
                    await asyncio.wait_for(seen.wait(), timeout=5.0)
                finally:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                    await session.cancel(reason="test cleanup")

        asyncio.run(run())
