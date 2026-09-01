"""Handoff mode example: transferring responsibility between agents.

Demonstrates: offer, add_context, accept_handoff, commit.
Requires a running MACP runtime, defaulting to localhost:50051 (override
with MACP_RUNTIME_TARGET).
"""

import os

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.handoff import HandoffSession

# --- Per-agent auth configs ---
owner_a_auth = AuthConfig.for_dev_agent("owner-a")
owner_b_auth = AuthConfig.for_dev_agent("owner-b")

# --- Create client ---
client = MacpClient(
    target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
    allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
    auth=owner_a_auth,
)

# --- Start handoff session ---
session = HandoffSession(client, auth=owner_a_auth)
session.start(
    intent="transfer service-xyz ownership",
    participants=["owner-a", "owner-b"],
    ttl_ms=60_000,
)

# --- Owner-A offers the handoff ---
session.offer(
    "h1",
    "owner-b",
    scope="service-xyz",
    reason="team rotation",
)

# --- Owner-A attaches context ---
session.add_context(
    "h1",
    content_type="application/json",
    context=b'{"runbooks": "https://wiki/service-xyz", "oncall": "owner-b"}',
)

# --- Owner-B accepts ---
session.accept_handoff("h1", sender="owner-b", auth=owner_b_auth)

# --- Commit the handoff ---
proj = session.handoff_projection
if proj.is_accepted("h1"):
    session.commit(
        action="handoff.accepted",
        authority_scope="service-ownership",
        reason="owner-b now holds service-xyz",
    )
    print("Handoff completed successfully")

client.close()
