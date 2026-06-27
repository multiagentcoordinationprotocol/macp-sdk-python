# Authentication

All authenticated operations require an `AuthConfig`. The SDK supports two authentication methods, matching the runtime's security model.

For the runtime side of authentication (token issuance, validators, sender derivation), see [Runtime API § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#authentication). The general security guarantees (transport, isolation, replay protection, rate limiting, audit logging) are summarised in [Security](security.md).

## Development: dev-agent bearer auth

```python
from macp_sdk import AuthConfig

auth = AuthConfig.for_dev_agent("my-agent")
```

This sends `Authorization: Bearer my-agent`. The runtime's
`dev_authenticate` fallback (`runtime/src/security.rs`) binds the
bearer token value verbatim as the authenticated sender, so a dev
runtime without a configured auth resolver chain accepts the call and
treats the sender as `my-agent`.

!!! warning "Development only"
    **Never use in production** — the token value is unencrypted and
    trivially spoofable. In production, use
    [`AuthConfig.for_bearer`](#production-bearer-tokens) with a real
    token issued by `MACP_AUTH_TOKENS_JSON` / `MACP_AUTH_TOKENS_FILE`.

## Production: bearer tokens

```python
auth = AuthConfig.for_bearer(
    "tok-abc123",
    expected_sender="my-agent",   # client-side guardrail
)
```

This sends the `Authorization: Bearer tok-abc123` header. The runtime validates the token against its token configuration and maps it to a sender identity.

### `expected_sender` (client-side guardrail, SDK ≥ 0.2.0)

`expected_sender` is the identity the runtime will bind this token to. When
set, the SDK raises `MacpIdentityMismatchError` **before** the envelope
reaches the wire if a call passes an explicit `sender=` that does not match.
This surfaces identity mistakes as a clear Python exception instead of
an opaque `UNAUTHENTICATED` from the runtime.

```python
from macp_sdk import AuthConfig, MacpIdentityMismatchError

auth = AuthConfig.for_bearer("tok-alice", expected_sender="alice")
session = DecisionSession(client, auth=auth)

session.vote("p1", "approve")                  # sender defaults to "alice" — OK
session.vote("p1", "approve", sender="alice")  # matches — OK
session.vote("p1", "approve", sender="mallory")
# ↑ raises MacpIdentityMismatchError(expected="alice", actual="mallory")
```

When `expected_sender` is `None`, the SDK performs no client-side check
and the runtime remains the final authority.

### Token configuration (runtime side)

Token issuance, scope (`allowed_modes`, `can_start_sessions`, `max_open_sessions`, `can_manage_mode_registry`, `is_observer`), and the `MACP_AUTH_TOKENS_JSON` / `MACP_AUTH_TOKENS_FILE` format live in the runtime docs — see [Runtime Deployment § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md#authentication) and [Runtime API § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#authentication).

## JWT tokens

The runtime's resolver chain (JWT bearer → static bearer → dev-mode fallback) treats JWTs and opaque static tokens interchangeably at the wire level — both travel in `Authorization: Bearer`. The SDK does not need to parse or validate the JWT; just pass it to `AuthConfig.for_bearer`:

```python
auth = AuthConfig.for_bearer(jwt, expected_sender="agent://analyst")
```

The runtime's JWT resolver (active when `MACP_AUTH_ISSUER` is set) verifies signature, issuer, audience, and expiration against a JWKS, derives `sender` from the `sub` claim, and reads capability flags from the optional `macp_scopes` claim (`allowed_modes`, `can_start_sessions`, `max_open_sessions`, `can_manage_mode_registry`, `is_observer`). Set `expected_sender` to the value you expect `sub` to produce so `MacpIdentityMismatchError` surfaces client-side before the runtime NACKs.

Supported algorithms, JWKS configuration (`MACP_AUTH_JWKS_JSON` inline or `MACP_AUTH_JWKS_URL` fetched + cached), and the `macp_scopes` claim layout are documented in the runtime's [JWT mode guide](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/getting-started.md#jwt-mode).

## Observer identities

Non-participant agents (audit agents, dashboards, read-only observers) authenticate with a normal bearer credential whose runtime-side identity carries `is_observer: true`. On the SDK side, observers open a stream and call [`send_subscribe(session_id, after_sequence=0)`](guides/streaming.md#session-subscription-replay) to replay accepted history and then consume live envelopes — no membership in the session's `participants` list is required.

```python
auth = AuthConfig.for_bearer(observer_token, expected_sender="agent://auditor")
client = MacpClient(target="runtime:50051", auth=auth)

stream = client.open_stream()
stream.send_subscribe(session_id)          # full replay + live tail
for envelope in stream.responses(timeout=5.0):
    ...
```

Observers still cannot `Send` into a session unless the mode's authority rules allow it — passive observation does not bypass mode authority. See the runtime's [SDK guide § Observer identities](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/sdk-guide.md#observer-identities) for server-side provisioning.

## Sender identity

The `sender` field on envelopes is **runtime-derived**, not self-asserted — RFC-MACP-0004 §4. The runtime maps your auth credential (token or dev-header) to a single identity and rejects any envelope whose `sender` disagrees with `UNAUTHENTICATED`.

The SDK enforces the same rule client-side via `expected_sender`:

```python
from macp_sdk import AuthConfig, MacpIdentityMismatchError

auth = AuthConfig.for_bearer("tok-alice", expected_sender="alice")

# No explicit sender — the SDK fills in "alice".
session.vote("p1", "approve")

# Explicit sender matches — allowed.
session.vote("p1", "approve", sender="alice")

# Mismatch — raises MacpIdentityMismatchError BEFORE the envelope is sent.
session.vote("p1", "approve", sender="mallory")
```

See the [Direct Agent Auth guide](guides/direct-agent-auth.md) for the
end-to-end pattern.

### Advanced: `sender_hint`

`sender_hint` is the low-level field the SDK reads when no explicit `sender=`
is passed to a method. In almost every case you should not set it directly —
pass `expected_sender` instead and let the SDK derive `sender_hint` for you.

Supply `sender_hint` only when the envelope `sender` you want on the wire
differs from the identity the runtime binds to your token — a rare deployment
where one operator credential fronts for many logical senders. In that case,
keep `expected_sender` matching the token identity and override `sender_hint`
explicitly:

```python
auth = AuthConfig.for_bearer(
    "tok-fleet",
    sender_hint="fleet-agent-17",   # envelope .sender
    expected_sender="fleet-agent-17",
)
```

For all normal use, prefer:

```python
# Dev: sender_hint + expected_sender both default to the agent_id.
AuthConfig.for_dev_agent("alice")

# Prod: set expected_sender; sender_hint is derived automatically.
AuthConfig.for_bearer("tok-alice", expected_sender="alice")
```

## Per-operation auth overrides

Session helpers accept an `auth=` parameter on each method to override the session/client default:

```python
coordinator_auth = AuthConfig.for_dev_agent("coordinator")
alice_auth = AuthConfig.for_dev_agent("alice")
bob_auth = AuthConfig.for_dev_agent("bob")

# Session uses coordinator auth by default
session = DecisionSession(client, auth=coordinator_auth)
session.start(intent="...", participants=["coordinator", "alice", "bob"], ttl_ms=60_000)

# Override auth for specific messages
session.vote("p1", "approve", sender="alice", auth=alice_auth)
session.vote("p1", "approve", sender="bob", auth=bob_auth)

# Commitment uses the session's default (coordinator)
session.commit(action="approved", authority_scope="release", reason="majority")
```

This pattern is essential for multi-agent sessions where different participants use different credentials.

## Client-level vs. session-level auth

```python
# Client-level: used as fallback for all operations
client = MacpClient(target="...", auth=coordinator_auth)

# Session-level: overrides client auth for this session
session = DecisionSession(client, auth=special_auth)

# Method-level: overrides both client and session auth
session.vote("p1", "approve", auth=alice_auth)
```

Priority: **method-level > session-level > client-level**

## TLS configuration

TLS 1.2+ is required in production (RFC-MACP-0006 §3) and is the SDK default
in 0.2.0+. `secure=True` is implied unless you pass `allow_insecure=True`:

```python
# Production — secure by default
client = MacpClient(
    target="runtime.example.com:50051",
    root_certificates=open("ca.pem", "rb").read(),  # CA certificate
    auth=AuthConfig.for_bearer("tok-prod-123", expected_sender="my-agent"),
)

# Local dev against MACP_ALLOW_INSECURE=1 runtime — must opt in explicitly
client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,
    auth=AuthConfig.for_dev_agent("my-agent"),
)
```

Passing `secure=False` without `allow_insecure=True` raises `MacpSdkError`
so plaintext transport can never ship accidentally. The runtime must be
configured with `MACP_TLS_CERT_PATH` and `MACP_TLS_KEY_PATH` for TLS.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `MacpSdkError: this operation requires auth` | No auth configured | Pass `auth=` to client, session, or method |
| `MacpSdkError: secure=False requires allow_insecure=True` | TLS default now strict (SDK ≥ 0.2.0) | Pass `allow_insecure=True` for local dev; otherwise use `secure=True` |
| `MacpIdentityMismatchError` | Explicit `sender=` doesn't match `auth.expected_sender` | Use per-participant `auth=` with matching `expected_sender` |
| `MacpAckError: UNAUTHENTICATED` | Token invalid or expired | Check token in runtime config |
| `MacpAckError: FORBIDDEN` | Sender not authorized for this mode | Check `allowed_modes` in token config |
| `MacpAckError: FORBIDDEN` on Commitment | Sender is not the session initiator | Only the initiator can commit |
| Spoofed sender rejected | Sender doesn't match auth identity | Use correct auth for each participant |

## API Reference

::: macp_sdk.auth.AuthConfig
