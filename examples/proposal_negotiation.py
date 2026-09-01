"""Proposal mode example: buyer/seller negotiation.

Demonstrates: propose, counter_propose, accept, reject, commit.
Requires a running MACP runtime, defaulting to localhost:50051 (override
with MACP_RUNTIME_TARGET).
"""

import os

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.proposal import ProposalSession

# --- Create clients for each participant ---
coordinator_auth = AuthConfig.for_dev_agent("coordinator")
coordinator = MacpClient(
    target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
    allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
    auth=coordinator_auth,
)
buyer = AuthConfig.for_dev_agent("buyer")
seller = AuthConfig.for_dev_agent("seller")

# --- Start a proposal session ---
# The coordinator is a neutral convener here, not a negotiating party, so it
# is deliberately left out of `participants`. Proposal mode's default
# acceptance criterion is `all_parties` (runtime: proposal.rs's convergence
# rule), which requires every declared participant to accept the same live
# proposal before commit is possible -- a non-voting coordinator in
# `participants` would make convergence unreachable. The initiator retains
# Commitment authority regardless of participant membership (RFC-MACP-0007 §2).
session = ProposalSession(coordinator, auth=coordinator_auth)
session.start(
    intent="negotiate service contract terms",
    participants=["buyer", "seller"],
    ttl_ms=60_000,
)

# --- Seller makes initial proposal ---
session.propose(
    "p1", "Standard Package", summary="$100k/year, basic SLA", sender="seller", auth=seller
)

# --- Buyer counter-proposes ---
session.counter_propose(
    "p2",
    "p1",
    "Enhanced Package",
    summary="$80k/year, enhanced SLA",
    sender="buyer",
    auth=buyer,
)

# --- Seller accepts the counter-proposal ---
session.accept("p2", reason="terms acceptable", sender="seller", auth=seller)
# --- Buyer also accepts ---
session.accept("p2", reason="agreed", sender="buyer", auth=buyer)

# --- Check convergence and commit ---
proj = session.proposal_projection
if proj.accepted_proposal() == "p2":
    session.commit(
        action="contract.agreed",
        authority_scope="procurement",
        reason="Both parties accepted proposal p2",
    )
    print(f"Negotiation resolved: {proj.commitment.action}")  # type: ignore[union-attr]
else:
    print("No convergence reached")

coordinator.close()
