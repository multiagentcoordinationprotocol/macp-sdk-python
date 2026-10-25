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

Requires Python **>= 3.11** (`requires-python` in `pyproject.toml`); CI runs the unit
suite on Python 3.11, 3.12 and 3.13.

## Quick start

### 1. Start a local runtime

```bash
docker run -p 50051:50051 -e MACP_ALLOW_INSECURE=1 -e MACP_MEMORY_ONLY=1 ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0
```

This is the runtime image CI pins. `MACP_ALLOW_INSECURE=1` permits plaintext and
dev-agent auth (local development only); `MACP_MEMORY_ONLY=1` skips on-disk
persistence. Since runtime **v0.5.0**, a runtime with no authentication configured
(`MACP_AUTH_TOKENS_*` / `MACP_AUTH_ISSUER` unset) **refuses to start** unless
`MACP_ALLOW_INSECURE=1` is set, and the image does not bake it in.

### 2. Connect

#### Local dev (the runtime above)

```python
from macp_sdk import AuthConfig, DecisionSession, MacpClient

client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,                                # dev only
    auth=AuthConfig.for_dev_agent("coordinator"),
)
```

Dev-agent auth rides the standard `Authorization: Bearer` header — the
runtime's `dev_authenticate` fallback binds the token value verbatim
as the sender.

#### Production (Bearer + TLS)

```python
from macp_sdk import AuthConfig, DecisionSession, MacpClient

# TLS is the default; Bearer + expected_sender binds the session to this identity.
client = MacpClient(
    target="runtime.example.com:50051",
    auth=AuthConfig.for_bearer("tok-coord", expected_sender="coordinator"),
)
```

### 3. Drive a decision

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

### 4. Async (asyncio)

`AsyncMacpClient` is a `grpc.aio` client with the same methods, auth and errors as
`MacpClient`. RPCs and session actions are coroutines, `watch_*` RPCs are async
iterators, and every mode has an `Async*Session` helper. Here is the
quickstart session, async (`examples/async_first_session.py`):

```python
import asyncio

from macp.v1.envelope_pb2 import SessionState

from macp_sdk import AsyncDecisionSession, AsyncMacpClient, AuthConfig


async def main() -> None:
    async with AsyncMacpClient(
        target="127.0.0.1:50051",
        allow_insecure=True,  # local dev only
        auth=AuthConfig.for_dev_agent("coordinator"),
    ) as client:
        session = AsyncDecisionSession(client)
        await session.start(
            intent="pick a deployment plan",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=60_000,
        )
        await session.propose("p1", "deploy v2.1", rationale="tests passed")
        await session.evaluate(
            "p1", "APPROVE", confidence=0.94, reason="low risk",
            sender="alice", auth=AuthConfig.for_dev_agent("alice"),
        )
        await session.vote(
            "p1", "APPROVE", reason="ship it",
            sender="bob", auth=AuthConfig.for_dev_agent("bob"),
        )

        winner = session.decision_projection.majority_winner()
        await session.commit(
            action="deployment.approved",
            authority_scope="release-management",
            reason=f"winner={winner}",
        )

        meta = (await session.metadata()).metadata
        print("winner:", winner)
        print("state:", SessionState.Name(meta.state), "mode:", meta.mode)


asyncio.run(main())
```

```
winner: p1
state: SESSION_STATE_RESOLVED mode: macp.mode.decision.v1
```

Create the client inside the running loop: `grpc.aio` channels bind to the loop
that is current when they are created. See
[docs/guides/async.md](docs/guides/async.md) for streaming, watches and concurrency.

## Decision receipts

A decision receipt (`macp-receipt/1`, RFC-MACP-0013 Appendix A) is a portable
JSON record of one Commitment that anyone can check offline: *integrity* (the
recorded commitment has the recorded `commitment_hash`) needs no keys, and
*authenticity* comes from an Ed25519 JWS attestation by a key the verifier
trusts. Signing and signature checks need the `receipts` extra:

```bash
pip install 'macp-sdk-python[receipts]'
```

