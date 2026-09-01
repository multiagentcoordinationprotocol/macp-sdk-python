import os

from macp_sdk import AuthConfig, DecisionSession, MacpClient


def main() -> None:
    client = MacpClient(
        target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
        allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
        auth=AuthConfig.for_dev_agent("coordinator"),
    )
    try:
        init = client.initialize()
        print("initialize:", init.selected_protocol_version, init.runtime_info.name)

        session = DecisionSession(client)
        # Initiator must be in participants to propose
        session.start(
            intent="pick a deployment",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=60_000,
        )
        session.propose("p1", "deploy-v2.1", rationale="canary checks passed")
        session.evaluate(
            "p1",
            "APPROVE",
            confidence=0.95,
            reason="risk low",
            sender="alice",
            auth=AuthConfig.for_dev_agent("alice"),
        )
        session.vote(
            "p1",
            "APPROVE",
            reason="ship it",
            sender="bob",
            auth=AuthConfig.for_dev_agent("bob"),
        )

        winner = session.projection.majority_winner()
        print("winner:", winner)
        session.commit(
            action="deployment.approved",
            authority_scope="release-management",
            reason=f"winner={winner}",
        )
        metadata = session.metadata().metadata
        print("state:", metadata.state, "mode:", metadata.mode)
    finally:
        client.close()


if __name__ == "__main__":
    main()
