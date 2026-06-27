# Direct-agent authentication (SDK ≥ 0.2.0)

From SDK 0.2.0, agents are expected to authenticate to the MACP runtime
**directly** with their own Bearer identity. The orchestrator /
control-plane no longer forges envelopes on behalf of agents. This matches
RFC-MACP-0004 §4 ("`sender` MUST be derived from authenticated identity")
and the architectural invariants spelled out in
`ui-console/plans/direct-agent-auth.md`.

For the runtime side of identity binding (token validators, sender derivation, dev-mode fallback), see [Runtime API § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#authentication) and [Runtime Deployment § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md#authentication).

This guide shows the initiator and non-initiator patterns.

## Bootstrap from the orchestrator

Your orchestrator (examples-service, scenario compiler, CLI — whatever
produces a run) should pre-allocate a `session_id` and hand each agent
a bootstrap document that includes:

- `runtime.address` — gRPC endpoint (e.g. `runtime.example.com:50051`)
- `runtime.bearerToken` — per-agent Bearer token
- `runtime.tls` — `true` in production; `false` only for local dev
- `run.sessionId` — UUID v4 pre-allocated by the orchestrator
- `participant.participantId` — this agent's bare sender identity
- `initiator.sessionStartPayload` + `initiator.kickoff` — **only on the
  initiator agent's bootstrap**

The `session_id` must satisfy the runtime validator (UUID v4/v7 or
base64url ≥22 chars). Use `macp_sdk.new_session_id()` to generate one.

## Initiator agent

The initiator owns `SessionStart`. It is the agent whose identity the
runtime records as `session.initiator`, and the only participant
authorised to commit (unless policy delegates otherwise).

```python
from macp_sdk import (
    AuthConfig,
    DecisionSession,
    MacpClient,
    new_session_id,
)

# Pulled from bootstrap JSON
runtime_address = bootstrap["runtime"]["address"]
bearer_token = bootstrap["runtime"]["bearerToken"]
use_tls = bootstrap["runtime"]["tls"]
participant_id = bootstrap["participant"]["participantId"]
session_id = bootstrap["run"]["sessionId"]
start_payload = bootstrap["initiator"]["sessionStartPayload"]

auth = AuthConfig.for_bearer(bearer_token, expected_sender=participant_id)

client = MacpClient(
    target=runtime_address,
    # secure=True is default; local dev:
    allow_insecure=not use_tls,
    auth=auth,
)
client.initialize()

session = DecisionSession(client, session_id=session_id, auth=auth)

# 1. Unary Send(SessionStart) — the runtime binds session.initiator
#    to this agent's Bearer identity.
session.start(
    intent=start_payload["intent"],
    participants=start_payload["participants"],
    ttl_ms=start_payload["ttlMs"],
)

# 2. Open the bidi stream for subsequent events.
stream = session.open_stream()

# 3. Emit the kickoff envelope (the first mode-specific message).
session.propose("p1", "deploy-v1", rationale="canary checks passed")

# … run the event loop on stream.responses() …
```

## Non-initiator agent

Non-initiators never call `.start()`. They open a stream on a session
that may or may not yet exist; the runtime delivers SessionStart as the
first envelope once the initiator emits it.

```python
auth = AuthConfig.for_bearer(bearer_token, expected_sender=participant_id)
client = MacpClient(target=runtime_address, allow_insecure=not use_tls, auth=auth)
client.initialize()

session = DecisionSession(client, session_id=session_id, auth=auth)
stream = session.open_stream()

for envelope in stream.responses():
    if envelope.message_type == "Proposal":
        session.evaluate(
            proposal_id=..., recommendation="APPROVE", confidence=0.9
        )
    elif envelope.message_type == "Vote":
        # ... aggregate and possibly emit session.vote() ...
        ...
```

## Why `expected_sender` matters

The runtime already derives the envelope `sender` from the authenticated
identity — a spoofed `sender=` fails at the runtime with
`UNAUTHENTICATED`. Setting `expected_sender` on the `AuthConfig` lets the
SDK catch the mistake locally and raise `MacpIdentityMismatchError`
**before** the envelope hits the wire. Clearer traceback, no wasted RTT,
and no ambiguity about whose identity the session was bound to.

```python
auth = AuthConfig.for_bearer("tok-alice", expected_sender="alice")
session = DecisionSession(client, auth=auth)
session.vote("p1", "APPROVE", sender="mallory")
# ↑ MacpIdentityMismatchError(expected="alice", actual="mallory")
```

## Cancellation

Cancellation authority stays with the initiator (RFC-MACP-0001 §7.2)
unless a policy's `commitment.authority` delegates it. Two patterns:

- **Option A (default):** the initiator agent exposes a local HTTP
  `POST /agent/cancel` endpoint. The orchestrator calls it, and the agent
  invokes `session.cancel(reason=...)` over its own gRPC channel. Since
  SDK 0.3.0 this is **wired automatically by `from_bootstrap`** — see
  [Agent Framework → Cancel callback](agent-framework.md#cancel-callback).
  If you hand-roll the agent outside the framework you can still stand up
  the endpoint yourself with
  `macp_sdk.agent.start_cancel_callback_server(...)`.
- **Option B (opt-in):** the scenario's policy designates the orchestrator
  as a commitment authority; it can then call `CancelSession` directly.

Either way, the SDK's `session.cancel()` call carries the agent's Bearer
identity, so the runtime enforces authority consistently.
