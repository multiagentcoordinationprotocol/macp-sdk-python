# Error Handling and Retry

The SDK maps runtime error codes to a Python exception hierarchy and provides retry helpers. The canonical list of error codes (with HTTP status mappings and runtime-side meanings) lives in [Runtime API § Message Transport](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#message-transport) and [Runtime SDK Guide § Error handling](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md#error-handling). The tables below map them to Python exceptions and retry behaviour.

## Exception hierarchy

Protocol, session, and transport failures derive from `MacpSdkError`. Argument-type checks,
local-file-parsing, and internal-mapping lookups raise the idiomatic Python built-ins instead:
`TypeError` for a wrong argument *type*, `ValueError` for a malformed bootstrap file or an
unknown internal mapping key. Three such residual sites are `agent/runner.py`'s bootstrap
parsing (`ValueError`, at `:43`, `:56`, `:84` and `:94`), `envelope.py:225`'s non-protobuf
message argument (`TypeError`), and `proto_registry.py:129`'s internal registry lookup
(`ValueError`). (A few module-level `__getattr__` shims for deprecated aliases also raise
`AttributeError` on an unknown name — that's Python's import protocol, not part of the SDK's
error contract.)

```
MacpSdkError                    Base exception
├── MacpAckError                Runtime rejected the message (NACK)
├── MacpSessionError            Session-level error (wrong state, not started)
├── MacpIdentityMismatchError   Envelope sender does not match the auth identity
└── MacpTransportError          gRPC communication failure
    ├── MacpTimeoutError        Operation timed out
    └── MacpRetryError          All retry attempts exhausted
```

## Handling NACKs

When the runtime rejects a message, the SDK raises `MacpAckError` with a structured `AckFailure`:

```python
from macp_sdk import MacpAckError

try:
    session.vote("p1", "approve", sender="alice")
except MacpAckError as e:
    print(e.failure.code)        # e.g., "FORBIDDEN"
    print(e.failure.message)     # e.g., "sender not authorized for mode"
    print(e.failure.session_id)  # session context
    print(e.failure.message_id)  # message that was rejected
```

### Structured policy denials

A `POLICY_DENIED` failure carries two views of why the commitment was
rejected. `e.reasons` is a list of human-readable strings (all runtime
versions). Runtime ≥ 0.9.0 also sends `e.denials`, a list of `PolicyDenial`
objects (RFC-MACP-0012 §6.2) with a stable `code`, a `message`, and the
`participants` the reason concerns:

```python
from macp_sdk import MacpAckError
from macp_sdk.errors import DENY_VETO

try:
    session.commit(...)
except MacpAckError as e:
    if e.failure.code == "POLICY_DENIED":
        for denial in e.denials:
            if denial.code == DENY_VETO:
                print("vetoed by", denial.participants)
```

Since 0.9.0 the `reasons` strings **no longer embed participant ids** (a veto
used to end in `", from: agent://a, agent://b"`), so read participants from
`denials`, not by parsing `reasons`. Known codes are in `KNOWN_DENY_CODES`
(`veto`, `veto_hold`, `quorum_not_met`, `threshold_not_met`,
`confidence_below_minimum`, `evaluation_required`, `no_votes`,
`decline_over_approval`, `decline_not_backed`, `criterion_not_met`,
`output_required`, `reassignment_not_allowed`, `unsupported_schema_version`,
`invalid_rules`, `unspecified`). The set is open: a custom evaluator may send
its own code, which the SDK passes through unchanged, so treat any
`denial.code` not in `KNOWN_DENY_CODES` as `unspecified`. Against a pre-0.9
runtime `denials` is empty.

`parse_error_details(raw_bytes)` decodes the same JSON from any source:
`Ack.error.details`, an inline stream `MACPError.details`, or the
`macp-error-details-bin` trailing metadata.

### gRPC status vs. MACP code (runtime 0.9.0)

Most rejections arrive in the `Ack`, with the RPC itself succeeding. When a MACP error is
returned as a gRPC status instead, runtime ≥ 0.9.0 picks the status from the
error-codes registry (`POLICY_DENIED`/`FORBIDDEN` → `PERMISSION_DENIED`,
`SESSION_NOT_FOUND`/`UNKNOWN_POLICY_VERSION` → `NOT_FOUND`, `INVALID_*` →
`INVALID_ARGUMENT`, `DUPLICATE_MESSAGE`/`SESSION_ALREADY_EXISTS` →
`ALREADY_EXISTS`, `SESSION_NOT_OPEN` → `FAILED_PRECONDITION`,
`PAYLOAD_TOO_LARGE`/`RATE_LIMITED` → `RESOURCE_EXHAUSTED`, `INTERNAL_ERROR` →
`INTERNAL`) and attaches the registry code as `macp-error-code` metadata.
Several codes share one status, so the SDK branches on that metadata key:

- `client.send()` raises `MacpAckError` with `failure.code` set to the
  registry code (and `reasons`/`denials` from `macp-error-details-bin`),
  including `UNAUTHENTICATED`, which `retry_send` therefore doesn't retry.
- Every other RPC raises `MacpTransportError`, where `exc.code` is the gRPC
  status name and `exc.macp_code` is the registry code. `macp_code` is
  `None` when the runtime attached no code.
- Mutating a read-only policy registry still raises
  `MacpAckError(code="FAILED_PRECONDITION")`, as before 0.9; on 0.9 its
  `e.macp_code` is `REGISTRY_READ_ONLY`.

Runtime 0.9.0 also adds four control-plane codes, all exported from
`macp_sdk`: `REGISTRY_READ_ONLY` (`FAILED_PRECONDITION`), `INVALID_REQUEST`
(`INVALID_ARGUMENT`; e.g. a negative `page_size` or bad `page_token`),
`STREAM_LAGGED` (`RESOURCE_EXHAUSTED`; resubscribe with `after_sequence`) and
`AGENT_NOT_FOUND` (`NOT_FOUND`; `get_manifest` for an agent the runtime
doesn't host). Treat an unrecognised `macp_code` by its gRPC status.

A few read-only RPCs (`initialize`, `get_session`, `get_manifest`,
`list_sessions`/`list_sessions_page`, `get_policy`, `list_policies`) raise
the raw `grpc.RpcError` rather than an SDK error. Read its registry code with
`macp_error_code()` (`None` against a pre-0.9 runtime):

```python
import grpc
from macp_sdk import SESSION_NOT_FOUND, macp_error_code

try:
    client.get_session(session_id)
except grpc.RpcError as e:
    if macp_error_code(e) == SESSION_NOT_FOUND:
        ...
```

Against a pre-0.9 runtime (no `macp-error-code`) `send()` keeps the old
inference: `ALREADY_EXISTS` → `SESSION_ALREADY_EXISTS`,
`FAILED_PRECONDITION` → `POLICY_DENIED`, anything else → `MacpTransportError`.

### Error code categories

**Permanent errors** — Do not retry. Fix the underlying issue.

| Code | Cause | Action |
|------|-------|--------|
| `UNAUTHENTICATED` | Bad token or missing credentials | Check `AuthConfig` |
| `FORBIDDEN` | Sender not authorized for this mode/session | Check token permissions |
| `SESSION_NOT_FOUND` | Session doesn't exist | Verify session_id |
| `SESSION_NOT_OPEN` | Session already resolved or expired | Check session state |
| `INVALID_ENVELOPE` | Malformed envelope or payload | Fix message construction |
| `MODE_NOT_SUPPORTED` | Runtime doesn't support this mode | Check `client.list_modes()` |
| `PAYLOAD_TOO_LARGE` | Exceeds max payload size (default 1MB) | Reduce payload |
| `INVALID_SESSION_ID` | Session ID format invalid (lowercase UUID v4/v7, or base64url of 22-128 chars; the runtime caps ids at 128 bytes) | Use `new_session_id()` |
| `UNSUPPORTED_PROTOCOL_VERSION` | Version mismatch | Update SDK |
| `UNKNOWN_POLICY_VERSION` | Commitment `policy_version` doesn't match the session's bound policy | Echo the bound id, or send an empty `policy_version` (see below) |
| `INVALID_POLICY_DEFINITION` | `RegisterPolicy` refused the descriptor (runtime ≥ 0.9.0 closes every rule object): an unknown rule key, an off-domain value, an unsupported `schema_version`, an `x-` key no embedder registered, or a legacy `audit` block | Build descriptors with the `build_*_policy` helpers, which refuse off-domain values client-side with `MacpSessionError` |
| `FAILED_PRECONDITION` | Mutating a read-only policy registry (`MACP_POLICIES_DIR`) | Check `Initialize` `policy_registry.register_policy` first |

> **Commitment `policy_version` echo contract (runtime v0.5.0).** A commitment
> with an **empty** `policy_version` matches whatever policy the session is
> bound to — a session started with an empty policy_version no longer has to
> echo `policy.default`. A **non-empty** value must equal the resolved policy
> id (e.g. `policy.default`) or the commitment is rejected with
> `UNKNOWN_POLICY_VERSION`. The SDK's session helpers echo
> `self.policy_version` (default `policy.default`) on `commit()`, so
> `start`/`commit` stay self-consistent by construction.

**Transient errors** — Safe to retry with backoff.

| Code | Cause | Action |
|------|-------|--------|
| `RATE_LIMITED` | Per-sender rate limit exceeded | Retry with backoff |
| `INTERNAL_ERROR` | Runtime internal failure | Retry with backoff |

### Duplicate detection

A duplicate `message_id` returns `ok=true, duplicate=true` — not an error. The SDK's session helpers generate unique `message_id` values automatically. If you're building custom envelopes, ensure uniqueness.

## Retry with backoff

The SDK provides `retry_send()` for automatic exponential backoff:

```python
from macp_sdk import RetryPolicy, retry_send

# Default policy: 3 retries, 0.1s base backoff, retries RATE_LIMITED and INTERNAL_ERROR
retry_send(client, envelope, auth=auth)

# Custom policy for high-throughput workloads
policy = RetryPolicy(
    max_retries=5,
    backoff_base=0.5,        # first retry after 0.5s
    backoff_max=10.0,        # cap at 10s between retries
    retryable_codes=frozenset({"RATE_LIMITED", "INTERNAL_ERROR"}),
)
retry_send(client, envelope, policy=policy, auth=auth)
```

`retry_send` raises `MacpRetryError` (subclass of `MacpTransportError`) when all attempts are exhausted.

!!! warning "Session helpers don't retry automatically"
    `session.vote()`, `session.propose()`, etc. do **not** retry on failure. They call `client.send()` once. If you need retry behavior, build custom envelopes and use `retry_send()`, or wrap the session helper call in your own retry logic.

## Transport errors

`MacpTransportError` is raised when gRPC communication fails entirely (network down, server unreachable, connection reset), and also when a watcher's `next_change()`/`next_signal()` finds a server-streaming RPC has ended before yielding an item — in that case `exc.code` is `None` (no gRPC status applies to a cleanly-ended stream), unlike the network-failure case where `code` carries the status name:

```python
from macp_sdk import MacpTransportError

try:
    client.initialize()
except MacpTransportError as e:
    print(f"Cannot reach runtime: {e}")
```

## Timeout handling

Set timeouts at the client level or per-operation:

```python
# Client-level default timeout
client = MacpClient(target="...", default_timeout=10.0, ...)

# Per-operation override
response = client.get_session(session_id, timeout=5.0)
```

When a timeout occurs, gRPC raises an error that the SDK translates to `MacpTransportError`.

## Graceful degradation patterns

### Check session state before acting

```python
# Query metadata before sending messages to a potentially stale session
meta = session.metadata()
if meta.metadata.state == core_pb2.SessionState.OPEN:
    session.vote("p1", "approve", sender="alice")
else:
    print(f"Session is {meta.metadata.state}, skipping vote")
```

### Handle already-resolved sessions

```python
try:
    session.vote("p1", "approve", sender="alice")
except MacpAckError as e:
    if e.failure.code == "SESSION_NOT_OPEN":
        # Session resolved or expired while we were preparing
        print("Session already concluded")
    else:
        raise
```

### Cancellation

Cancel a session that should not proceed:

```python
session.cancel(reason="coordinator decided to abort")
```

This transitions the session to **CANCELLED** (SDK < 0.4.0 surfaced it as
EXPIRED; `macp-proto 0.1.3` split the states). Already-resolved sessions
cannot be cancelled.
