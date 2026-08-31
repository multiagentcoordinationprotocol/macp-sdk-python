"""Quorum mode example: N-of-M threshold approval.

Demonstrates: request_approval, approve, reject, abstain, commit.
Requires a running MACP runtime on localhost:50051.
"""

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.quorum import QuorumSession

# --- Create client ---
client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
    auth=AuthConfig.for_dev_agent("coordinator"),
)

# --- Start quorum session ---
session = QuorumSession(client, auth=AuthConfig.for_dev_agent("coordinator"))
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
session.approve("r1", reason="good improvement", sender="alice")
session.reject("r1", reason="too aggressive timeline", sender="bob")
session.approve("r1", reason="long overdue", sender="carol")
session.approve("r1", reason="agreed", sender="dave")

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
