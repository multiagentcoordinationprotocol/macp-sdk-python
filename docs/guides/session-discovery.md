# Session Discovery

From SDK 0.3.0 the SDK wraps the runtime's `ListSessions` and `WatchSessions`
RPCs. Together they let orchestrators and supervisor agents enumerate active
sessions and react to lifecycle events (`CREATED` / `RESOLVED` / `EXPIRED`,
plus `CANCELLED` / `SUSPENDED` / `RESUMED` since SDK 0.4.0) without polling
`GetSession`.

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

### Pagination (macp-proto 0.1.6)

`macp-proto 0.1.6` added `page_size` / `page_token` to the `ListSessions`
request, and the SDK threads them: `list_sessions()` **auto-paginates** —
it follows the runtime's `next_page_token` until empty and returns the
complete list — so callers are forward-compatible with a paginating runtime.
Because it drains every page before returning, the complete result set is
accumulated in one in-process list; for a session count large enough that
this matters, use `list_sessions_page` (below) to walk pages manually
instead of holding the whole set in memory at once.

> **Runtime status (since runtime v0.8.0):** the runtime implements
> `ListSessions` pagination server-side — it decodes/encodes an opaque
> continuation token, honours `page_size` against the runtime's configured
> default/max page size (overridable per deployment), rejects a negative
> `page_size`, and returns a non-empty `next_page_token` when more pages
> remain. A pre-0.8 runtime ignores `page_size`/`page_token` and returns the
> full set in a single page with an empty token; `list_sessions()` returns
> the complete list correctly either way, so only `list_sessions_page`'s
> manual-paging behaviour differs by runtime version.

```python
all_sessions = client.list_sessions(page_size=100)   # drains all pages
```

If you want to page manually — e.g. to render one page at a time — use
`list_sessions_page`, which returns `(sessions, next_page_token)`. An empty
token means the last page; **don't assume a complete list until the token is
empty**.

```python
page, token = client.list_sessions_page(page_size=50)
while token:
    more, token = client.list_sessions_page(page_size=50, page_token=token)
    page.extend(more)
```

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
`"SUSPENDED"`, `"RESUMED"`). These values are the wire enum names with the
`EVENT_TYPE_` prefix stripped, and the terminal subset (`RESOLVED`,
`EXPIRED`, `CANCELLED`) is exported as a reusable
`macp_sdk.TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES`. **Cross-SDK divergence:**
`macp-sdk-typescript` surfaces the *prefixed* form (`"EVENT_TYPE_RESOLVED"`)
and exports its own similarly-named-but-differently-shaped
`TERMINAL_SESSION_LIFECYCLE_EVENT_TYPES`, so a membership test on a log
written by the other SDK must normalise first —
`t.removeprefix("EVENT_TYPE_") in TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES`.
Convenience predicates:

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

> **Suspension cap (SDK 0.5.0 / runtime v0.5.0):** pass `max_suspend_ms` to
> `session.start(...)` (or `SessionStart`) to bind a per-session maximum
> suspension window. `0` (default) selects the runtime default (currently 7
> days). A suspension that outlasts the cap **expires** the session
> (`SUSPENDED` → `EXPIRED`), which you will observe as an `is_expired`
> lifecycle event. The resolved cap is recorded on the session's `SessionStart`
> log entry and used on replay, so it is stable regardless of later runtime
> configuration. Negative values are rejected client-side.

### Startup snapshot semantics

The runtime's initial sync emits a `CREATED` event for **every session
currently in its registry** at subscribe time — not just open ones. A
terminal session still resident in memory (terminal sessions are evicted
after `MACP_SESSION_RETENTION_SECS`, one hour by default) arrives as a
`CREATED` event too; its real lifecycle state is only visible via
`event.session.state`. `event.is_created` therefore does not mean "still
open" — check `event.session.state` if you only want to act on open
sessions. Live events follow the initial sync. That means a freshly-started
supervisor sees every session in the registry, open or terminal, without a
separate `list_sessions()` call:

```python
from macp.v1 import envelope_pb2

for event in SessionLifecycleWatcher(client).changes():
    if event.is_created and event.session.state == envelope_pb2.SessionState.SESSION_STATE_OPEN:
        register(event.session)   # fires once per pre-existing OPEN session, plus every new one
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

Both RPCs require the same Bearer auth as any other SDK call, but neither
is scoped to the caller's identity. `GetSession` is participant/observer-
scoped, while `ListSessions` and `WatchSessions` return metadata for **all**
sessions to any authenticated identity (RFC-0006 permits this shape; see the
runtime's [Deployment § Observation-surface authorization](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md#observation-surface-authorization)).
Deployments with confidentiality requirements between agent groups should
front these RPCs with a proxy, or restrict which identities may call them.

## Related

- [Streaming → Watchers](streaming.md#server-streaming-watchers-macp_sdkwatchers)
  for the full watcher catalogue (`PolicyWatcher`, `SignalWatcher`, …).
- [Building Orchestrators → Supervisor pattern](building-orchestrators.md#pattern-supervisor--observer)
  for a worked example.
