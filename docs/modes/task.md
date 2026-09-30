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

## External orchestrator (runtime v0.5.0)

Unlike the other modes, Task mode lets the **initiator sit outside
`participants`**. Since runtime v0.5.0 (RFC-MACP-0009), `SessionStart` no
longer requires the initiator to be a member: `TaskRequest` is authorized by
the initiator *role*, not by membership. This lets a standalone orchestrator
open a task session for a pool of worker agents it delegates to without
becoming an assignee itself.

Two rules still hold:

- The `participants` pool must contain **at least one eligible assignee other
  than the initiator** — a task needs someone to do it.
- Only the initiator (the requester) can commit the terminal outcome.

```python
# Orchestrator "coordinator" is NOT in participants — it delegates only.
session = TaskSession(client, auth=AuthConfig.for_dev_agent("coordinator"))
session.start(
    intent="summarize the incident",
    participants=["worker-a", "worker-b"],   # assignees; initiator excluded
    ttl_ms=60_000,
)
```

> This is Task-specific. **Handoff** still requires the initiator in
> `participants` (RFC-MACP-0010 §2 — intrinsic to the delegated model), and
> **Decision** likewise keeps its initiator-membership rule (RFC-MACP-0007
> §2). Do not generalize the external-orchestrator pattern to those modes.

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.task import TaskSession

# Per-agent auth configs
planner_auth = AuthConfig.for_dev_agent("planner")
analyst_auth = AuthConfig.for_dev_agent("analyst-agent")

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=planner_auth)
session = TaskSession(client, auth=planner_auth)
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
session.accept_task("t1", sender="analyst-agent", auth=analyst_auth)

# Worker reports progress
session.update_task(
    "t1", status="running", progress=0.3, message="Loading datasets...",
    sender="analyst-agent", auth=analyst_auth,
)
session.update_task(
    "t1", status="running", progress=0.7, message="Computing trends...",
    sender="analyst-agent", auth=analyst_auth,
)

# Worker completes
session.complete_task(
    "t1",
    output=b'{"revenue": "$2.3M", "growth": "12%", "top_product": "Widget Pro"}',
    summary="Q4 revenue up 12% YoY, driven by Widget Pro",
    sender="analyst-agent",
    auth=analyst_auth,
)

# Requester commits the outcome
proj = session.task_projection
if proj.is_completed("t1"):
    session.commit(
        action="task.completed",
        authority_scope="data-analysis",
        reason="analyst-agent delivered Q4 analysis",
    )
```

## Projection queries

```python
proj = session.task_projection
task_id = "t1"

# Per-task current state (request fields + live status/progress/assignee)
proj.tasks                        # dict[str, TaskRecord] -- every task_id seen this session
proj.get_task(task_id)            # TaskRecord or None
proj.get_task(task_id).status     # "requested" | "accepted" | "in_progress" |
                                   # "completed" | "failed" | "rejected"
proj.get_task(task_id).progress   # 0.0-1.0
proj.get_task(task_id).assignee   # str or None
proj.active_tasks()               # list[TaskRecord] with status in
                                   # {"requested", "accepted", "in_progress"}
proj.active_assignment            # (sender, task_id) or None -- the session-scoped
                                   # single-assignee slot (RFC-MACP-0009 §5 rule 3),
                                   # distinct from a per-task `.assignee` field above

# Convenience reads (equivalent to the fields above, kept for existing callers).
# For an unknown task_id these return None/0.0 rather than raising, unlike
# get_task(task_id) itself, which returns None and would raise on attribute
# access.
proj.current_status(task_id)      # same as get_task(task_id).status, or None
proj.current_assignee(task_id)    # same as get_task(task_id).assignee, or None
proj.progress_of(task_id)         # same as get_task(task_id).progress, or 0.0
proj.is_accepted(task_id)         # status in {"accepted", "in_progress"}
proj.is_completed(task_id)        # status == "completed"
proj.is_failed(task_id)           # status == "failed"
proj.is_retryable(task_id)        # True if it failed with retryable=True

# Full audit trails
proj.updates                      # list[TaskUpdateRecord]
proj.rejections                   # list[TaskRejectRecord]
proj.completions                  # list[TaskCompleteRecord]
proj.failures                     # list[TaskFailRecord]
proj.latest_progress()            # 0.7 (from proj.updates[-1], or None)

# Lifecycle
proj.phase                        # "Pending" | "Requested" | "InProgress" |
                                   # "Completed" | "Failed" | "Committed"
```

> **`TaskUpdateRecord.status` is not `TaskRecord.status`.** The former is
> the raw wire value a worker reported in a `TaskUpdate` (e.g. `"running"`,
> whatever string the caller chose); the latter is the projection's own
> derived lifecycle state (`"requested"` | `"accepted"` | `"in_progress"` |
> `"completed"` | `"failed"` | `"rejected"`). They share a field name, not
> a meaning.

> **`get_task()` and `active_tasks()` return the projection's own live
> record objects, not copies.** Treat them as read-only — mutating a
> returned `TaskRecord` mutates the projection's internal state directly
> (same as `ProposalProjection`'s equivalent accessors).

## Handling task failures

```python
if proj.is_failed(task_id):
    report = proj.failures[-1]  # the most recent TaskFailRecord
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
