# Changelog

## [0.8.0](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/v0.7.0...v0.8.0) (2026-08-31)


### ⚠ BREAKING CHANGES

* the first accepted Vote or ballot per sender now stands and later ones are discarded. Callers that relied on re-sending to change a vote no longer can. Discarded messages surface in projection.anomalies.

### Features

* add ProjectionAnomaly to the projection API ([#47](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/47)) ([5a4b7a6](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/5a4b7a6183a1f0f99e07a6437ff2fe71b33c8ba7))
* first accepted vote or ballot stands ([#48](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/48)) ([e22b2e4](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/e22b2e4251ffa95628d5a89aaf6ffad674791883))


### Bug Fixes

* make projection apply idempotent on message_id ([#45](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/45)) ([070f6f5](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/070f6f57a72193ea2900d15e9eefde5b20d4bc64))


Test-suite and CI/CD hardening, a projection replay-inflation fix, a new
projection-anomaly surface, and a **breaking change to vote/ballot
cardinality**. This is not additive-only: two projection code paths now return
different results than before for the same accepted history. See **Changed**
below before upgrading if any orchestrator re-sends a vote or ballot to
change it.

### Added

- **`ProjectionAnomaly`** (`macp_sdk.base_projection`) — a frozen, slots
  dataclass recording a discarded-message observation. Every projection now
  exposes `anomalies: list[ProjectionAnomaly]` and `has_anomalies: bool`.
  Two `kind` constants are exported alongside it: `ANOMALY_DUPLICATE_VOTE =
  "duplicate_vote"` and `ANOMALY_DUPLICATE_BALLOT = "duplicate_ballot"`.
  **Wired in this release:** `_record_anomaly` has two callers —
  `DecisionProjection`'s Vote handling and `QuorumProjection._set_ballot` —
  both added by the first-wins change documented under **Changed** below.
  A discarded second Vote or ballot from the same sender is what populates
  `anomalies` / `has_anomalies`; nothing else in this release produces one.
  An anomaly is an **observation**, not a spec-violation verdict — a
  projection cannot tell a genuinely non-conforming source from a
  conforming one replayed through an unfiltered loader.
  `ProjectionAnomaly`'s field names, their order, and both `kind` string
  values are a byte-for-byte contract shared with the TypeScript SDK
  (macp-sdk-typescript#55).

### Changed

- **BREAKING: the first accepted Vote or ballot per sender now stands;
  later ones are discarded (issue #43).** `DecisionProjection`'s Vote
  handling and `QuorumProjection._set_ballot` (the shared funnel behind
  `Approve`/`Reject`/`Abstain`) used to overwrite by sender — last write
  wins. **If your code re-sends a Vote or ballot to change your mind, that
  no longer works**: the second envelope is discarded and the first
  answer stands. There is no vote-changing mechanism in this SDK, and we
  are not adding one — changing an already-cast vote would require a
  spec-level Retract/Supersede message with its own cardinality rules,
  which no current RFC defines. The legal way to change an outcome
  differs by mode: for **Decision**, cast a fresh `Vote` under a new
  `proposal_id` (RFC-MACP-0007 §5 rule 1: `proposal_id` MUST be unique
  within the session but a session MAY hold more than one) — or start a
  new session. For **Quorum**, a session accepts at most one
  `ApprovalRequest` (RFC-MACP-0011 §5 rule 1), so a new `request_id` is
  not available within the same session; only a new session works.

  **Motivation: this aligns the SDK with the runtime, which already
  rejects the second Vote/ballot** (`INVALID_ENVELOPE`,
  `macp-runtime/crates/macp-modes/src/mode/decision.rs:217`,
  `mode/quorum.rs:164/184/204`) — it never reaches accepted history, so
  last-wins was unreachable through a conforming runtime and only ever
  fired on hand-built fixtures, captured/edited transcripts, or direct
  `apply_envelope`/`process_event` calls. This is not a response to an RFC
  edit tightening the rule; the runtime already enforced first-wins.

  **Concrete stake:** with `required_approvals=1`, a participant who
  Rejects and then Approves the same request used to reach quorum
  (`has_quorum(request_id)` `True`) and now does not (`False`) — same accepted
  history, different terminal outcome, previously with no signal either
  way.

  A discarded Vote/ballot now appends a `ProjectionAnomaly` (see *Added*
  above) to `proj.anomalies`, so this is no longer silent going forward.
  That anomaly is an **observation** — "a second distinct message of this
  shape was discarded, the first stands" — not a verdict that the source
  transcript violates the spec; a projection cannot tell a genuinely
  non-conforming feed from a conforming one replayed through an unfiltered
  loader.

### Fixed

- **Projections no longer inflate their lists on replay (issue #43).**
  `BaseProjection.apply_envelope` now no-ops when handed an envelope whose
  `message_id` it has already applied. This is a `message_id`-keyed dedup
  guard, not a vote/ballot-cardinality change — it affects callers who never
  touch votes. It closes seven previously-unguarded `.append(` sites: Decision
  `evaluations` / `objections` (and the derived `review_evaluations()` /
  `qualifying_evaluations()` list accessors), Proposal `accepts` /
  `rejections`, and Task `updates` / `completions` / `failures`, plus
  `transcript` itself. Predicate accessors that were already duplicate-
  tolerant by construction (`any(...)`/set/dict-backed, e.g.
  `has_blocking_objection()`, `is_accepted()`, `accepted_proposal()`,
  `is_retryable()`, `latest_progress()`, `progress_of()`) are unaffected —
  they returned the same value before and after this fix.
  The live trigger: `Participant`'s stream transport subscribes with
  `after_sequence` defaulting to `0` (`agent/transports.py:60`), so every
  (re)subscribe replays the full accepted session history, and
  `Participant.run()` (`agent/participant.py:483`) has no re-entry guard — a
  supervisor restarting `run()` after an error re-feeds the whole history into
  the same projection object, previously double- (or N-times-) counting every
  evaluation, objection, accept, rejection, task update, completion, and
  failure it had already recorded.
  A second trigger is doc-taught, not framework-internal: the streaming loop
  in `docs/guides/building-orchestrators.md` ("Pattern: Event-driven
  orchestrator") feeds `stream.responses()` into `session.projection`
  alongside `session.vote()` / `session.approve()`, which already applies
  locally on `ack.ok` — the same double apply, on any orchestrator that
  copied that snippet and also votes through the session it streams. If
  your `evaluations` / `objections` / `accepts` / `rejections` / `updates`
  / `completions` / `failures` counts or `len(transcript)` look inflated
  and you built a coordinator this way, this fix (and the retraction in
  that guide) is why — and after this release it would also have produced
  spurious `duplicate_vote` anomalies had dedup not landed first.
  Two boundaries worth calling out: an empty `message_id` (the proto3 default
  for hand-built envelopes) is never deduplicated — every such envelope is
  applied. And if applying an envelope raises, the `transcript` entry and its
  dedup id are rolled back before the exception propagates, so a caller may
  retry the same envelope — but this rollback covers only `transcript` and
  the dedup set, not subclass-derived state.

### Dependencies

- **grpcio floor relaxed to `>=1.82.0rc2` and `macp-proto` capped `<0.1.8`**
  — grpcio 1.82.0 stable was yanked from PyPI (metadata-only error,
  [grpc/grpc#42906](https://github.com/grpc/grpc/issues/42906)) with no newer
  stable available, making the previous `>=1.82.0` floor unsatisfiable in any
  fresh environment (macp-proto 0.1.8 re-declares that unsatisfiable floor,
  hence the cap). The rc satisfies the gencode's version check; re-widen both
  once a stable grpcio ≥ 1.82.1 ships.
- `grpcio-tools` removed from the `dev` extra — this repo has no
  proto-generation step, and every installable grpcio-tools caps
  `protobuf<7.0.0`, conflicting with the `protobuf>=7.35.0` floor.

### Testing

- Integration tests now **auto-skip** when no runtime is reachable
  (`tests/integration/conftest.py` probes `MACP_RUNTIME_TARGET`, default
  `127.0.0.1:50051`), so a bare `pytest tests/` no longer fails with
  connection errors. The duplicated per-file `_client()` helpers moved into
  that conftest, and `test_modes.py` now honours `MACP_RUNTIME_TARGET`.
- New unit tests: `_logging` (`configure_logging`), retry backoff schedule
  (exponential values + `backoff_max` clamp, no jitter — cross-SDK parity),
  public re-export surface (`__all__` guard), direct `BaseSession` /
  `BaseProjection` contract tests, and compile-only smoke tests for
  `examples/`.
- New integration tests (local-only): positive-path policy-registry
  round-trip (register → get → list → unregister, duplicate-id conflict),
  in-session `send_progress`, and `ListSessions` page-token threading. The
  pagination test self-skips against runtime v0.5.0, which does not implement
  pagination server-side (it ignores `page_size` and returns everything).
- Removed a dead always-skip test; shared unit-test helpers
  (`VALID_SESSION_ID`, `FakeRpcError`, `client_with_stub`) dedupe into
  `tests/conftest.py`.
- Pytest now runs with `--strict-markers` and `filterwarnings = error`;
  `pytest-asyncio` / `asyncio_mode` removed (no async tests).

### CI/CD

- Coverage gate moved to `pyproject.toml` `[tool.coverage.*]` — 85% with
  **branch coverage** — and now applies to `make test` locally, not just CI.
- New reusable `.github/workflows/checks.yml` (lint / typecheck / unit matrix
  3.11–3.13 / conformance) shared by `ci.yml` and `publish.yml`; conformance
  replay now runs on every PR. `ci.yml` gains `twine check`, pip caching,
  concurrency cancel, `workflow_dispatch`, and job timeouts.
- All GitHub Actions SHA-pinned with version comments; `.github/dependabot.yml`
  added (github-actions + pip weekly, `macp-proto` excluded — absorption is
  manual).

## [0.7.0](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/v0.6.0...v0.7.0) (2026-08-30)


### ⚠ BREAKING CHANGES

* build_commitment_ref and build_commitment_payload's supersedes handling now validate commitment_hash against RFC-MACP-0013's canonical shape (sha256: + 64 lowercase hex) via a shared validate_commitment_hash(), raising MacpSessionError on a non-conforming value. Previously both were zero-validation pass-throughs, including when a CommitmentRef was constructed directly and passed as supersedes=, bypassing build_commitment_ref entirely -- that bypass is now closed. A caller previously passing an arbitrary string will now get an exception instead of a wire-invalid CommitmentRef/CommitmentPayload.

### Features

* compute RFC-MACP-0013 canonical commitment hashes and validate CommitmentRef syntax ([#36](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/36)) ([7e975dd](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/7e975dda9b21b3a1a618ea76ac27f69a96ccbcd0))

## [0.6.0](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/v0.5.0...v0.6.0) (2026-07-11)


### Documentation

* **plans:** cross-SDK parity follow-ups + runtime changes needed ([137123f](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/137123f4b8542824e093773c5fcf622037c08b9a))
* **plans:** cross-SDK parity follow-ups and runtime-changes-needed ([e5211b5](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/e5211b5a40a634486805f1ff825159d9ac07f9ce))


### Miscellaneous Chores

* release 0.6.0 ([dad0d7f](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/dad0d7f0103bb90c28b2162b729ed0c80899a1c6))

## 0.5.0 (2026-07-06)

Absorb **runtime v0.5.0** and **`macp-proto 0.1.6`**. Additive API surface for
the new runtime capabilities, plus the canonical conformance-fixture format.

> **⚠️ Consumer-visible dependency bump.** `macp-proto 0.1.6`'s generated code
> was produced by **protobuf 7.35.0 / grpc 1.82.0** and protobuf enforces this
> at *import time*, so this release raises three floors together:
> **`macp-proto>=0.1.6`**, **`protobuf>=7.35.0`**, and **`grpcio>=1.82.0`**.
> Deployments pinned to protobuf 6.x / grpcio < 1.82 cannot upgrade to 0.5.0 —
> there is no way to straddle protobuf majors. Stay on 0.4.x if you must.

### Added

- **`max_suspend_ms` on session start** — `build_session_start_payload`,
  `BaseSession.start`, and the agent `InitiatorConfig` / `start_session` accept
  a per-session maximum-suspension cap (macp-proto ≥ 0.1.5). `0` selects the
  runtime default (7 days); negatives are rejected client-side.
- **`ListSessions` pagination** — `list_sessions()` now auto-paginates (drains
  all pages, returns the complete list) and gains a `page_size` kwarg. New
  `list_sessions_page(page_size, page_token) -> (sessions, next_page_token)`
  for manual paging.
- **Handoff `implicit` accept surfacing** — `HandoffRecord.implicit` and
  `HandoffProjection.is_implicitly_accepted(handoff_id)` distinguish a
  runtime-synthesized implicit accept (RFC-MACP-0010 §5.1) from an explicit
  client accept. `accept_handoff()` never sets `implicit` (regression-tested).
- **multi_round `Contribute` proto encoding** — `ext.multi_round.v1`
  `Contribute` now encodes as `macp.modes.multi_round.v1.ContributePayload`
  (macp-proto ≥ 0.1.4). Legacy JSON payloads are still decoded (tried first,
  permanently). New `build_contribute_payload(value)` helper.
- **`MacpTransportError.code`** — watch/stream transport errors now carry the
  gRPC status code name (e.g. `RESOURCE_EXHAUSTED`, `UNAUTHENTICATED`,
  `FAILED_PRECONDITION`) so callers can branch on lag/auth/precondition.
- **`auth` kwargs on watch RPCs** — `watch_signals`, `watch_policies`,
  `watch_mode_registry`, `watch_roots` accept/forward auth.
- **Ext-mode `Commitment`-terminal guard** — `register_ext_mode` rejects a
  descriptor missing `Commitment` from `terminal_message_types` client-side.

### Changed

- **Dependency floors** — `macp-proto>=0.1.6,<0.2.0`, **`protobuf>=7.35.0`**,
  **`grpcio>=1.82.0`** (see the warning above).
- **`list_sessions()` auto-paginates** — strictly-more-complete results;
  behaviourally additive for single-page runtimes.
- **`QuorumThreshold.value: float → int`** with range validation — a
  `percentage` value must be an integer `0-100`, and any negative value is
  rejected with `MacpSessionError` at build time. Such descriptors were
  already rejected by the runtime's schema (`INVALID_POLICY_DEFINITION`); this
  converts the server-side rejection into an immediate client-side error.
- **`client_version`** default corrected to `0.5.0` (was a stale `0.4.0`).

### Fixed

- **`WatchSignals` authentication** — `MacpClient.watch_signals` and
  `SignalWatcher` now send auth metadata; against a runtime v0.5.0 an
  unauthenticated `WatchSignals` was rejected with `UNAUTHENTICATED`. The
  other watchers' stored `auth` (previously dead code) is now forwarded too.
- **Conformance harness** — re-keyed to fully-qualified canonical
  `payload_type` names (derived from the SDK's own `proto_registry`),
  excludes the vendored `schema.json` from fixture loading, and adds a
  format-guard test. Vendored fixtures resynced to the canonical pack.
- **`after_sequence` / stream docs** — documented the exclusive 1-based
  accepted-envelope ordinal contract, compaction stability, and
  `FAILED_PRECONDITION`-below-compaction recovery; stream errors preserve the
  gRPC code for reconnect handling.
- **Read-only policy registry** — `register_policy` / `unregister_policy` (and
  the ext-mode mutation RPCs) wrap `grpc.RpcError`; a runtime with
  `MACP_POLICIES_DIR` surfaces `FAILED_PRECONDITION` as a typed `MacpAckError`
  instead of a raw gRPC error.
- **`validate_session_id` no-fall-through** — a UUID-shaped string must now be
  a lowercase v4/v7 UUID (no reinterpretation as base64url); 36-char base64url
  IDs containing `-` remain accepted (advisory validator).

## 0.4.1 (2026-07-02)

Patch release: packaging metadata and Decision policy JSON parity fixes. No
runtime-surface or API changes; drop-in over 0.4.0.

### Fixed

- **Packaging metadata** corrected after the repository rename to
  ``macp-sdk-python`` (project URLs / references).
- **Decision policy JSON** now omits an unset ``voting.quorum`` field,
  restoring byte-parity with ``macp-sdk-typescript``. Decision policy fields
  aligned to RFC-0012 v2 and the negative-outcome conformance vector synced.

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
