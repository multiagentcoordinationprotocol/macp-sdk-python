# macp-sdk-python

Python SDK for the [MACP](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol) Rust runtime.

## What this package does

- Connects to the Rust runtime over gRPC
- Provides typed session helpers for all 5 standard coordination modes
- Maintains in-process projections for local state tracking
- Supports envelope builders, retry helpers, and structured logging

## Important runtime boundary

This SDK **calls** the Rust runtime. The runtime does not call into this Python package.
If you need runtime-driven business logic, run a Python agent/orchestrator as a separate process and let it communicate with the runtime over MACP transport.

## Install

```bash
pip install macp-sdk-python
```

## Quick start

### Production (Bearer + TLS)

```python
from macp_sdk import AuthConfig, DecisionSession, MacpClient

# TLS is the default; Bearer + expected_sender binds the session to this identity.
client = MacpClient(
    target="runtime.example.com:50051",
    auth=AuthConfig.for_bearer("tok-coord", expected_sender="coordinator"),
)
```

### Local dev (runtime started with `MACP_ALLOW_INSECURE=1`)

```python
client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,                                # dev only
    auth=AuthConfig.for_dev_agent("coordinator"),
)
```

### Driving a decision

```python
session = DecisionSession(client)
session.start(
    intent="pick a deployment plan",
    participants=["coordinator", "alice", "bob"],
    ttl_ms=60_000,
)
session.propose("p1", "deploy v2.1", rationale="tests passed")
session.evaluate(
    "p1", "approve", confidence=0.94, reason="low risk",
    sender="alice", auth=AuthConfig.for_dev_agent("alice"),
)
session.vote(
    "p1", "approve", reason="ship it",
    sender="bob", auth=AuthConfig.for_dev_agent("bob"),
)

winner = session.decision_projection.majority_winner()
if winner and not session.decision_projection.has_blocking_objection(winner):
    session.commit(
        action="deployment.approved",
        authority_scope="release-management",
        reason=f"winner={winner}",
    )
```

## Supported modes

| Mode | Session Helper | Projection | Example |
|------|---------------|------------|---------|
| Decision | `DecisionSession` | `DecisionProjection` | `examples/decision_smoke.py` |
| Proposal | `ProposalSession` | `ProposalProjection` | `examples/proposal_negotiation.py` |
| Task | `TaskSession` | `TaskProjection` | `examples/task_delegation.py` |
| Handoff | `HandoffSession` | `HandoffProjection` | `examples/handoff_escalation.py` |
| Quorum | `QuorumSession` | `QuorumProjection` | `examples/quorum_approval.py` |

## Development

```bash
# Setup
make setup              # pip install -e ".[dev,docs]"

# Quality
make lint               # ruff check
make fmt                # ruff format
make typecheck          # mypy strict
make test               # unit tests
make test-all           # lint + typecheck + all tests
make coverage           # coverage report

# Build
make build              # sdist + wheel

# Proto definitions (provided by macp-proto package)
make dev-link-protos    # link local proto package for development
```

For local development against the runtime:

```bash
export MACP_ALLOW_INSECURE=1
cargo run   # in the runtime repo
```

Dev-agent auth rides the standard `Authorization: Bearer` header — the
runtime's `dev_authenticate` fallback binds the token value verbatim
as the sender.

## Documentation

Full docs available in `docs/` — build with `mkdocs serve` after `make setup`.

## Architecture boundary

This SDK is a **thin typed client library**. It provides:
- Typed state models and action builders
- Session helpers (`propose()`, `vote()`, `commit()`)
- Local state projections (because `GetSession` returns metadata only)

Business logic — voting rules, AI decision heuristics, policy enforcement — belongs in the orchestrator/agent layer **above** the SDK.

## Known runtime limitations

- `GetSession` returns metadata only (not mode state/transcript) — hence the local projection pattern; use `StreamSession` + `send_subscribe` for the full transcript
- `StreamSession` is scoped to one session per stream; use `MacpStream.send_subscribe(session_id)` (RFC-MACP-0006-A1, since SDK 0.2.3 / `macp-proto 0.1.2`) to replay accepted history before live broadcast
- For cross-session observability, use `MacpClient.list_sessions()` and `SessionLifecycleWatcher` (SDK 0.3.0) to enumerate active sessions and stream `CREATED` / `RESOLVED` / `EXPIRED` / `CANCELLED` / `SUSPENDED` / `RESUMED` lifecycle events
- Pause and cancel sessions with `MacpClient.suspend_session()` / `resume_session()` / `cancel_session()` (SDK 0.4.0 / `macp-proto 0.1.3`). A suspended session is non-terminal and rejects messages until resumed; an accepted `cancel_session()` now terminates as `CANCELLED` (previously surfaced as `EXPIRED`)
- `ListRoots` currently returns an empty list and `WatchRoots` idles — the runtime does not yet populate roots
- Business policy (majority, quorum, veto) belongs in your orchestrator/policy layer
