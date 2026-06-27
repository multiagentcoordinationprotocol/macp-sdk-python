# Decision Mode

**Mode URI:** `macp.mode.decision.v1`
**Status:** permanent
**RFC:** RFC-MACP-0007

Structured decision making with proposals, evaluations, objections, votes, and terminal commitment.

> **Runtime semantics:** phase progression, value normalization, and commitment-readiness rules are defined in [Runtime Modes § Decision Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#decision-mode). Per-mode authorization and termination rules live in the protocol spec — see [protocol modes](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/modes.md). This page covers the SDK API.

## When to use

Use Decision mode when multiple agents need to converge on a single outcome from a set of options. Common scenarios:

- Selecting a deployment plan from multiple candidates
- Choosing a vendor from shortlisted options
- Approving or rejecting a design proposal
- Any situation where agents propose, discuss, and vote

## Participant model: declared

Participants are **declared at SessionStart** and fixed for the session's lifetime. Only declared participants can send mode-specific messages. The session initiator (typically the coordinator) can send any message type.

## Determinism: semantic-deterministic

Same accepted envelope sequence → same semantic outcome. The vote counts, winner, and commitment will always be identical on replay. No external I/O or time-dependent logic is involved (beyond TTL).

## Message flow

```
SessionStart
  ↓
Proposal (one or more options)
  ↓
Evaluation (analysis with recommendation: APPROVE|REVIEW|BLOCK|REJECT)
  ↓
Objection (concerns with severity: low|medium|high|critical)
  ↓
Vote (per-participant: approve|reject|abstain)
  ↓
Commitment → RESOLVED
```

The phases are advisory — the runtime does not strictly enforce phase ordering beyond basic structural rules. However, the projection tracks phase transitions for your orchestrator logic.

## Authorization & termination

Per-message authorization (who can send Proposal/Evaluation/Vote/Commitment) and the runtime's commitment-readiness checks are defined in [Runtime Modes § Decision Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#decision-mode). Additional governance rules — vote quorum, confidence thresholds, veto — come from policies bound at SessionStart; see [Runtime Policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md).

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient, DecisionSession

client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,  # local dev; production uses TLS by default
    auth=AuthConfig.for_dev_agent("coordinator"),
)

session = DecisionSession(client)
session.start(
    intent="pick a deployment plan",
    participants=["coordinator", "alice", "bob"],
    ttl_ms=60_000,
)

# Propose options
session.propose("p1", "deploy v2.1", rationale="tests passed, low risk")
session.propose("p2", "deploy v3.0-beta", rationale="new features ready")

# Evaluations
session.evaluate("p1", "APPROVE", confidence=0.9, reason="stable release", sender="alice")
session.evaluate("p2", "REVIEW", confidence=0.6, reason="needs more testing", sender="alice")

# Objections
session.raise_objection("p2", reason="beta not validated in staging", severity="high", sender="bob")

# Votes
session.vote("p1", "approve", reason="safe choice", sender="alice")
session.vote("p1", "approve", reason="agreed", sender="bob")

# Check projection and commit
proj = session.decision_projection
winner = proj.majority_winner()
if winner and not proj.has_blocking_objection(winner):
    session.commit(
        action="deployment.approved",
        authority_scope="release-management",
        reason=f"winner={winner}, votes={proj.vote_totals()}",
    )
```

## Projection queries

The `DecisionProjection` tracks all proposals, evaluations, objections, and votes locally:

```python
proj = session.decision_projection

# Proposals
proj.proposals                    # dict[str, DecisionProposalRecord]
proj.proposals["p1"].option       # "deploy v2.1"

# Evaluations
proj.evaluations                  # list[DecisionEvaluationRecord]

# Objections
proj.objections                   # list[DecisionObjectionRecord]
proj.has_blocking_objection("p1") # True if severity in {high, critical, block}

# Votes
proj.votes                        # dict[proposal_id, dict[sender, DecisionVoteRecord]]
proj.vote_totals()                # {"p1": 2, "p2": 0}
proj.majority_winner()            # "p1" (most positive votes)

# Lifecycle
proj.phase                        # "Proposal" | "Evaluation" | "Voting" | "Committed"
proj.is_committed                 # True after Commitment accepted
proj.commitment                   # CommitmentPayload or None
proj.transcript                   # list[Envelope] — full ordered history
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` on Proposal | Sender not a declared participant | Verify sender is in participants list |
| `FORBIDDEN` on Commitment | Sender is not the session initiator | Only the coordinator should commit |
| `SESSION_NOT_OPEN` on Vote | Session already resolved/expired | Check session state before voting |
| `DUPLICATE_MESSAGE` | Same message_id sent twice | Safe to ignore (idempotent) |

## Real-world scenario: AI agent consensus

Three AI agents evaluate a security incident and decide on a response:

```python
session = DecisionSession(client, auth=coordinator_auth)
session.start(
    intent="respond to security alert SEC-2025-0042",
    participants=["coordinator", "threat-analyzer", "impact-assessor", "response-planner"],
    ttl_ms=300_000,  # 5 minutes
)

# Each agent proposes a response
session.propose("p1", "isolate affected hosts", rationale="contain lateral movement", sender="threat-analyzer")
session.propose("p2", "patch and monitor", rationale="known CVE, patch available", sender="response-planner")

# Agents evaluate each other's proposals
session.evaluate("p1", "APPROVE", confidence=0.85, reason="stops spread", sender="impact-assessor")
session.evaluate("p2", "BLOCK", confidence=0.3, reason="too slow for active exploit", sender="threat-analyzer")

# Agents vote
session.vote("p1", "approve", sender="threat-analyzer")
session.vote("p1", "approve", sender="impact-assessor")
session.vote("p1", "approve", sender="response-planner")

# Commit the consensus
session.commit(
    action="incident.response.selected",
    authority_scope="security-operations",
    reason="unanimous: isolate affected hosts",
)
```

## API Reference

::: macp_sdk.decision.DecisionSession

::: macp_sdk.projections.DecisionProjection
