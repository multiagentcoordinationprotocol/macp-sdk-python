# Architecture

This page describes the SDK's internal structure. For the runtime side of the stack (kernel layers, durability, concurrency, request flow), see [Runtime Architecture](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/architecture.md).

## Three-layer model

```
Runtime (Rust)     — protocol enforcement, transitions, replay, persistence
       ↑
Language SDK       — typed state models, action builders, session helpers
       ↑
Orchestrator       — decision strategies, voting rules, AI heuristics
```

The SDK is the middle layer: it provides typed convenience APIs for building and sending MACP envelopes, plus local state projections. It does **not** enforce business policy.

!!! info "What belongs where"
    - **SDK**: `session.vote("p1", "approve")` — builds envelope, sends to runtime, tracks locally
    - **Orchestrator**: "commit if >50% approved and no blocking objections" — your logic, your policy
    - **Runtime**: validates the Commitment, transitions session to RESOLVED — protocol enforcement

### Action-method signature conventions

Every mode-helper action method follows the same call shape:

```
session.<action>(<required_ids>, *, <payload_kwargs>, sender=None, auth=None)
```

- **Required IDs are positional** — e.g. `session.vote(proposal_id, vote)`, `session.accept_task(task_id)`. These are the minimum required to identify the subject of the action.
- **Everything else is keyword-only** after the `*` separator — payload fields (`reason`, `confidence`, `summary`, …), `sender`, and the per-call `auth` override.
- **`sender=` and `auth=` are always the last two kwargs.** The SDK resolves the effective sender via `method > session > client` precedence and enforces the `expected_sender` guardrail from §Auth.

This rule matches how `DecisionSession.vote`, `ProposalSession.propose`, `TaskSession.accept_task`, `HandoffSession.offer`, and `QuorumSession.approve` are shaped today. New helpers must follow the same pattern so call sites read predictably.

## Module map

```
macp_sdk/
├── client.py          MacpClient (sync gRPC) + MacpStream (bidirectional streaming, send_subscribe replay)
├── auth.py            AuthConfig — bearer-token auth (for_bearer / for_dev_agent), expected_sender guardrail
├── base_session.py    BaseSession ABC — shared start/commit/cancel/metadata/open_stream
├── base_projection.py BaseProjection ABC — shared transcript/commitment tracking
├── decision.py        DecisionSession — propose, evaluate, object, vote
├── proposal.py        ProposalSession + ProposalProjection
├── task.py            TaskSession + TaskProjection
├── handoff.py         HandoffSession + HandoffProjection
├── quorum.py          QuorumSession + QuorumProjection
├── projections.py     DecisionProjection
├── envelope.py        Low-level envelope/payload builders
├── watchers.py        Server-stream wrappers: ModeRegistryWatcher, RootsWatcher,
│                      PolicyWatcher, SessionLifecycleWatcher, SignalWatcher
├── policy.py          Typed policy builders for all 5 modes (build_decision_policy, …)
├── validation.py      Client-side input validation helpers
├── proto_registry.py  Proto registry helper for extension modes
├── errors.py          Exception hierarchy (MacpSdkError → MacpAckError, …)
├── constants.py       Mode URIs and version strings
├── retry.py           RetryPolicy + retry_send helper
├── _logging.py        SDK logger configuration
└── agent/             High-level agent framework
    ├── participant.py     Participant event loop, handler registration, InitiatorConfig
    ├── dispatcher.py      Dispatcher — message-type and phase-change dispatch
    ├── strategies.py      EvaluationStrategy / VotingStrategy / CommitmentStrategy protocols
    ├── runner.py          from_bootstrap() — factory from bootstrap JSON (+ cancel-callback wiring)
    ├── cancel_callback.py CancelCallbackServer — stdlib HTTP server (RFC-0001 §7.2 Option A)
    ├── transports.py      GrpcTransportAdapter (auto send_subscribe) + TransportAdapter protocol
    └── types.py           SessionInfo, IncomingMessage, HandlerContext, TerminalResult
```

## Agent framework (`macp_sdk.agent`)

On top of the low-level client/session layer, the SDK ships a high-level agent
framework that is the recommended way to write long-running participant agents.
See [Agent Framework](guides/agent-framework.md) for the full guide.

```
bootstrap.json → from_bootstrap() → Participant
                                      │
                                      ├── .on("Proposal", handler)          ← message-type dispatch
                                      ├── .on_phase_change("Voting", h)     ← projection phase hooks
                                      ├── .on_terminal(h)                   ← Committed / Cancelled / …
                                      ├── .run()                            ← blocking event loop
                                      │
                                      └── cancel_callback HTTP endpoint     ← runtime / orchestrator shutdown
```

The framework wires up auth, transport (with automatic `send_subscribe` replay),
the mode-specific projection, dispatcher, and cancel-callback — callers only
supply handlers. Composable strategies (`VotingStrategy`, `EvaluationStrategy`,
`CommitmentStrategy`) let you slot in policy without rewriting the event loop.

