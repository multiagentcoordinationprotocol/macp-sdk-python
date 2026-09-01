"""Quorum mode example: N-of-M threshold approval.

Demonstrates: request_approval, approve, reject, abstain, commit.
Requires a running MACP runtime, defaulting to localhost:50051 (override
with MACP_RUNTIME_TARGET).
"""

import os

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.quorum import QuorumSession

# --- Per-agent auth configs ---
coordinator_auth = AuthConfig.for_dev_agent("coordinator")
alice_auth = AuthConfig.for_dev_agent("alice")
bob_auth = AuthConfig.for_dev_agent("bob")
carol_auth = AuthConfig.for_dev_agent("carol")
dave_auth = AuthConfig.for_dev_agent("dave")

# --- Create client ---
client = MacpClient(
    target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
    allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
    auth=coordinator_auth,
)

# --- Start quorum session ---
session = QuorumSession(client, auth=coordinator_auth)
session.start(
    intent="approve security policy update",
    participants=["coordinator", "alice", "bob", "carol", "dave"],
    ttl_ms=60_000,
)

# --- Coordinator creates approval request ---
session.request_approval(
    "r1",
    "security-policy-update",
    summary="Update TLS minimum to 1.3",
    required_approvals=3,
)

# --- Participants vote ---
session.approve("r1", reason="good improvement", sender="alice", auth=alice_auth)
session.reject("r1", reason="too aggressive timeline", sender="bob", auth=bob_auth)
session.approve("r1", reason="long overdue", sender="carol", auth=carol_auth)
session.approve("r1", reason="agreed", sender="dave", auth=dave_auth)

# --- Check threshold ---
proj = session.quorum_projection
request_id = "r1"
print(
    f"Approvals: {proj.approval_count(request_id)}, Rejections: {proj.rejection_count(request_id)}"
)
print(f"Threshold reached: {proj.has_quorum(request_id)}")

if proj.has_quorum(request_id):
    session.commit(
        action="quorum.approved",
        authority_scope="security-policy",
        reason=f"{proj.approval_count(request_id)} of 5 approved",
    )
    print("Policy update approved via quorum")

client.close()
