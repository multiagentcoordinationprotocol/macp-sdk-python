# Agent Framework

`macp_sdk.agent` is the recommended high-level API for writing long-running
MACP participant agents. It handles auth, transport, projection, handler
dispatch, and shutdown — callers only supply handlers (or plug in
strategies).

This guide walks through the agent-framework building blocks and shows how to
wire them together. For the lower-level escape hatch — building envelopes by
hand — see [Direct Agent Auth](direct-agent-auth.md). For the underlying RPCs
this framework drives, see the [Runtime SDK Guide](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md).

## Big picture

```
bootstrap.json ─┐
                │   from_bootstrap()
                │        │
                ▼        ▼
            ┌──────────────────────┐
            │     Participant      │─── .on(message_type, handler)
            │                      │─── .on_phase_change(phase, handler)
            │   ┌──────────────┐   │─── .on_terminal(handler)
            │   │  Dispatcher  │   │─── .run()   ← blocking event loop
            │   └──────────────┘   │
            │   mode projection    │
            │   cancel_callback    │── HTTP POST → participant.stop()
            └──────────────────────┘
                     │
                     ▼ gRPC (bidi stream, auto send_subscribe replay)
                  Runtime
```

## `from_bootstrap` — factory

A bootstrap JSON document is the handoff format between your orchestrator /
control-plane and a spawned agent process. `from_bootstrap` reads it and
returns a fully-configured `Participant`:

```python
from macp_sdk.agent import from_bootstrap

participant = from_bootstrap("bootstrap.json")
# or: from_bootstrap()  ← reads $MACP_BOOTSTRAP_PATH
```

### Bootstrap shape

```json
{
  "participant_id": "alice",
  "session_id": "018f9b85-...-v4",
  "mode": "macp.mode.decision.v1",

  "runtime_url": "runtime.example.com:50051",
  "secure": true,
  "allow_insecure": false,

  "auth": { "bearer_token": "tok-alice-secret" },

  "participants": ["coordinator", "alice", "bob"],
  "mode_version": "1.0.0",
  "configuration_version": "org-2026.q2",
  "policy_version": "procurement-v3",

  "initiator": {
    "session_start": {
      "intent": "pick deployment plan",
      "participants": ["coordinator", "alice", "bob"],
      "ttl_ms": 120000,
      "context_id": "ctx-deploy-42",
      "extensions": { "aitp.v1": "BASE64PROTOBYTES==" }
    },
    "kickoff": {
      "message_type": "Proposal",
      "payload": { "proposal_id": "p1", "option": "deploy-v2" }
    }
  },

  "cancel_callback": {
    "host": "127.0.0.1",
    "port": 8721,
    "path": "/agent/cancel"
  }
}
```

- **Non-initiator agents** omit the `initiator` block — they only subscribe
  and react.
- **Initiators** include `initiator.session_start` and optionally a
  `kickoff`. `from_bootstrap` emits `SessionStart` and the kickoff envelope
  before entering the event loop.
- **`auth.agent_id`** is accepted as a dev-auth shorthand (equivalent to
  `AuthConfig.for_dev_agent`). Production bootstraps must set
  `bearer_token`.
- **`extensions`** values are encoded as proto-JSON canonical base64 — the
  loader decodes back to `dict[str, bytes]` and threads them onto
  `SessionStart.extensions`.

## Handlers

Register handlers with a fluent API:

```python
from macp_sdk.agent import from_bootstrap

participant = from_bootstrap("bootstrap.json")

@participant.on("Proposal")
def on_proposal(msg, ctx):
    payload = msg.payload         # mode-specific proto message (decoded)
    ctx.actions.evaluate(payload.proposal_id, "APPROVE", confidence=0.9)

@participant.on_phase_change("Voting")
def on_voting(phase, ctx):
    ctx.log_fn("entering voting phase")

@participant.on_terminal
def on_done(result):
    print("terminal:", result.state, result.commitment)

participant.run()   # blocks until a terminal event fires or stop() is called
```

### Handler context

Every handler receives a `HandlerContext`:

| Field | Purpose |
|-------|---------|
| `ctx.participant` | this agent's id |
| `ctx.session` | `SessionInfo` with mode, versions, participants |
| `ctx.projection` | live mode-specific projection (may be `None` for extension modes) |
| `ctx.actions` | bound `ParticipantActions` — `evaluate`, `vote`, `propose`, `commit`, `cancel_session`, `send_envelope` |
| `ctx.log_fn` | SDK logger |

