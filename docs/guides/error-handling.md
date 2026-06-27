# Error Handling and Retry

The SDK maps runtime error codes to a Python exception hierarchy and provides retry helpers. The canonical list of error codes (with HTTP status mappings and runtime-side meanings) lives in [Runtime API § Message Transport](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#message-transport) and [Runtime SDK Guide § Error handling](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md#error-handling). The tables below map them to Python exceptions and retry behaviour.

## Exception hierarchy

All SDK exceptions derive from `MacpSdkError`:

```
MacpSdkError                    Base exception
├── MacpAckError                Runtime rejected the message (NACK)
├── MacpSessionError            Session-level error (wrong state, not started)
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
| `INVALID_SESSION_ID` | Session ID format invalid | Use `new_session_id()` |
| `UNSUPPORTED_PROTOCOL_VERSION` | Version mismatch | Update SDK |

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

`MacpTransportError` is raised when gRPC communication fails entirely (network down, server unreachable, connection reset):

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

This transitions the session to EXPIRED. Already-resolved sessions cannot be cancelled.
