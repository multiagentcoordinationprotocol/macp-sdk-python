# Building Orchestrators

The SDK provides typed action builders and state projections. **Policy logic** — voting rules, decision strategies, AI heuristics — belongs in the **orchestrator layer** above the SDK. (Note: the runtime also exposes a *governance* policy engine that evaluates declarative rules at commitment time — see [Runtime Policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md). Use it to enforce hard constraints like quorum thresholds or veto rights; keep dynamic strategy logic in your orchestrator.)

## Architecture reminder

```
Runtime API (Rust)  — enforces protocol, transitions, replay
       ↑
Language SDK         — typed models, action builders, projections   ← you use this
       ↑
Orchestrator         — your decision logic, policies, strategies    ← you build this
       ↑
Application          — your product/service
```

## Pattern: Policy-driven decision orchestrator

```python
from macp_sdk import AuthConfig, MacpClient, DecisionSession


def run_decision(client, intent, participants, proposals):
    """Orchestrator that runs a majority-vote decision session."""
    auth = AuthConfig.for_dev_agent("orchestrator")
    session = DecisionSession(client, auth=auth)
    session.start(intent=intent, participants=participants, ttl_ms=120_000)

    # Submit proposals
    for pid, option, rationale in proposals:
        session.propose(pid, option, rationale=rationale)

    # Collect votes (in a real system, agents vote asynchronously)
    for participant in participants:
        if participant == "orchestrator":
            continue
        # Your policy logic: ask each agent to vote
        vote = ask_agent_for_vote(participant, session.session_id)
        session.vote(vote.proposal_id, vote.choice, sender=participant)

    # Apply your policy: majority wins
    proj = session.decision_projection
    winner = proj.majority_winner()

    if winner and not proj.has_blocking_objection(winner):
        session.commit(
            action="approved",
            authority_scope="my-domain",
            reason=f"Majority selected {winner}",
        )
        return {"status": "resolved", "winner": winner}
    else:
        session.cancel(reason="No majority or blocking objection")
        return {"status": "cancelled"}
```

## Pattern: Multi-stage pipeline

Combine multiple modes in sequence:

```python
def deployment_pipeline(client):
    """
    1. Decision: pick which version to deploy
    2. Quorum: get approval from required reviewers
    3. Task: delegate the deployment to a worker
    """
    # Stage 1: Decision
    decision = DecisionSession(client, auth=coordinator_auth)
    decision.start(intent="pick version", participants=["a", "b", "c"], ttl_ms=60_000)
    # ... proposals, votes, commit ...
    winner = decision.decision_projection.majority_winner()

    # Stage 2: Quorum approval
    quorum = QuorumSession(client, auth=coordinator_auth)
    quorum.start(intent=f"approve deploy of {winner}", participants=["r1", "r2", "r3"], ttl_ms=60_000)
    quorum.request_approval("req-1", "deploy", summary=f"Deploy {winner}", required_approvals=2)
    # ... collect approvals ...

    # Stage 3: Task delegation
    task = TaskSession(client, auth=coordinator_auth)
    task.start(intent=f"deploy {winner}", participants=["coordinator", "deploy-agent"], ttl_ms=300_000)
    task.request_task("t1", f"Deploy {winner}", instructions="...", requested_assignee="deploy-agent")
    # ... wait for completion ...
```

## Pattern: Supervisor / observer

Use `list_sessions()` + `SessionLifecycleWatcher` to build a supervisor that
tracks every session a tenant/agent can see — no need to pre-register session
ids or poll `GetSession`:

```python
from macp_sdk import MacpClient, AuthConfig, SessionLifecycleWatcher

supervisor = MacpClient(
    target="runtime:50051",
    auth=AuthConfig.for_bearer("tok-supervisor", expected_sender="supervisor"),
)
supervisor.initialize()

# Snapshot on startup
for meta in supervisor.list_sessions():
    print("seen", meta.session_id, meta.mode, meta.state)

# React to live events
for ev in SessionLifecycleWatcher(supervisor).changes():
    if ev.is_created:
        spawn_monitor(ev.session.session_id)
    elif ev.is_terminal:
        reconcile(ev.session.session_id, ev.event_type)
```

The runtime emits an initial `CREATED` event for each already-open session at
subscribe time, so the watcher is safe to (re)start at any point — you won't
miss live sessions.

## Pattern: Event-driven orchestrator

Use streaming to react to accepted envelopes in real time:

```python
stream = client.open_stream(auth=coordinator_auth)

for envelope in stream.responses(timeout=300.0):
    if envelope.message_type == "Vote":
        # Check if we have enough votes to commit
        session.projection.apply_envelope(envelope)
        proj = session.decision_projection
        if proj.majority_winner():
            session.commit(...)
            break
    elif envelope.message_type == "Commitment":
        break
```

## What NOT to put in the SDK

These belong in your orchestrator, not in the SDK:

- **Voting rules**: "2/3 majority required" → orchestrator policy
- **AI decision heuristics**: "use GPT-4 to evaluate proposals" → orchestrator logic
- **Timeout strategies**: "wait 30s for votes, then commit with what we have" → orchestrator timing
- **Escalation logic**: "if no quorum in 5min, escalate to manager" → orchestrator workflow
- **Notification logic**: "email stakeholders when committed" → orchestrator side-effects

The SDK's projections give you the **facts** (vote counts, proposal states, ballot tallies). Your orchestrator decides **what to do** with those facts.