### Terminal dispatch

`on_terminal` fires when the projection enters one of
`{"Committed", "Accepted", "Declined", "Cancelled", "TerminalRejected"}`,
or when a `SessionCancel` envelope is received. After it fires the event
loop exits on the next iteration.

## Strategies (composable policy)

For common orchestration shapes, compose a strategy instead of hand-rolling
a handler. Every strategy is a small protocol with a matching helper that
wraps it into a dispatcher handler:

| Protocol | Helper | What it does |
|----------|--------|--------------|
| `EvaluationStrategy` | `evaluation_handler` / `function_evaluator` | Decide `APPROVE/REJECT/ABSTAIN` + confidence per proposal |
| `VotingStrategy` | `voting_handler` / `function_voter` | Decide when to vote and which proposal to vote for |
| `CommitmentStrategy` | `commitment_handler` / `function_committer` | Decide when the session is ready to commit and emit the Commitment |
| `majority_voter` / `majority_committer` | built-in | Canonical majority-vote implementations |

```python
from macp_sdk.agent import (
    from_bootstrap,
    evaluation_handler,
    majority_voter,
    majority_committer,
)

participant = from_bootstrap("bootstrap.json")

@participant.on("Proposal")
def evaluate(msg, ctx):
    return evaluation_handler(my_llm_strategy)(msg, ctx)

participant.on("Vote", majority_voter(threshold=0.5))
participant.on_phase_change("Voting", majority_committer(
    action="deployment.approved",
    authority_scope="release",
))

participant.run()
```

## Cancel callback

The `cancel_callback` field in the bootstrap turns on an RFC-0001 §7.2
Option A endpoint. `from_bootstrap` starts a stdlib `http.server` daemon
bound to `participant.stop()` — a `POST` with `{"runId": ..., "reason": ...}`
shuts the agent down cleanly.

```text
POST http://127.0.0.1:8721/agent/cancel
Content-Type: application/json

{"runId": "run-42", "reason": "operator aborted"}
→ 202 Accepted   {"ok": true}
```

The server is daemon-threaded and auto-closes when `participant.stop()`
fires (from any thread — including from the handler that fired it). No
dependencies beyond the standalone library.

### Using the cancel-callback outside `from_bootstrap`

If you're not using bootstrap JSON you can still stand up the endpoint
directly:

```python
from macp_sdk.agent import start_cancel_callback_server

def on_cancel(run_id: str, reason: str) -> None:
    participant.stop()

server = start_cancel_callback_server(
    host="127.0.0.1", port=0, path="/agent/cancel", on_cancel=on_cancel,
)
print("listening on", server.address)
...
server.close()   # explicit shutdown; also called by participant.stop()
```

## Initiator agents

An initiator bootstrap includes an `initiator.session_start` block.
`from_bootstrap` emits the `SessionStart` envelope and (if present) the
kickoff before the event loop starts — no code changes required on your
side.

```python
participant = from_bootstrap("bootstrap.initiator.json")
participant.run()
```

Under the hood this flows through `Participant._emit_initiator_envelopes()`,
which builds `SessionStartPayload` from `InitiatorConfig` (including
`extensions` and `context_id`).

## Transport

`from_bootstrap` wires a `GrpcTransportAdapter` onto the participant. The
adapter:

1. Opens the bidi stream via `session.open_stream()`.
2. Immediately sends a `send_subscribe(session_id)` frame
   (RFC-MACP-0006-A1) so late-joining agents replay accepted history
   before live broadcast.
3. Decodes every accepted envelope to an `IncomingMessage` and hands it
   to `Participant._process_envelope()`.

To use a different transport (HTTP polling, in-process test shim, etc.)
implement the `TransportAdapter` protocol and pass it via
`Participant(transport=..., ...)`. The framework stays identical.

## Related

- [Direct Agent Auth](direct-agent-auth.md) — low-level initiator / non-initiator wire pattern (no framework).
- [Session Discovery](session-discovery.md) — supervisor-side counterpart.
- [Architecture → Agent framework](../architecture.md#agent-framework-macp_sdkagent).
