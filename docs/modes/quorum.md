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
- Each participant casts at most **one ballot** — the first ballot stands and later ballots from the same sender are discarded (see ["First ballot stands"](#first-ballot-stands) below)

> **`threshold` is the approval bar, not a participation quorum (RFC-MACP-0012 §4.2).** When you override `required_approvals` with a bound policy via `build_quorum_policy(threshold=QuorumThreshold(...))`, the `threshold.value` is an **integer**: an approval *count* for `type="n_of_m"` / `"weighted"`, and an integer *percentage 0-100* for `type="percentage"`. There is no separate participation quorum in schema_version ≤ 2. `build_quorum_policy` rejects a non-integer value (e.g. a fractional `0.75`), a negative value, and a `percentage` value over `100` — all with `MacpSessionError` — matching the runtime's schema so a bad descriptor fails immediately instead of round-tripping through `RegisterPolicy`.
- Commitment is eligible when:
    - Approvals ≥ `required_approvals` (threshold reached), OR
    - Remaining possible approvals cannot reach threshold (mathematically unreachable)

## Authorization & termination

Per-message authorization and the runtime's commitment-readiness check (threshold reached *or* mathematically unreachable) are defined in [Runtime Modes § Quorum Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#quorum-mode). `proj.commitment_ready(request_id)` mirrors the threshold-reached half of that check on the SDK side (and folds in "not already committed"); pair it with `proj.is_threshold_unreachable(request_id, total_eligible)` for the mathematically-unreachable half before calling `commit()`.

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.quorum import QuorumSession

# Per-agent auth configs
coordinator_auth = AuthConfig.for_dev_agent("coordinator")
alice_auth = AuthConfig.for_dev_agent("alice")
bob_auth = AuthConfig.for_dev_agent("bob")
carol_auth = AuthConfig.for_dev_agent("carol")
dave_auth = AuthConfig.for_dev_agent("dave")
eve_auth = AuthConfig.for_dev_agent("eve")

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=coordinator_auth)
session = QuorumSession(client, auth=coordinator_auth)
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
session.approve("r1", reason="long overdue improvement", sender="alice", auth=alice_auth)
session.reject("r1", reason="too aggressive timeline", sender="bob", auth=bob_auth)
session.approve("r1", reason="security best practice", sender="carol", auth=carol_auth)
session.abstain("r1", reason="not in my domain", sender="dave", auth=dave_auth)
session.approve("r1", reason="agreed", sender="eve", auth=eve_auth)

# Check and commit
proj = session.quorum_projection
request_id = "r1"
# Every participant in the SessionStart list is an eligible ballot caster.
# RFC-MACP-0011 §2.1: the coordinator is eligible only because it appears in
# `participants` -- the coordinator role itself confers nothing.
participants = ["coordinator", "alice", "bob", "carol", "dave", "eve"]
total_eligible = len(participants)  # 6

if proj.has_quorum(request_id):
    session.commit(
        action="quorum.approved",
        authority_scope="security-policy",
        reason=f"{proj.approval_count(request_id)} of {total_eligible} approved (threshold: 3)",
    )
elif proj.is_threshold_unreachable(request_id, total_eligible):
    session.commit(
        action="quorum.rejected",
        authority_scope="security-policy",
        reason=f"Only {proj.approval_count(request_id)} approvals possible, need 3",
    )
```

## Projection queries

All query methods take the `request_id` they apply to. A session accepts at
most one `ApprovalRequest` (RFC-MACP-0011 §5, rule 1), but the projection's
internal `requests` / `ballots` maps are still keyed by `request_id` — that
implementation shape is what the accessors mirror, not multiple concurrent
requests.

```python
proj = session.quorum_projection
request_id = "r1"

# Request metadata
proj.requests.get(request_id)                     # ApprovalRequestRecord or None
proj.requests[request_id].required_approvals      # 3
proj.requests[request_id].action                  # "security-policy-tls13"

# Ballots -- keyed request_id -> sender -> BallotRecord
proj.ballots                                       # dict[request_id, dict[sender, BallotRecord]]
proj.ballots[request_id]["alice"].vote             # "approve"
proj.ballots[request_id]["bob"].vote               # "reject"

# Counts
proj.approval_count(request_id)                    # 3
proj.rejection_count(request_id)                   # 1
proj.abstention_count(request_id)                  # 1

# Threshold logic
proj.has_quorum(request_id)                                  # True (3 >= 3)
proj.is_threshold_unreachable(request_id, total_eligible=6)  # False
proj.commitment_ready(request_id)               # has_quorum(request_id) and phase != "Committed"
proj.threshold(request_id)                      # 3 (0 if the request hasn't arrived yet)
proj.voted_senders(request_id)                  # ["alice", "bob", "carol", "dave", "eve"]
proj.remaining_votes_needed(request_id)         # 0 (max(0, required_approvals - approval_count))

# Lifecycle
proj.phase                                # "Pending" | "Voting" | "Committed"
proj.is_committed                         # True after Commitment

# Anomalies -- discarded second ballots (see "First ballot stands" below)
proj.anomalies                            # list[ProjectionAnomaly]
proj.has_anomalies                        # True if any ballot for this session was discarded
```

## First ballot stands

Each eligible participant gets **at most one ballot per request**, across
`Approve`, `Reject`, and `Abstain` combined — enforced by the single funnel
`QuorumProjection._set_ballot` (`quorum.py:92`) that all three call. RFC-MACP-0011
§5 opens "Implementations MUST enforce the following," and rule 3 caps a
participant at one ballot across the three ballot types — that cap is firm.
**RFC-0011 itself is silent on *which* of two ballots stands** if a sender
somehow submits two; that gap is not filled by the RFC. This SDK infers
first-wins from parity with RFC-MACP-0007 §5.3 ("the first accepted `Vote`
stands") and from what the only conforming runtime actually does
(`quorum.rs:164/184/204`, which NACK a second ballot from the same sender
with `INVALID_ENVELOPE` before it ever reaches a projection).

Against a conforming runtime, a second ballot from the same sender never
reaches the projection at all — the runtime rejects it and
`session.approve(...)` (or `.reject()` / `.abstain()`) returns an `Ack` with
`ok=False`, so `_send_and_track` never calls `apply_envelope` for it. The
discard-and-record behavior below is what runs when a projection is fed
ballots directly — a hand-built fixture, a captured/edited transcript, or
any other non-runtime-mediated `apply_envelope` call:

```python
proj.apply_envelope(reject_envelope)   # accepted -- alice's ballot is "reject"
proj.apply_envelope(approve_envelope)  # discarded -- alice already has a ballot on "r1"

proj.ballots["r1"]["alice"].vote  # "reject" (first ballot stands)
proj.approval_count("r1")         # 0, not 1

proj.anomalies[-1].kind        # "duplicate_ballot"
proj.anomalies[-1].sender      # "alice"
proj.anomalies[-1].subject_id  # "r1"
proj.has_anomalies             # True
```

**There is no vote-changing mechanism, and the SDK will not invent one.**
"Alice changes her mind" is not representable by re-sending a ballot.
Supporting that would require a spec-level Retract/Supersede message with
its own cardinality and ordering rules — RFC-MACP-0011 does not define one.
Until it does, the only way to change an outcome is a new session —
a fresh `ApprovalRequest` under a new `request_id`. A second
`ApprovalRequest` in the same session is exactly what RFC-MACP-0011 §5
rule 1 forbids (see ["Error cases"](#error-cases) below), so a new
`request_id` is not an alternative to a new session; it requires one.

## Orchestrator patterns

### Deadline-based auto-commit

```python
import time

request_id = "r1"
session.request_approval(request_id, "deploy", required_approvals=2)
proj = session.quorum_projection

deadline = time.time() + 3600  # 1 hour
while time.time() < deadline:
    # ... collect votes asynchronously ...
    if proj.commitment_ready(request_id):
        break
    time.sleep(10)

if proj.has_quorum(request_id):
    session.commit(action="approved", ...)
else:
    session.commit(action="rejected", reason="deadline reached without quorum")
```

### Weighted quorum (orchestrator logic)

The SDK tracks raw ballot counts. For weighted voting (e.g., senior reviewers count double), implement the weighting in your orchestrator:

```python
request_id = "r1"
weights = {"alice": 2, "bob": 1, "carol": 1}
weighted_approvals = sum(
    weights.get(sender, 1)
    for sender, ballot in proj.ballots.get(request_id, {}).items()
    if ballot.vote == "approve"
)
if weighted_approvals >= required_weighted:
    session.commit(...)
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` on Approve/Reject/Abstain | Sender not a declared participant | Verify sender |
| `INVALID_ENVELOPE` | Second ApprovalRequest in same session | Only one per session (v1) |
| `INVALID_ENVELOPE` | Second ballot (Approve/Reject/Abstain) from the same sender on a request | One ballot per participant per request (see [First ballot stands](#first-ballot-stands)) |
| `FORBIDDEN` on Commitment | Sender not the coordinator | Only initiator can commit |

## API Reference

::: macp_sdk.quorum.QuorumSession

::: macp_sdk.quorum.QuorumProjection
