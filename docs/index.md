# MACP Python SDK

Python SDK for the [Multi-Agent Coordination Protocol](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol) (MACP) Rust runtime.

## What is MACP?

MACP is a coordination kernel for autonomous agent ecosystems. It defines one strict invariant: **binding, convergent coordination MUST occur inside explicit, bounded Coordination Sessions**. Ambient interaction remains continuous and non-binding through Signals.

This separation keeps coordination explicit, bounded, auditable, and replayable.

## What this SDK does

- Connects to the MACP Rust runtime over gRPC
- Provides **typed session helpers** for all 5 standard coordination modes
- Maintains **in-process projections** for local state tracking (because `GetSession` returns metadata only)
- Offers **envelope builders** for low-level message construction
- Includes **retry helpers** with exponential backoff
- Supports **bidirectional streaming** with mid-session replay (`send_subscribe`)
- Exposes **session discovery** — `list_sessions()` + `SessionLifecycleWatcher`
- Ships an **agent framework** — `Participant`, `from_bootstrap`, handler dispatch, strategies, and cancel-callback wiring
- Provides **policy registration** and `PolicyWatcher` for governance updates

## What this SDK does NOT do

This is a **thin typed client library**. It does not contain:

- Decision policies or voting rules
- AI heuristics or LLM integration
- Application-specific business logic
- Workflow orchestration

Those belong in the **orchestrator/agent layer** above the SDK. See [Building Orchestrators](guides/building-orchestrators.md).

## Install

```bash
pip install macp-sdk-python
```

For development:

```bash
git clone https://github.com/multiagentcoordinationprotocol/macp-sdk-python
cd macp-sdk-python
make setup   # pip install -e ".[dev,docs]"
```

## Quick start

```python
from macp_sdk import AuthConfig, MacpClient, DecisionSession

client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,  # local dev only — TLS is the default in production
    auth=AuthConfig.for_dev_agent("coordinator"),
)

session = DecisionSession(client)
session.start(
    intent="pick a deployment plan",
    participants=["coordinator", "alice", "bob"],
    ttl_ms=60_000,
)
session.propose("p1", "deploy v2.1", rationale="tests passed")
session.evaluate("p1", "APPROVE", confidence=0.9, reason="low risk", sender="alice")
session.vote("p1", "approve", reason="ship it", sender="alice")
session.vote("p1", "approve", reason="agreed", sender="bob")

winner = session.decision_projection.majority_winner()
if winner and not session.decision_projection.has_blocking_objection(winner):
    session.commit(
        action="deployment.approved",
        authority_scope="release-management",
        reason=f"winner={winner}",
    )
```

## Supported modes

| Mode | Session Helper | Projection | Participant Model | Determinism |
|------|---------------|------------|-------------------|-------------|
| [Decision](modes/decision.md) | `DecisionSession` | `DecisionProjection` | declared | semantic-deterministic |
| [Proposal](modes/proposal.md) | `ProposalSession` | `ProposalProjection` | peer | semantic-deterministic |
| [Task](modes/task.md) | `TaskSession` | `TaskProjection` | orchestrated | structural-only |
| [Handoff](modes/handoff.md) | `HandoffSession` | `HandoffProjection` | delegated | context-frozen |
| [Quorum](modes/quorum.md) | `QuorumSession` | `QuorumProjection` | quorum | semantic-deterministic |

## Documentation guide

| Topic | What you'll learn |
|-------|-------------------|
| [Core Protocol](protocol.md) | Envelopes, sessions, lifecycle, errors, discovery |
| [Architecture](architecture.md) | Module map, BaseSession pattern, projections, data flow |
| [Determinism & Replay](determinism.md) | Determinism classes, version binding, replay testing |
| [Security](security.md) | TLS defaults, `expected_sender` guardrail, retry on rate limits |
| [Error Handling](guides/error-handling.md) | Exception hierarchy, retry patterns, graceful degradation |
| [Streaming](guides/streaming.md) | `MacpStream`, `send_subscribe` replay, registry/root/policy/signal watchers |
| [Session Discovery](guides/session-discovery.md) | `list_sessions`, `SessionLifecycleWatcher`, supervisor patterns |
| [Agent Framework](guides/agent-framework.md) | `Participant`, `from_bootstrap`, dispatcher, strategies, cancel-callback |
| [Direct Agent Auth](guides/direct-agent-auth.md) | Initiator + non-initiator bootstrap, `expected_sender` guardrail |
| [Building Orchestrators](guides/building-orchestrators.md) | Policy patterns, multi-stage pipelines, event-driven |
| [Auth](auth.md) | AuthConfig setup for dev and production |
| [API Reference](api/index.md) | Auto-generated class and function docs |

## Prerequisites

- Python 3.11+
- A running MACP Rust runtime (see [runtime repo](https://github.com/multiagentcoordinationprotocol/macp-runtime))

For local development:

```bash
# In the runtime repo:
export MACP_ALLOW_INSECURE=1
cargo run
```

## Related documentation

This SDK is the gRPC client; the runtime is the source of truth for protocol semantics, RPC contracts, deployment, and auth. Rather than duplicate runtime material, we link to it:

- [Runtime — Getting Started](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/getting-started.md) — build the runtime, static/JWT auth configuration, first session
- [Runtime — API Reference](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md) — all 22 gRPC RPCs with request/response fields and capability flags
- [Runtime — Architecture](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/architecture.md) — layer structure, request flow, durability model
- [Runtime — Modes](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md) — per-mode state machine implementation details
- [Runtime — Policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md) — policy framework, rule schemas, evaluator internals
- [Runtime — Deployment](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md) — production config, storage backends, TLS, crash recovery
- [Runtime — SDK Developer Guide](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md) — envelope construction, streaming, passive subscribe, retries
- [Protocol Specification](https://www.multiagentcoordinationprotocol.io/docs) — two-plane model, session lifecycle, determinism, security, transport bindings
