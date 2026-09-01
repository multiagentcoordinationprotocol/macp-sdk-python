"""Task mode example: planner delegates work to a worker agent.

Demonstrates: request, accept_task, update, complete, commit.
Requires a running MACP runtime, defaulting to localhost:50051 (override
with MACP_RUNTIME_TARGET).
"""

import os

from macp_sdk import AuthConfig, MacpClient
from macp_sdk.task import TaskSession

# --- Per-agent auth configs ---
planner_auth = AuthConfig.for_dev_agent("planner")
worker_auth = AuthConfig.for_dev_agent("worker")

# --- Create client ---
client = MacpClient(
    target=os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051"),
    allow_insecure=True,  # local dev only; production requires TLS (RFC-0006 §3)
    auth=planner_auth,
)

# --- Start task session ---
session = TaskSession(client, auth=planner_auth)
session.start(
    intent="analyze Q4 sales data",
    participants=["planner", "worker"],
    ttl_ms=120_000,
)

# --- Planner creates task request ---
session.request_task(
    "t1",
    "Q4 Sales Analysis",
    instructions="Run the sales pipeline and produce a summary report",
    requested_assignee="worker",
)

# --- Worker accepts ---
session.accept_task("t1", sender="worker", auth=worker_auth)

# --- Worker reports progress ---
session.update_task(
    "t1", status="running", progress=0.5, message="50% complete", sender="worker", auth=worker_auth
)

# --- Worker completes ---
session.complete_task(
    "t1",
    output=b"Q4 revenue: $2.3M",
    summary="Analysis complete",
    sender="worker",
    auth=worker_auth,
)

# --- Planner commits the outcome ---
proj = session.task_projection
if proj.is_completed("t1"):
    session.commit(
        action="task.completed",
        authority_scope="data-analysis",
        reason="Worker delivered output successfully",
    )
    print("Task completed and committed")

client.close()
