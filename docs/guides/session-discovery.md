# Session Discovery

From SDK 0.3.0 the SDK wraps the runtime's `ListSessions` and `WatchSessions`
RPCs. Together they let orchestrators and supervisor agents enumerate active
sessions and react to `CREATED` / `RESOLVED` / `EXPIRED` lifecycle events
without polling `GetSession`.

For the underlying RPC contracts (request/response shapes, scoping rules), see [Runtime API § Discovery](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#discovery) and [§ Streaming Watches](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#streaming-watches).

## When to use

- **Supervisor dashboards** — show every OPEN session for a tenant and their
  terminal outcomes as they occur.
- **Late-joining orchestrators** — recover in-flight sessions after a restart
  without keeping an out-of-band session registry.
- **Reconciliation** — cross-check your control-plane's view of sessions
  against what the runtime actually accepted.

## `list_sessions` (snapshot)

```python
from macp_sdk import AuthConfig, MacpClient

client = MacpClient(
    target="runtime:50051",
    auth=AuthConfig.for_bearer("tok-ops", expected_sender="ops"),
)
client.initialize()

for meta in client.list_sessions():
    print(
        meta.session_id,
        meta.mode,
        meta.state,
        meta.context_id or "-",
        list(meta.extension_keys),
    )
```

Each entry is a `SessionMetadata` proto with the same shape returned by
`GetSession` — including the projected `context_id` and `extension_keys`
fields that surface any extension blobs the initiator attached to
`SessionStart.extensions` (see [Protocol → SessionStart](../protocol.md#sessionstart)).

## `SessionLifecycleWatcher` (live stream)

```python
from macp_sdk import SessionLifecycleWatcher

watcher = SessionLifecycleWatcher(client)

for event in watcher.changes():
    print(event.event_type, event.session.session_id)
    if event.is_terminal:
        # This session will not emit more events.
        ...
```

`event.event_type` is a short string (`"CREATED"`, `"RESOLVED"`,
`"EXPIRED"`, and — since SDK 0.4.0 / `macp-proto 0.1.3` — `"CANCELLED"`,
`"SUSPENDED"`, `"RESUMED"`). Convenience predicates:

| Predicate | True for |
|-----------|---------|
| `event.is_created` | `CREATED` |
| `event.is_resolved` | `RESOLVED` (Commitment accepted) |
| `event.is_expired` | `EXPIRED` (TTL / policy expiry) |
| `event.is_cancelled` | `CANCELLED` (accepted `CancelSession`) |
| `event.is_suspended` | `SUSPENDED` (non-terminal; `SuspendSession`) |
| `event.is_resumed` | `RESUMED` (non-terminal; `ResumeSession`) |
| `event.is_terminal` | `RESOLVED`, `EXPIRED`, or `CANCELLED` |

> **Cancellation moved (SDK 0.4.0):** an accepted `CancelSession` now
> surfaces as `CANCELLED`, not `EXPIRED`. `is_terminal` includes `CANCELLED`,
> so loops that wait `until event.is_terminal` keep working — but switch any
> code that special-cased `is_expired` to detect cancellation over to
> `is_cancelled`. `SUSPENDED` / `RESUMED` are non-terminal.

### Startup snapshot semantics

The runtime emits an initial `CREATED` event for every session that is
already OPEN at subscribe time, then live events thereafter. That means a
freshly-started supervisor sees every in-flight session without a separate
`list_sessions()` call:

```python
for event in SessionLifecycleWatcher(client).changes():
    if event.is_created:
        register(event.session)   # fires once per pre-existing session, plus every new one
    elif event.is_terminal:
        finalise(event.session)
```

`list_sessions()` is still useful when you want a bounded snapshot without
holding the stream open.

### Blocking handler form

`watch(handler)` is shorthand for a blocking for-loop:

```python
def on_event(ev):
    dashboard.update(ev.session.session_id, ev.event_type)

SessionLifecycleWatcher(client).watch(on_event)  # blocks
```

### Threading

The watcher reads from a gRPC server-streaming RPC on the caller's thread.
Run it in a background thread or process if your agent also needs to send
envelopes on the same client:

```python
import threading

def run_watcher():
    for ev in SessionLifecycleWatcher(client).changes():
        handle(ev)

threading.Thread(target=run_watcher, daemon=True).start()
```

## Authorisation

Both RPCs require the same Bearer auth as any other SDK call — the runtime
scopes results to the authenticated identity. An agent only sees sessions it
is a participant in, plus any sessions its token is authorised to observe
via runtime config.

## Related

- [Streaming → Watchers](streaming.md#server-streaming-watchers-macp_sdkwatchers)
  for the full watcher catalogue (`PolicyWatcher`, `SignalWatcher`, …).
- [Building Orchestrators → Supervisor pattern](building-orchestrators.md#pattern-supervisor-observer)
  for a worked example.
