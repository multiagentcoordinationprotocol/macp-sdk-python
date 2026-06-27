# Quorum Mode

**Mode URI:** `macp.mode.quorum.v1`
**Status:** provisional
**RFC:** RFC-MACP-0011

Threshold-based approval or rejection. N-of-M participants must approve for the action to pass. Designed for governance, compliance gates, and multi-party authorization.

> **Runtime semantics:** threshold resolution (including policy overrides), abstention handling, and commitment readiness are defined in [Runtime Modes § Quorum Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#quorum-mode). Bound a [policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md) to override `required_approvals` at the runtime. This page covers the SDK API.

## When to use

Use Quorum mode when an action requires approval from a minimum number of parties:

- Security policy changes (require 3-of-5 security team members)
- Production deployments (require 2-of-3 reviewers)
- Budget approvals (require manager + finance)
- Any N-of-M voting scenario

## Participant model: quorum

Threshold-based, not unanimous. The `required_approvals` field sets the bar. Each eligible participant casts at most one ballot (approve, reject, or abstain). The session resolves when the threshold is reached or becomes mathematically unreachable.

## Determinism: semantic-deterministic

Same accepted envelope sequence → same ballot counts and threshold outcome. The quorum result is fully determined by the message history.

## Message flow

```
SessionStart
  ↓
ApprovalRequest (defines action, threshold)
  ↓
Approve / Reject / Abstain (participants cast ballots)
  ↓
Commitment → RESOLVED
```

### Key semantics

- At most **one ApprovalRequest** per session (v1)
- `required_approvals` must be > 0 and ≤ participant count
- Each participant casts at most **one ballot** — later ballots override earlier ones
- Commitment is eligible when:
    - Approvals ≥ `required_approvals` (threshold reached), OR
    - Remaining possible approvals cannot reach threshold (mathematically unreachable)

## Authorization & termination

Per-message authorization and the runtime's commitment-readiness check (threshold reached *or* mathematically unreachable) are defined in [Runtime Modes § Quorum Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#quorum-mode). Use `proj.commitment_ready(total_eligible)` to mirror that check on the SDK side before calling `commit()`.

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.quorum import QuorumSession

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=AuthConfig.for_dev_agent("coordinator"))
session = QuorumSession(client)
session.start(
    intent="approve security policy update",
    participants=["coordinator", "alice", "bob", "carol", "dave", "eve"],
    ttl_ms=86_400_000,  # 24 hours
)

# Coordinator creates the approval request
session.request_approval(
    "r1",
    "security-policy-tls13",
    summary="Enforce TLS 1.3 minimum across all services",
    details=b'{"affected_services": 47, "rollout_plan": "gradual over 2 weeks"}',
    required_approvals=3,
)

# Participants vote over time
session.approve("r1", reason="long overdue improvement", sender="alice")
session.reject("r1", reason="too aggressive timeline", sender="bob")
session.approve("r1", reason="security best practice", sender="carol")
session.abstain("r1", reason="not in my domain", sender="dave")
session.approve("r1", reason="agreed", sender="eve")

# Check and commit
proj = session.quorum_projection
total_eligible = 5  # all participants except coordinator

if proj.has_quorum():
    session.commit(
        action="quorum.approved",
        authority_scope="security-policy",
        reason=f"{proj.approval_count()} of {total_eligible} approved (threshold: 3)",
    )
elif proj.is_threshold_unreachable(total_eligible):
    session.commit(
        action="quorum.rejected",
        authority_scope="security-policy",
        reason=f"Only {proj.approval_count()} approvals possible, need 3",
    )
```

## Projection queries

```python
proj = session.quorum_projection

# Request metadata
proj.request                              # ApprovalRequestRecord or None
proj.request.required_approvals           # 3
proj.request.action                       # "security-policy-tls13"

# Ballots
proj.ballots                              # dict[sender, BallotRecord]
proj.ballots["alice"].choice              # "approve"
proj.ballots["bob"].choice                # "reject"

# Counts
proj.approval_count()                     # 3
proj.rejection_count()                    # 1
proj.abstention_count()                   # 1

# Threshold logic
proj.has_quorum()               # True (3 >= 3)
proj.is_threshold_unreachable(5)          # False
proj.commitment_ready(5)                  # True (threshold reached OR unreachable)

# Lifecycle
proj.phase                                # "Pending" | "Voting" | "Committed"
proj.is_committed                         # True after Commitment
```

## Ballot override

If the same sender votes multiple times, the **latest ballot supersedes** the previous one:

```python
session.reject("r1", sender="alice")   # alice initially rejects
session.approve("r1", sender="alice")  # alice changes to approve

proj.ballots["alice"].choice  # "approve" (latest wins)
proj.approval_count()         # 1 (not 0)
```

## Orchestrator patterns

### Deadline-based auto-commit

```python
import time

session.request_approval("r1", "deploy", required_approvals=2)

deadline = time.time() + 3600  # 1 hour
while time.time() < deadline:
    # ... collect votes asynchronously ...
    if proj.commitment_ready(total_eligible=5):
        break
    time.sleep(10)

if proj.has_quorum():
    session.commit(action="approved", ...)
else:
    session.commit(action="rejected", reason="deadline reached without quorum")
```

### Weighted quorum (orchestrator logic)

The SDK tracks raw ballot counts. For weighted voting (e.g., senior reviewers count double), implement the weighting in your orchestrator:

```python
weights = {"alice": 2, "bob": 1, "carol": 1}
weighted_approvals = sum(
    weights.get(sender, 1)
    for sender, ballot in proj.ballots.items()
    if ballot.choice == "approve"
)
if weighted_approvals >= required_weighted:
    session.commit(...)
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` on Approve/Reject/Abstain | Sender not a declared participant | Verify sender |
| `INVALID_ENVELOPE` | Second ApprovalRequest in same session | Only one per session (v1) |
| `FORBIDDEN` on Commitment | Sender not the coordinator | Only initiator can commit |

## API Reference

::: macp_sdk.quorum.QuorumSession

::: macp_sdk.quorum.QuorumProjection
