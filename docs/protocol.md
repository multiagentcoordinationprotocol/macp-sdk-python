# Core Protocol Concepts

This page is a quick orientation for SDK users. It covers only what you need to read SDK code without surprises. The runtime docs are authoritative for protocol semantics — links are inline below.

- Protocol spec: [MACP RFCs](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol)
- Runtime overview: [Runtime README](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/README.md)
- RPC reference: [Runtime API](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md)
- SDK-author guide: [Runtime SDK Guide](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md)

## Two planes of communication

MACP separates all agent communication into two planes:

- **Ambient Plane (Signals)** — Continuous, non-binding informational messages. Signals do not create sessions, mutate state, or produce binding outcomes.
- **Coordination Plane (Sessions)** — Bounded, explicit, binding coordination. All coordination that produces binding outcomes must happen inside a session.

The core invariant: *binding coordination MUST occur inside explicit, bounded Coordination Sessions*.

## Envelopes

Every message is a canonical `Envelope`. The SDK builds envelopes for you via `build_envelope()` and the session helpers — you rarely construct one by hand.

For the wire-level field list and validation rules, see [Runtime SDK Guide § Building envelopes](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md#building-envelopes).

Two SDK-relevant invariants worth knowing:

- **Idempotency** — each envelope carries a unique `message_id`. The runtime deduplicates within a session, so retries with the same `message_id` are safe (Ack returns `duplicate=true`). This is what makes [`retry_send()`](api/index.md) safe.
- **Sender identity is runtime-derived** — the `sender` field is bound from your authenticated identity, never self-asserted. The SDK fills it in from `AuthConfig`; the runtime validates it. See [Authentication](auth.md).

## Session lifecycle

The state machine (`OPEN → RESOLVED | EXPIRED | CANCELLED`, with a non-terminal `OPEN ↔ SUSPENDED` pause — see the full event set at [§ Server-streaming (watchers)](#server-streaming-watchers) below), monotonic transitions, and terminal-message rules are defined and enforced by the runtime. See [Runtime API § Session Lifecycle](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#session-lifecycle) and [Runtime Architecture § Coordination Kernel](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/architecture.md#layers).

The SDK tracks the lifecycle locally via projections — see [Architecture § Why projections exist](architecture.md#why-projections-exist).

### SessionStart

Every session begins with a `SessionStart` envelope. The SDK exposes its fields directly on `BaseSession.start(...)`:

```python
session.start(
    intent="pick a deployment plan",
    participants=["coordinator", "alice", "bob"],
    ttl_ms=60_000,
    mode_version="1.0.0",
    configuration_version="org-2025.q4",
    policy_version="procurement-v3",      # optional
    context_id="release-2025-q4-deploy",   # optional
    extensions={"aitp": b"..."},           # optional opaque blobs
)
```

`mode_version`, `configuration_version`, and `policy_version` are **bound at SessionStart and cannot change** during the session — see [Determinism](determinism.md).

`context_id` and the *keys* of `extensions` are projected onto every `SessionMetadata` returned by `GetSession`/`ListSessions`/`WatchSessions` — values stay opaque. Use this for protocol-extension signalling without parsing payloads. See [Session Discovery](guides/session-discovery.md).

### Commitment

A Commitment is the terminal message that resolves a session:

```python
session.commit(
    action="deployment.approved",
    authority_scope="release-management",
    reason="2/3 majority approved",
)
```

Who is authorised to commit is governed by the runtime's policy engine — see [Runtime Policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md#commitment-authority). By default, only the session initiator can commit.

## Capabilities and initialization

Before any session work, call `Initialize` to negotiate capabilities:

```python
response = client.initialize()
# response.selected_protocol_version  → "1.0"
# response.capabilities               → what the runtime supports
# response.supported_modes            → available mode URIs
```

`initialize()` caches `response.capabilities` on `client.server_capabilities`.

Field-level details: [Runtime API § Protocol Handshake](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#protocol-handshake).

## Errors

The runtime returns structured errors with RFC-defined codes (`UNAUTHENTICATED`, `FORBIDDEN`, `SESSION_NOT_FOUND`, `SESSION_NOT_OPEN`, `DUPLICATE_MESSAGE`, `INVALID_ENVELOPE`, `MODE_NOT_SUPPORTED`, `PAYLOAD_TOO_LARGE`, `RATE_LIMITED`, `INTERNAL_ERROR`). The full table with HTTP status mappings lives in [Runtime API § Message Transport](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#message-transport) and [Runtime SDK Guide § Error handling](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md#error-handling).

The SDK maps them to Python exceptions:

```python
from macp_sdk import MacpAckError, MacpTransportError

try:
    session.vote("p1", "approve", sender="alice")
except MacpAckError as e:
    print(e.failure.code)     # "FORBIDDEN"
    print(e.failure.message)  # "sender not authorized"
except MacpTransportError as e:
    print(e)                  # gRPC transport failure
```

For retry behaviour and which codes are safe to retry, see [Error Handling](guides/error-handling.md) and `RetryPolicy` in the [API Reference](api/index.md).

## Discovery

The runtime exposes discovery RPCs the SDK wraps as sync methods (unary) or iterators (server-streaming). See [Runtime API § Discovery](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#discovery) and [§ Streaming Watches](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#streaming-watches) for the underlying RPCs.

### Unary

```python
modes      = client.list_modes()       # standard modes
manifest   = client.get_manifest()     # runtime/agent manifest
ext_modes  = client.list_ext_modes()   # registered extension modes
roots      = client.list_roots()
sessions   = client.list_sessions()    # SDK ≥ 0.3.0
policies   = client.list_policies()
```

### What-if replay (RFC-MACP-0012 §8.1)

`what_if_replay` replays a session's accepted history under a candidate
policy and reports how the outcome would have differed. It is read-only: the
candidate is validated but never registered.

```python
from macp_sdk import (
    HISTORY_UNAVAILABLE, MacpTransportError, build_decision_policy, parse_error_details,
)

candidate = build_decision_policy("policy.stricter", "require 2 approvals")
try:
    report = client.what_if_replay(session_id, candidate)
except MacpTransportError as e:
    if e.macp_code == HISTORY_UNAVAILABLE:
        ...  # history compacted below the session start
    raise
if report.HasField("first_divergence"):  # index 0 is the SessionStart
    print("diverges at entry", report.first_divergence)
for denial in report.denials:            # details decode like POLICY_DENIED
    print(denial.message_id, parse_error_details(denial.details).denials)
```

- The method is gated on `policy_registry.what_if_replay`.
  - It calls `initialize()` once if the client hasn't initialized yet.
  - If the flag is false, it raises `MacpSdkError` without sending the RPC.
  - Bindings older than macp-proto 0.1.12 lack the flag, so the gate always
    refuses under them.
- RPC failures raise `MacpTransportError` with `macp_code` set:
  - `SESSION_NOT_FOUND` or `FORBIDDEN`: the caller can't observe the session.
  - `INVALID_POLICY_DEFINITION`: the candidate failed validation.
  - `HISTORY_UNAVAILABLE`: the history has been compacted.

`AsyncMacpClient.what_if_replay` is the awaitable twin.

### Runtime-issued decision receipts (RFC-MACP-0013 §13)

A runtime can optionally issue the decision receipt for a resolved session
itself. It advertises this with `capabilities.decision_receipt`. The flag
`get_decision_receipt` means both RPCs are served, and `signed` means its
receipts carry an attestation.

```python
from macp_sdk import (
    RECEIPT_UNAVAILABLE, MacpTransportError, trusted_keys_from_jwks, verify_receipt,
)

try:
    fetched = client.get_decision_receipt(session_id)
except MacpTransportError as e:
    if e.macp_code == RECEIPT_UNAVAILABLE:
        ...  # no binding Commitment yet, or no receipt can be issued
    raise
trusted = trusted_keys_from_jwks(client.get_receipt_keys())
result = verify_receipt(fetched.receipt, trusted)  # attested / integrity_only / ...
archive(fetched.raw)  # the runtime's bytes, verbatim
```

- `get_decision_receipt` returns a `DecisionReceiptResult`:
  - `receipt` is the parsed dict.
  - `raw` is the runtime's `receipt_json`, verbatim. An attested receipt is
    served byte-identical on every call, so store `raw`.
  - Auth is required, and the caller must be able to observe the session.
- `get_receipt_keys` returns the runtime's JWK Set (`ReceiptKeySet`).
  - It needs only transport auth.
  - An unsigned runtime returns `{"keys": []}`, so its receipts verify as
    `integrity_only` at best.
- `trusted_keys_from_jwks` turns the set into the `kid -> x` map that
  `verify_receipt` takes.
  - It keeps only Ed25519 public keys usable for signatures, and drops any
    `kid` the set names twice.
  - Like `verify_receipt`, it never raises.
  - See [Security](security.md#trusting-runtime-receipt-keys) for what trusting
    these keys does and does not establish.
- Both methods are gated on `decision_receipt.get_decision_receipt`, the same
  way as `what_if_replay`.
  - They call `initialize()` once if needed.
  - If the flag is false, they raise `MacpSdkError` without sending the RPC.
  - The gate always refuses under bindings older than macp-proto 0.1.13.
- RPC failures raise `MacpTransportError` with `macp_code` set:
  `RECEIPT_UNAVAILABLE` (provisional in the registry), `SESSION_NOT_FOUND` or
  `FORBIDDEN`.
- A response that isn't valid JSON raises `MacpSdkError`. That covers a receipt
  that isn't an object and a key set without a `keys` list. The non-JSON
  literals `NaN` and `Infinity` are rejected too.
- These calls are not retried. A receipt exists only for a session that
  resolved with a `CommitmentPayload`, so `RECEIPT_UNAVAILABLE` on an open
  session may clear once it resolves that way. Poll the session state rather
  than the receipt. A session that resolves without a Commitment stays
  unavailable.

`AsyncMacpClient` has awaitable twins of both methods.

### Server-streaming (watchers)

The SDK wraps each server-streaming RPC in a `*Watcher` that normalises responses into typed records. See [Streaming](guides/streaming.md) and [Session Discovery](guides/session-discovery.md) for usage.

| RPC | Watcher | Yields |
|-----|---------|--------|
| `WatchModeRegistry` | `ModeRegistryWatcher` | Registry diff events |
| `WatchRoots` | `RootsWatcher` | Root diff events (runtime currently idles) |
| `WatchPolicies` | `PolicyWatcher` | `PolicyChange(descriptors, observed_at_unix_ms)` |
| `WatchSessions` | `SessionLifecycleWatcher` | `SessionLifecycleEvent` (`CREATED`/`RESOLVED`/`EXPIRED`/`CANCELLED`/`SUSPENDED`/`RESUMED`) |
| `WatchSignals` | `SignalWatcher` | Ambient-plane signal envelopes |
