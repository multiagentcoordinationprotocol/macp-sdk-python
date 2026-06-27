# Security

This page covers the SDK's security surface — TLS defaults, the `expected_sender` guardrail, and SDK-level retry behaviour for rate limits. The runtime is the source of truth for authentication, authorization, isolation, replay protection, rate limiting, and audit logging.

- [Runtime API § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#authentication)
- [Runtime API § Rate Limiting](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#rate-limiting)
- [Runtime Deployment § Authentication](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md#authentication)
- [Runtime Policy § Commitment authority](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md#commitment-authority)
- Per-mode authorization rules: [Runtime Modes](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md)

For the SDK-side `AuthConfig` API and identity-mismatch guardrail, see [Authentication](auth.md).

## Transport security

TLS 1.2+ is required in production. SDK 0.2.0+ defaults to `secure=True`; plaintext channels require an explicit `allow_insecure=True` opt-in:

```python
# Production
client = MacpClient(
    target="runtime.example.com:50051",
    root_certificates=open("ca.pem", "rb").read(),
    auth=AuthConfig.for_bearer("tok-prod-123", expected_sender="my-agent"),
)

# Local dev against MACP_ALLOW_INSECURE=1
client = MacpClient(
    target="127.0.0.1:50051",
    allow_insecure=True,
    auth=AuthConfig.for_dev_agent("my-agent"),
)
```

Passing `secure=False` without `allow_insecure=True` raises `MacpSdkError` so plaintext can never ship accidentally.

For server-side TLS configuration (`MACP_TLS_CERT_PATH`, `MACP_TLS_KEY_PATH`), see [Runtime Deployment](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md).

## Sender identity guardrail

The runtime binds the envelope `sender` from the authenticated identity and rejects any mismatch. The SDK additionally enforces `expected_sender` *client-side* so spoofed senders fail before the envelope leaves the process, surfacing as `MacpIdentityMismatchError` rather than an opaque `UNAUTHENTICATED`:

```python
auth = AuthConfig.for_bearer("tok-alice", expected_sender="alice")
session = DecisionSession(client, auth=auth)

session.vote("p1", "approve")                  # sender defaults to "alice" — OK
session.vote("p1", "approve", sender="mallory")
# ↑ raises MacpIdentityMismatchError(expected="alice", actual="mallory")
```

Always set `expected_sender` in production. See [Authentication](auth.md) for the full pattern, including per-operation auth overrides for multi-participant agents.

## Retrying rate limits and transient failures

The runtime enforces per-sender rate limits and returns `RATE_LIMITED` when exceeded. The SDK ships `RetryPolicy` + `retry_send()` for safe exponential-backoff retries (idempotency keys make this safe — see [Core Protocol § Envelopes](protocol.md#envelopes)):

```python
from macp_sdk import RetryPolicy, retry_send

policy = RetryPolicy(
    max_retries=5,
    backoff_base=0.5,
    retryable_codes=frozenset({"RATE_LIMITED", "INTERNAL_ERROR"}),
)
retry_send(client, envelope, policy=policy, auth=auth)
```

Limit defaults and environment variables: [Runtime API § Rate Limiting](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/API.md#rate-limiting).

## SDK-side production checklist

Runtime-side hardening (TLS keys, audit logging, token storage, rate-limit tuning) is covered in [Runtime Deployment § Production checklist](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/deployment.md#production-checklist). The items below are SDK-specific:

- [ ] Use TLS (default since SDK 0.2.0) — never pass `allow_insecure=True` in prod
- [ ] Set `expected_sender` on every `AuthConfig.for_bearer` call so sender spoofing fails fast (`MacpIdentityMismatchError`)
- [ ] Use real bearer tokens — never `for_dev_agent` in prod
- [ ] When sending as multiple participants, scope auth per call (see [Authentication § Per-operation auth overrides](auth.md#per-operation-auth-overrides))
- [ ] Wrap network calls in `retry_send()` with a `RetryPolicy` that excludes permanent codes (`FORBIDDEN`, `INVALID_ENVELOPE`)
- [ ] Use `SessionLifecycleWatcher` for supervisor visibility rather than polling `GetSession` (see [Session Discovery](guides/session-discovery.md))
