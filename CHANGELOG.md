# Changelog

## 0.4.0 (2026-06-22)

Adopt ``macp-proto 0.1.3`` — session **suspend / resume**, an explicit
**cancelled** terminal state, and **superseding commitments**. Requires a
runtime built with the suspend/cancel/supersede surface (>= 0.4.0).

### Added

- **``MacpClient.suspend_session()`` / ``resume_session()``** — wrap the new
  ``SuspendSession`` / ``ResumeSession`` RPCs with the same auth/metadata and
  ``raise_on_nack`` handling as ``cancel_session()``. Suspending moves a
  session to the non-terminal ``SESSION_STATE_SUSPENDED`` state (messages
  sent meanwhile are rejected with a non-OPEN error); resuming returns it to
  ``OPEN``.
- **``BaseSession.suspend()`` / ``resume()``** — session-level convenience
  helpers next to ``cancel()``, inherited by every mode helper
  (``DecisionSession``, ``ProposalSession``, ``TaskSession``,
  ``HandoffSession``, ``QuorumSession``). Delegate to the client RPCs with
  the session's auth. Parity with ``macp-sdk-typescript``.
- **``SessionLifecycle`` predicates** ``is_cancelled`` / ``is_suspended`` /
  ``is_resumed`` for the new ``CANCELLED`` / ``SUSPENDED`` / ``RESUMED``
  lifecycle events from ``WatchSessions``.
- **``build_commitment_ref()``** + a ``supersedes`` parameter on
  ``build_commitment_payload()`` — author a ``CommitmentPayload`` that
  revises a prior commitment via a ``CommitmentRef``
  (``session_id`` + ``commitment_hash``). Unrelated to proposal-mode
  ``supersedes_proposal_id``.

### Changed

- **Cancellation surfaces as ``CANCELLED``, not ``EXPIRED``** (semantic
  shift). An accepted ``cancel_session()`` now terminates the session as
  ``SESSION_STATE_CANCELLED`` and emits an ``EVENT_TYPE_CANCELLED`` event;
  ``is_expired`` is now TTL/policy-expiry only. ``is_terminal`` includes
  ``CANCELLED``, so terminal-wait loops are unaffected — but code that
  special-cased ``is_expired`` to detect cancellation must switch to
  ``is_cancelled``.
- **Dependency floors** raised to ``macp-proto>=0.1.3`` and, because 0.1.3's
  generated gRPC stubs require it, ``grpcio>=1.81.1``.

### Fixed

- **``SessionLifecycle.is_terminal`` now includes ``CANCELLED``** — a
  consumer looping ``until event.is_terminal`` no longer hangs on a
  cancelled session.

## 0.3.0 (2026-04-21)

Session discovery surface: the SDK now wraps the runtime's
``ListSessions`` and ``WatchSessions`` RPCs, so Python orchestrators
and supervisor agents can enumerate active sessions and react to
``CREATED`` / ``RESOLVED`` / ``EXPIRED`` lifecycle events without
polling ``GetSession``. Parity with ``macp-sdk-typescript`` 0.3.0.

### Added

- **``MacpClient.list_sessions()``** (SDK-PY-2) — unary RPC returning
  ``list[SessionMetadata]``; each entry includes the ``context_id``
  and ``extension_keys`` the runtime projects from the accepted
  SessionStart payload.
- **``MacpClient.watch_sessions()``** (SDK-PY-3) — server-streaming
  RPC yielding ``WatchSessionsResponse`` frames.
- **``SessionLifecycleWatcher`` + ``SessionLifecycle``** (SDK-PY-3)
  in ``macp_sdk.watchers`` — high-level wrapper mirroring the existing
  ``PolicyWatcher`` pattern. Event-type integers are normalised to
  short string names (``CREATED`` / ``RESOLVED`` / ``EXPIRED``), with
  ``is_created``/``is_resolved``/``is_expired``/``is_terminal``
  convenience predicates. Exported from ``macp_sdk`` package root.
- **SDK-PY-6 — bootstrap ``cancel_callback`` now wired**
  (RFC-0001 §7.2 Option A). When ``bootstrap.cancel_callback =
  {host, port, path}`` is present, ``from_bootstrap`` spins up a
  stdlib-only HTTP daemon bound to ``participant.stop()`` and attaches
  it to the participant; a ``POST`` with ``{runId, reason}`` shuts the
  agent down cleanly. New public helpers on ``macp_sdk.agent``:
  ``start_cancel_callback_server`` + ``CancelCallbackServer``.

### Changed

- **SDK-PY-4**: ``_default_capabilities()`` now advertises
  ``SessionsCapability(stream=True, list_sessions=True,
  watch_sessions=True)``.
