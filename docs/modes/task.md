# Task Mode

**Mode URI:** `macp.mode.task.v1`
**Status:** provisional
**RFC:** RFC-MACP-0009

Bounded task delegation from a requester to an assignee. The requester defines work; the assignee accepts, executes, and reports results.

> **Runtime semantics:** assignment lifecycle, the `allow_reassignment_on_reject` policy rule, and terminal-report rules are defined in [Runtime Modes § Task Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#task-mode). This page covers the SDK API.

## When to use

Use Task mode when one agent needs to delegate bounded work to another:

- Data analysis or processing pipelines
- Code review or testing delegations
- Document generation or translation
- Any requester→worker delegation pattern

## Participant model: orchestrated

The requester **directs** the assignee. Roles are asymmetric:

- **Requester** (session initiator): Creates the task, receives results, commits outcome
- **Assignee** (worker): Accepts/rejects the task, reports progress and completion/failure

## Determinism: structural-only

Session lifecycle transitions (OPEN → RESOLVED) are deterministic on replay. However, the **semantic outcome** (the actual task output) is **not guaranteed** — the assignee's external execution may produce different results on replay.

The Commitment documents the *intended* outcome. Use the "plan then execute" pattern for critical side effects.

## Message flow

```
SessionStart
  ↓
TaskRequest (requester defines work)
  ↓
TaskAccept / TaskReject (assignee responds)
  ↓
TaskUpdate (progress reports)
  ↓
TaskComplete / TaskFail (assignee reports terminal status)
  ↓
Commitment → RESOLVED
```

### Key semantics

- At most **one TaskRequest** per session (v1)
- Only the **requested assignee** (or any non-initiator if unspecified) can accept
- Only **one assignee** becomes active per session
- TaskComplete/TaskFail do **not** resolve the session — only Commitment does
- The requester commits after reviewing the terminal report

## Authorization & termination

Per-message authorization and commitment-readiness rules are defined in [Runtime Modes § Task Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#task-mode). The runtime validates "active assignee" against the authenticated sender, not a payload field — spoofed assignees fail at transport.

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.task import TaskSession

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=AuthConfig.for_dev_agent("planner"))
session = TaskSession(client)
session.start(
    intent="analyze Q4 sales data",
    participants=["planner", "analyst-agent"],
    ttl_ms=300_000,  # 5 minutes
)

# Requester creates the task
session.request_task(
    "t1", "Q4 Sales Analysis",
    instructions="Run the sales pipeline, produce a summary with key metrics and trends",
    requested_assignee="analyst-agent",
    input_data=b'{"quarter": "Q4", "year": 2025}',
    deadline_unix_ms=1735689600000,  # optional soft deadline
)

# Worker accepts
session.accept_task("t1", sender="analyst-agent")

# Worker reports progress
session.update_task("t1", status="running", progress=0.3, message="Loading datasets...", sender="analyst-agent")
session.update_task("t1", status="running", progress=0.7, message="Computing trends...", sender="analyst-agent")

# Worker completes
session.complete_task(
    "t1",
    output=b'{"revenue": "$2.3M", "growth": "12%", "top_product": "Widget Pro"}',
    summary="Q4 revenue up 12% YoY, driven by Widget Pro",
    sender="analyst-agent",
)

# Requester commits the outcome
proj = session.task_projection
if proj.is_completed():
    session.commit(
        action="task.completed",
        authority_scope="data-analysis",
        reason="analyst-agent delivered Q4 analysis",
    )
```

## Projection queries

```python
proj = session.task_projection

# Task metadata
proj.task                    # TaskRequestRecord or None
proj.task.task_id            # "t1"
proj.task.requested_assignee # "analyst-agent"

# Assignment
proj.active_assignee         # "analyst-agent" or None
proj.is_accepted()           # True after TaskAccept

# Progress
proj.updates                 # list[TaskUpdateRecord]
proj.latest_progress()       # 0.7 (last reported)

# Terminal report
proj.terminal_report         # TaskCompleteRecord | TaskFailRecord | None
proj.is_completed()          # True if TaskComplete received
proj.is_failed()             # True if TaskFail received

# Rejections (before acceptance)
proj.rejections              # list[TaskRejectRecord]

# Lifecycle
proj.phase                   # "Pending" | "Requested" | "InProgress" | "Completed" | "Failed" | "Committed"
```

## Handling task failures

```python
if proj.is_failed():
    report = proj.terminal_report
    print(f"Task failed: {report.error_code} — {report.reason}")
    if report.retryable:
        # Create a new session for retry
        retry_session = TaskSession(client, auth=planner_auth)
        retry_session.start(intent="retry: " + session.session_id, ...)
    else:
        session.commit(
            action="task.failed",
            authority_scope="data-analysis",
            reason=f"Non-retryable failure: {report.error_code}",
        )
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` on TaskAccept | Sender is not the requested assignee | Only the specified assignee can accept |
| `FORBIDDEN` on TaskUpdate | Sender is not the active assignee | Only the accepted assignee can send updates |
| `FORBIDDEN` on Commitment | Sender is not the requester | Only the session initiator can commit |
| `INVALID_ENVELOPE` | Second TaskRequest in same session | Only one TaskRequest per session (v1) |

## API Reference

::: macp_sdk.task.TaskSession

::: macp_sdk.task.TaskProjection
