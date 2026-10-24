"""Async version of the canonical quickstart (``first_session.py``).

Same Decision session as the sync quickstart -- start, propose, evaluate,
vote, commit -- driven through ``AsyncMacpClient`` / ``AsyncDecisionSession``.
Expected output::

    winner: p1
    state: SESSION_STATE_RESOLVED mode: macp.mode.decision.v1
"""

import asyncio
import os

from macp.v1.envelope_pb2 import SessionState

from macp_sdk import AsyncDecisionSession, AsyncMacpClient, AuthConfig


async def main() -> None:
    async with AsyncMacpClient(
        target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
        allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
        auth=AuthConfig.for_dev_agent("coordinator"),
    ) as client:
        session = AsyncDecisionSession(client)
        await session.start(
            intent="pick a deployment plan",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=60_000,
        )
        await session.propose("p1", "deploy v2.1", rationale="tests passed")
        await session.evaluate(
            "p1",
            "APPROVE",
            confidence=0.94,
            reason="low risk",
            sender="alice",
            auth=AuthConfig.for_dev_agent("alice"),
        )
        await session.vote(
            "p1", "APPROVE", reason="ship it", sender="bob", auth=AuthConfig.for_dev_agent("bob")
        )

        winner = session.decision_projection.majority_winner()
        await session.commit(
            action="deployment.approved",
            authority_scope="release-management",
            reason=f"winner={winner}",
        )

        meta = (await session.metadata()).metadata
        print("winner:", winner)
        print("state:", SessionState.Name(meta.state), "mode:", meta.mode)


if __name__ == "__main__":
    asyncio.run(main())
