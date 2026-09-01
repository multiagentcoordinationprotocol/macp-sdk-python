"""Example: register a governance policy, then run a policy-governed decision session.

Requires a running MACP Rust runtime, defaulting to localhost:50051
(override with MACP_RUNTIME_TARGET). Start the runtime with:
    MACP_ALLOW_INSECURE=1 cargo run
"""

import os

from macp_sdk import (
    AuthConfig,
    CommitmentRules,
    DecisionSession,
    EvaluationRules,
    MacpClient,
    ObjectionHandlingRules,
    VotingRules,
    build_decision_policy,
)


def main() -> None:
    client = MacpClient(
        target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
        allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
        auth=AuthConfig.for_dev_agent("coordinator"),
    )
    try:
        init = client.initialize()
        print("runtime:", init.runtime_info.name)

        # A runtime started with MACP_POLICIES_DIR serves a read-only policy
        # registry and refuses register/unregister with FAILED_PRECONDITION.
        # Check the advertised capability before attempting a mutation.
        if not init.capabilities.policy_registry.register_policy:
            print("policy registry is read-only (MACP_POLICIES_DIR); skipping register")
            return

        # ── Build a governance policy ────────────────────────────
        # Rule fields match Runtime's src/policy/rules.rs exactly
        policy = build_decision_policy(
            policy_id="policy.deploy.majority-veto",
            description="Majority vote with veto power for blocking objections",
            voting=VotingRules(
                algorithm="majority",
                threshold=0.5,
                quorum_type="count",
                quorum_value=2,
            ),
            objection_handling=ObjectionHandlingRules(
                critical_severity_vetoes=True,
                veto_threshold=1,
            ),
            evaluation=EvaluationRules(
                minimum_confidence=0.7,
                required_before_voting=True,
            ),
            commitment=CommitmentRules(
                authority="initiator_only",
                require_vote_quorum=True,
            ),
        )

        # ── Register with the runtime ────────────────────────────
        resp = client.register_policy(policy)
        print("registered:", resp.ok)

        # ── Verify it's listed ───────────────────────────────────
        listed = client.list_policies(mode="macp.mode.decision.v1")
        print("policies:", [d.policy_id for d in listed.descriptors])

        # ── Retrieve by ID ───────────────────────────────────────
        got = client.get_policy("policy.deploy.majority-veto")
        print("retrieved:", got.policy_descriptor.policy_id, got.policy_descriptor.description)

        # ── Run a session with this policy ───────────────────────
        session = DecisionSession(
            client,
            policy_version="policy.deploy.majority-veto",
        )
        # Initiator must be in participants to propose
        session.start(
            intent="approve production deployment v2.1",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=60_000,
        )
        session.propose("p1", "deploy-v2.1", rationale="canary checks passed")

        # Evaluations (required before voting by policy)
        session.evaluate(
            "p1",
            "APPROVE",
            confidence=0.95,
            reason="risk low",
            sender="alice",
            auth=AuthConfig.for_dev_agent("alice"),
        )
        session.evaluate(
            "p1",
            "APPROVE",
            confidence=0.85,
            reason="tests green",
            sender="bob",
            auth=AuthConfig.for_dev_agent("bob"),
        )

        # Votes (quorum of 2 required by policy)
        session.vote(
            "p1",
            "APPROVE",
            reason="ship it",
            sender="alice",
            auth=AuthConfig.for_dev_agent("alice"),
        )
        session.vote(
            "p1",
            "APPROVE",
            reason="lgtm",
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

        # ── Cleanup ──────────────────────────────────────────────
        client.unregister_policy("policy.deploy.majority-veto")
        print("unregistered policy")

    finally:
        client.close()


if __name__ == "__main__":
    main()