```python
from macp_sdk import ReceiptSigner, build_receipt, verify_receipt

receipt = build_receipt(
    commitment,                      # the session's CommitmentPayload
    session_id=session.session_id,
    mode="macp.mode.decision.v1",
    issuer="runtime:example",
    issued_at="2026-10-24T12:00:00Z",
    semantics_rev={"implementation": "macp-runtime", "rev": 4},
    signer=ReceiptSigner("example-1", private_key=seed),  # 32-byte seed, or sign=callback
)

result = verify_receipt(receipt, {"example-1": public_key_x})  # kid -> JWK x (base64url)
# {"integrity": "match", "attestations": ["valid"], "verdict": "attested"}
```

`verify_receipt` never raises on a malformed receipt; it returns one of the
verdicts `attested`, `integrity_only`, `unverified` or `refused` (unknown
`receipt_format`), in the same shape as the spec's vectors, which the unit tests
replay from `tests/vectors/receipt/`. Pass `commitment_wire=` (the payload's
Protobuf bytes) to record `commitment_wire_b64`; `build_receipt` raises if it is
not the wire encoding of `commitment`. `commitment_from_projection()` turns a
receipt's snake_case `commitment` back into a `CommitmentPayload`.

## Framework integrations

Separate packages, each with its own version and release tag, living under `integrations/`:

| Package | Install | What it adds |
|---|---|---|
| [`langgraph-macp`](integrations/langgraph/) | `pip install langgraph-macp` | `MacpConsensusNode`: a LangGraph node that settles a Decision or Quorum session and writes the Commitment to graph state |
| [`claude-agent-sdk-macp`](integrations/claude-agent-sdk/) | `pip install claude-agent-sdk-macp` | `macp_consensus` and the `mcp__macp__consensus` tool: Claude Agent SDK subagents evaluate and vote in a Decision or Quorum session, and you get the Commitment back |

An OpenAI Agents SDK adapter (handoffs mapped to Handoff mode) is next.

## Supported modes

| Mode | Session Helper | Projection | Example |
|------|---------------|------------|---------|
| Decision | `DecisionSession` | `DecisionProjection` | `examples/decision_smoke.py` |
| Proposal | `ProposalSession` | `ProposalProjection` | `examples/proposal_negotiation.py` |
| Task | `TaskSession` | `TaskProjection` | `examples/task_delegation.py` |
| Handoff | `HandoffSession` | `HandoffProjection` | `examples/handoff_escalation.py` |
| Quorum | `QuorumSession` | `QuorumProjection` | `examples/quorum_approval.py` |
| Multi-round (`ext.multi_round.v1`, extension) | — (codec only) | — | — |

Each standard-mode helper has an asyncio twin (`AsyncDecisionSession`,
`AsyncProposalSession`, `AsyncTaskSession`, `AsyncHandoffSession`,
`AsyncQuorumSession`) for use with `AsyncMacpClient`.

Multi-round support is codec-only: the `MODE_MULTI_ROUND` constant,
`build_contribute_payload()`, and `ProtoRegistry` encode/decode of `Contribute`
(canonical protobuf, with legacy JSON still decoded). There is no session helper or
projection — build and send its envelopes with the low-level `MacpClient` API.

## Development

```bash
# Setup
make setup              # pip install -e ".[dev,docs]"

# Quality
make lint               # ruff check + ruff format --check
make fmt                # ruff format
make typecheck          # mypy strict
make test               # unit tests + coverage gate (85% branch, from pyproject)
make test-integration   # integration tests (auto-skip without a running runtime)
make test-all           # lint + typecheck + all tests
make coverage           # HTML + terminal coverage report

# Build
make build              # build sdist + wheel, then twine check

# Proto definitions (provided by macp-proto package)
make dev-link-protos    # link local proto package for development
```

`make test-integration-docker` starts the same pinned runtime image as the quick start,
runs `tests/integration/`, and cleans up.

## Documentation

Full docs available in `docs/` — build with `mkdocs serve` after `make setup`.

## Runtime compatibility

| SDK | Runtime | macp-proto |
|---|---|---|
| 0.17.x | 0.9.x (`semantics_rev` 4) | 0.1.11 |

CI runs `tests/integration/` against the pinned runtime image on every PR
(`make test-integration-docker` runs the same suite locally). Runtimes older than 0.9.0 are
best-effort: the wire stays compatible, but the mode helpers model the 0.9.0 rules
(task rule 2a, handoff rule 3a, quorum threshold overrides, Decision casing), so a session
on an older runtime may diverge from what the projections predict.

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