- **SDK-PY-5 — ``AuthConfig.for_dev_agent`` now emits
  ``Authorization: Bearer <agent_id>``**. The runtime's
  ``dev_authenticate`` fallback binds the token value verbatim as the
  sender, so participant lists keep working unchanged. Requires
  runtime ≥ 0.4.0.
- **``AuthConfig.agent_id`` field removed**. Construct directly with
  ``AuthConfig(bearer_token=...)`` or use the
  ``for_dev_agent`` / ``for_bearer`` classmethods.

## 0.2.3 (unreleased)

Streaming-path uplift: non-initiator agents no longer miss
``SessionStart`` / early ``Proposal`` envelopes when their stream
opens after the initiator has already published them.

### Added

- **``MacpStream.send_subscribe(session_id, after_sequence=0)``** —
  RFC-MACP-0006-A1 subscribe-only frame. The runtime replays accepted
  session envelopes from ``after_sequence`` onwards then switches to
  live broadcast, removing the previous late-attach limitation
  documented in ``docs/guides/streaming.md``.
- **``GrpcTransportAdapter``** now invokes ``send_subscribe`` as the
  first frame on every stream, so ``Participant`` / ``from_bootstrap``
  agents receive the full session history without any caller changes.
- ``tests/unit/test_client_stream.py`` (7 tests) covering the
  subscribe frame, envelope multiplexing through ``_request_iter``,
  proto serialisation, and closed-stream guards.
- ``tests/integration/test_subscribe_replay.py`` (2 tests) covering
  the late-joiner replay path and the ``after_sequence`` skip path.
- ``tests/unit/test_agent_transports.py`` — two new assertions that
  verify ``GrpcTransportAdapter`` subscribes before consuming
  responses.
- **SDK-PY-1 — ``InitiatorConfig.extensions``** (``dict[str, bytes]``)
  is now threaded through ``from_bootstrap`` and
  ``Participant._emit_initiator_envelopes()`` onto
  ``SessionStartPayload.extensions``. Bootstrap JSON carries each
  value as canonical proto-JSON base64; the loader decodes it back to
  bytes. Matches the Rust runtime's opaque ``map<string, bytes>``
  storage (see ``runtime/src/runtime.rs::process_session_start``);
  keys surface on ``SessionMetadata.extension_keys`` for the
  control-plane projection (CP-17).
- ``tests/unit/test_agent_participant.py`` (4 tests) covering the
  base64 decode, absent-key default, action-level extension
  forwarding, and the empty-dict → ``None`` normalisation path.

### Changed

- **``macp-proto`` pin widened** to ``>=0.1.2,<0.2.0``. 0.1.2 is the
  first release that exposes ``subscribe_session_id`` /
  ``after_sequence`` on ``StreamSessionRequest``; the subscribe path
  is a runtime ``ValueError`` against older bindings.
- Streaming guide documents the subscribe frame and removes the
  "No late-attach handshake" known-limitations bullet.

## 0.2.1 (unreleased)

Quality-uplift patch release (see `plans/code-quality-uplift.md`).
No public API changes; no semantic changes to the 0.2.0 behaviour.

### Changed (internal)