## BaseSession / BaseProjection pattern

All 5 mode session helpers inherit from `BaseSession`, which provides:

| Method | Purpose |
|--------|---------|
| `start()` | Send SessionStart with intent, participants, TTL, context |
| `commit()` | Send Commitment to resolve the session |
| `cancel()` | Cancel session via `CancelSession` RPC |
| `metadata()` | Query session metadata via `GetSession` RPC |
| `open_stream()` | Open bidirectional stream via `StreamSession` RPC |

Mode-specific subclasses add only their action methods (e.g., `propose()`, `vote()`).

Similarly, all projections inherit from `BaseProjection`, which handles:

- **Transcript tracking** — append-only list of accepted envelopes
- **Commitment detection** — recognizes Commitment messages and updates phase
- **Mode routing** — ignores envelopes from other modes

## Why projections exist

The runtime's `GetSession` RPC returns **metadata only** (state, TTL, versions) — not mode-specific state or transcript. The SDK therefore maintains local in-process projections that track every accepted envelope and derive mode state.

```python
# Send a message
session.vote("p1", "approve", sender="alice")
# ↑ On success, the projection is updated automatically

# Query the local projection
proj = session.decision_projection
proj.vote_totals()         # {"p1": 1}
proj.majority_winner()     # "p1"
```

### Projection lifecycle

1. Session helper sends an envelope via `client.send()`
2. Runtime validates and accepts (Ack with `ok=true`)
3. On success, `_send_and_track()` calls `projection.apply_envelope(envelope)`
4. Projection parses the payload and updates its local state
5. Orchestrator queries the projection for decision-making

### Important: projections are local

Projections only see envelopes **sent through this session helper instance**. If multiple SDK instances participate in the same session, each has its own partial view. For a complete view, use `StreamSession` — and call `MacpStream.send_subscribe(session_id)` right after opening, so the runtime replays accepted history (RFC-MACP-0006-A1) before switching to live broadcast. `GrpcTransportAdapter` does this automatically for agents built on the `Participant` framework.

## Client → Runtime interaction

```
┌─────────────┐          gRPC          ┌─────────────┐
│  MacpClient │ ───── Initialize ────→ │   Runtime    │
│             │ ───── Send ──────────→ │   (Rust)     │
│             │ ←──── Ack ───────────  │              │
│             │ ───── GetSession ───→  │              │
│             │ ───── ListSessions ─→  │              │
│             │ ───── CancelSession →  │              │
│             │ ←──→  StreamSession    │              │
│             │ ←───  WatchSessions    │              │
│             │ ───── ListModes ────→  │              │
│             │ ───── GetManifest ──→  │              │
│             │ ←───  WatchRegistry    │              │
│             │ ←───  WatchRoots       │              │
│             │ ←───  WatchPolicies    │              │
└─────────────┘                        └─────────────┘
```

All communication is **client-initiated**. The runtime never calls back into the SDK. If you need runtime-driven behavior, run a Python agent as a separate process that polls or streams from the runtime.

For how the runtime processes each RPC internally, see [Runtime Architecture § Request flow](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/architecture.md#request-flow-send-rpc).

## Data flow for a typical session

```
1. client.initialize()                    → negotiate capabilities
2. session.start(intent, participants)    → SessionStart envelope → Ack
3. session.propose("p1", "option-a")      → Proposal envelope → Ack → projection updated
4. session.vote("p1", "approve")          → Vote envelope → Ack → projection updated
5. session.decision_projection.majority_winner()  → query local state
6. session.commit(action="approved")      → Commitment envelope → Ack → session RESOLVED
```

Steps 3–5 repeat for as many messages as the session requires. The projection accumulates state incrementally.

## Extension modes

The runtime supports dynamic extension modes via `RegisterExtMode`, `UnregisterExtMode`, and `PromoteMode` RPCs — semantics, lifecycle, and promotion rules are defined in [Runtime Modes § Dynamic Extension Modes](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#dynamic-extension-modes). The SDK exposes them through these client methods:

```python
# Register a custom mode
from macp.v1 import core_pb2

descriptor = core_pb2.ModeDescriptor(
    mode="ext.my-custom.v1",
    mode_version="1.0.0",
    title="My Custom Mode",
    message_types=["CustomMessage", "CustomResponse"],
    terminal_message_types=["Commitment"],
)
client.register_ext_mode(descriptor, auth=admin_auth)

# Later: list registered extensions
ext_modes = client.list_ext_modes()

# Send messages using the low-level envelope builder
from macp_sdk.envelope import build_envelope, serialize_message
envelope = build_envelope(
    mode="ext.my-custom.v1",
    message_type="CustomMessage",
    session_id="...",
    payload=serialize_message(my_payload),
)
client.send(envelope, auth=auth)
```
