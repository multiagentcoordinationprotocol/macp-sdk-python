# Determinism and Replay

MACP defines formal determinism guarantees that enable session replay, audit verification, and distributed state reconstruction. The SDK relies on those guarantees but does not define them — they are protocol-level. This page focuses on what an SDK user needs to do (version binding, replay testing) and links out for the rest.

- Protocol spec: [Determinism](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/determinism.md)
- Per-mode determinism class: [Runtime Modes](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md)
- Replay enforcement: [Runtime Architecture § Durability model](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/architecture.md#durability-model)

## Determinism classes — at a glance

Each mode declares a determinism class. The SDK does not enforce these — the runtime does — but they govern what you can assume on replay:

| Class | Modes | Replay guarantee |
|-------|-------|------------------|
| `semantic-deterministic` | Decision, Proposal, Quorum | same envelopes → same semantic outcome |
| `structural-only` | Task | same envelopes → same lifecycle, outcomes may differ |
| `context-frozen` | Handoff | same envelopes + same frozen context → same outcome |
| `non-deterministic` | (extension modes) | structural transitions only |

Full definitions: [Protocol Determinism](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/determinism.md).

## Version binding (SDK responsibility)

Three versions are bound at `SessionStart` and cannot change for the life of the session. The SDK passes them through verbatim — pin them explicitly when the session is part of an audit trail:

```python
session = DecisionSession(
    client,
    mode_version="1.0.0",
    configuration_version="org-2025.q4",
    policy_version="procurement-v3",
)
```

Different versions produce different sessions; replay uses the bound versions, not whatever is current.

## External side effects

When a mode triggers actions outside the session (deployments, payments, emails), use the established protocol patterns to keep replay meaningful — *plan-then-execute*, *log external results*, or *idempotent external transactions*. See [Protocol Determinism § External side effects](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/determinism.md) for the canonical guidance.

The SDK supports the *plan-then-execute* pattern naturally — the Commitment carries the plan and an idempotency key:

```python
session.commit(
    action="deployment.approved",
    authority_scope="release",
    reason="approved with idempotency_key=deploy-v2.1-20250329",
)
```

## Testing determinism in Python

Verify replay correctness by recording the transcript and feeding it through a fresh projection:

```python
# Record during a live session
transcript = session.projection.transcript

# Replay against a new projection
from macp_sdk.projections import DecisionProjection
replay = DecisionProjection()
for envelope in transcript:
    replay.apply_envelope(envelope)

assert replay.majority_winner() == original_winner
assert replay.is_committed == original_committed
```

For `semantic-deterministic` modes, the replayed projection must match the original. Build this into your test suite for any orchestrator that depends on replay.