- **Ack error-reason parsing unified.** `MacpClient.send` and
  `MacpClient.cancel_session` now share a single
  `_failure_from_ack` helper, so `MacpAckError.reasons` surfaces on
  `CancelSession` NACKs too (e.g. `POLICY_DENIED` with structured
  rule IDs when cancellation isn't delegated).
- **Mode-helper return types narrowed** from `Any` to
  `envelope_pb2.Ack` on every action method in
  `Decision/Proposal/Task/Handoff/QuorumSession` and on
  `BaseSession.start` / `commit` / `cancel` / `metadata`. IDEs now
  autocomplete `ack.ok`, `ack.session_id`, `ack.message_id` directly.
- **`AuthConfig` docs rebalanced.** `docs/auth.md` now leads with
  `expected_sender` and treats `sender_hint` as an advanced escape
  hatch; `README.md` quick-start leads with a Bearer + TLS example.

### Added

- **Test backfill:** `tests/unit/test_watchers.py` (14 tests),
  `tests/unit/test_serialization_determinism.py` (11 tests),
  `tests/unit/test_proto_registry.py` (19 tests),
  `tests/unit/test_client_helpers.py` (17 tests),
  `tests/unit/test_validation.py` (19 tests). Unit-test line
  coverage moved from 80% → 87%.
- **Coverage gate:** CI and publish workflows now enforce
  `--cov-fail-under=85`.
- **Proto-drift cron:** `.github/workflows/proto-drift.yml` runs the
  test suite against the latest `macp-proto 0.x` daily and opens a
  tracking issue on regression.
- **Ruff rule set extended** with `ARG`, `RET`, `N`, `RUF`. Existing
  violations (sorted `__slots__`, unsorted `__all__`, stale `noqa`)
  auto-fixed; tests/examples got targeted per-file ignores.
- **Contributing guide:** `docs/contributing.md` documents the
  release flow and the `macp-proto` bump process.
- **Makefile help:** `make help` lists every target with a one-line
  description; `sync-fixtures` now points at the spec repo cleanly
  when missing.
- **`macp-proto` pin narrowed** to `>=0.1.0,<0.2.0`.
- **Direct-agent-auth examples:** `examples/direct_agent_auth_initiator.py`
  and `examples/direct_agent_auth_observer.py` mirror the
  integration test for runnable reference code.
- **Architecture doc** grew an "Action-method signature conventions"
  section pinning the `(ids*, *, payload_kwargs, sender, auth)` rule.

### Fixed

- Dropped an unused `monkeypatch` fixture in
  `tests/unit/test_sender_validation.py::test_signal_mismatch_raises`.
- Renamed an unused `message` parameter in the strategy-based
  `commitment_handler` factory to `_message` (with a comment) so
  the `ARG001` lint rule stays enabled going forward.

## 0.2.0 (unreleased)

Hardening release aligned with the `direct-agent-auth` plan — agents now
authenticate to the runtime directly with their own Bearer identity
(RFC-MACP-0004 §4), and the SDK enforces that posture client-side.

### Breaking

- `MacpClient(secure=...)` now defaults to `True` (RFC-MACP-0006 §3).
  Passing `secure=False` raises `MacpSdkError` unless `allow_insecure=True`
  is also supplied. Local dev must opt in explicitly:
  `MacpClient(target=..., allow_insecure=True, auth=...)`.
- `AuthConfig.for_dev_agent(agent_id)` now defaults
  `expected_sender=agent_id`. Calls that pass a different
  `sender=` without a matching per-call `auth=` override raise
  `MacpIdentityMismatchError`. This matches how the runtime already
  behaves and surfaces the problem earlier.
- Bootstrap files consumed by `from_bootstrap()` must set
  `"secure": false` **together with** `"allow_insecure": true` for
  plaintext transport.

### Added

- `MacpIdentityMismatchError` — raised client-side before an envelope
  reaches the wire when `sender=` conflicts with `auth.expected_sender`.
  Exported from `macp_sdk`.
- `AuthConfig.for_bearer(..., expected_sender=...)` — binds the token to
  an identity; the SDK enforces that `sender=` matches on every session
  helper and on `MacpClient.send_signal` / `send_progress`.
- `BaseSession.start(..., auth=...)` — matches the existing per-method
  `auth=` override on `commit/vote/...` so initiator agents can pass
  per-call credentials without reaching into private helpers.
- `.github/workflows/publish.yml` — trusted-publisher PyPI pipeline
  triggered on `v*` tags.
- Integration test `tests/integration/test_direct_agent_auth.py` covering
  the initiator-direct-auth flow (`DecisionSession(session_id=preallocated)
  .start().open_stream().propose()`) with both dev-header and Bearer auth.
- `docs/guides/direct-agent-auth.md` — walk-through of the new topology.

### Changed

- `MacpClient` bumps `client_version` default to `"0.2.0"` in the
  `ClientInfo` handshake.
- Examples, README, mode docs, and streaming guide updated to use
  `allow_insecure=True` for local dev and `expected_sender=` on every
  Bearer example.

## 0.1.0 (unreleased)

### Added
- `MacpClient` — sync gRPC client with all 14 runtime RPCs
- `MacpStream` — bidirectional streaming with background thread
- `BaseSession` / `BaseProjection` — abstract base classes for mode helpers
- `DecisionSession` + `DecisionProjection` — Decision mode
- `ProposalSession` + `ProposalProjection` — Proposal mode
- `TaskSession` + `TaskProjection` — Task mode
- `HandoffSession` + `HandoffProjection` — Handoff mode
- `QuorumSession` + `QuorumProjection` — Quorum mode
- `AuthConfig` — bearer token and dev agent authentication
- `RetryPolicy` + `retry_send` — exponential backoff retry helper
- `configure_logging` — SDK logger configuration
- Envelope builders and ID generators
- Full error hierarchy: `MacpSdkError`, `MacpAckError`, `MacpTransportError`, `MacpSessionError`, `MacpTimeoutError`, `MacpRetryError`
- Unit tests (90 tests)
- GitHub Actions CI (lint, typecheck, test, build)
- MkDocs documentation site
- Examples for all 5 modes
