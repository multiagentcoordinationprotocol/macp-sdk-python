# Plan: Absorb runtime v0.5.0 / macp-proto 0.1.6 / canonical conformance format

**Date:** 2026-07-06
**Repo:** `macp-sdk-python` (currently `0.4.1`, on `macp-proto>=0.1.3,<0.2.0`)
**Upstream (already landed):**
- `macp-runtime` **v0.5.0** (see `../macp-runtime/CHANGELOG.md` §[0.5.0] and
  `../macp-runtime/docs/change-review-phases-a-e.md`)
- **macp-proto 0.1.4 / 0.1.5 / 0.1.6** published on PyPI (verified:
  `pip index` shows `0.1.0 … 0.1.6`)
- Spec repo canonical conformance pack (`../multiagentcoordinationprotocol/schemas/conformance/`)
  rewritten to fully-qualified payload types + `schema.json` + `expected_error_code`
  (spec PRs #49–#52)

This SDK is one of the two ecosystem gates: downstream consumers cannot absorb
the new proto surface until this repo ships a release depending on
`macp-proto >= 0.1.6` and exposing the new fields.

---

## 1. Context — what the SDK provides, with verified evidence

### 1.1 Resolved dependency state (verified from the environment, not docs)

There is **no lockfile** in this repo (no `*.lock`; plain `pip` + `pyproject.toml`).
The resolved state is the dev venv:

- `.venv` has **macp-proto 0.1.3** installed (`pip show macp-proto` → `Version: 0.1.3`;
  `site-packages/macp_proto-0.1.3.dist-info`). The editable install metadata is
  stale at `macp_sdk_python-0.4.0.dist-info` (pyproject says 0.4.1 — cosmetic).
- `pyproject.toml` declares `macp-proto>=0.1.3,<0.2.0` — the range **already
  admits 0.1.6**, but nothing forces it; the floor must move to `>=0.1.6`
  because the SDK will start referencing `macp.modes.multi_round.v1`,
  `SessionStartPayload.max_suspend_ms`, `HandoffAcceptPayload.implicit`, and
  `ListSessionsRequest.page_size/page_token`, none of which exist in 0.1.3
  (verified: installed 0.1.3 `SessionStartPayload` fields end at
  `extensions`; `HandoffAcceptPayload` = `[handoff_id, accepted_by, reason]`;
  `macp.modes.multi_round` import fails).

**Critical empirical finding — hidden transitive floors.** The macp-proto
0.1.6 wheel (downloaded and inspected) declares `protobuf>=5.27.0` and
`grpcio>=1.74.0` in its METADATA, but its gencode was produced by
**protobuf 7.35.0** and **grpc 1.82.0**. Verified in a scratch venv:

- `macp-proto==0.1.6` + `protobuf==6.33.6` → `import macp.v1.core_pb2` raises
  `google.protobuf.runtime_version.VersionError: … gencode 7.35.0 runtime 6.33.6.
  Runtime version cannot be older than the linked gencode version.`
- `macp-proto==0.1.6` + `protobuf==7.35.1` + `grpcio==1.82.0` → all imports OK,
  and the new surface is present:
  - `macp.modes.multi_round.v1.multi_round_pb2.ContributePayload` (`fields=[value]`)
  - `SessionStartPayload.max_suspend_ms` (field 10)
  - `ListSessionsRequest = [page_size, page_token]`, `ListSessionsResponse = [sessions, next_page_token]`
  - `HandoffAcceptPayload = [handoff_id, accepted_by, reason, implicit]`
  - 0.1.6 ships **no `.pyi` stubs** (0.1.3 shipped them) — mypy impact to check
    during execution (the `[[tool.mypy.overrides]] module = "macp.*"` block at
    `pyproject.toml` already sets `ignore_missing_imports = true`, so likely benign).

So this SDK's absorption release must raise **three** floors together:
`macp-proto>=0.1.6,<0.2.0`, `protobuf>=7.35.0`, `grpcio>=1.82.0` — the same
compensation pattern used at 0.1.3 (see the grpcio comment in `pyproject.toml`
and `docs/contributing.md:30`).

**Cross-repo parity gate caveat.** The spec repo's
`scripts/check-proto-parity.sh` rule 3 **fails when the SDK's lower bound is
ahead of the canonical `packages/proto-python` version**. The local spec
checkout's `packages/proto-python/pyproject.toml` still reads `version = "0.1.3"`
even though PyPI has 0.1.6 (releases appear to be cut by CI without committing
the version bump). Additionally the script reads `$MONO_ROOT/python-sdk/pyproject.toml`
— a path that no longer exists after the rename to `macp-sdk-python` — so the
gate is currently inert against this repo. **Action item is coordination, not
code here**: confirm the spec repo's `packages/proto-python` version metadata
is synced to 0.1.6 (and the parity-script paths fixed) in the same window this
SDK bumps, or the gate will fire/misfire when repaired.

### 1.2 SDK surfaces (file:line evidence)

| Surface | Where | Notes |
|---|---|---|
| gRPC client (20 RPCs) | `src/macp_sdk/client.py` (`MacpClient`, 772 lines) | `list_sessions` builds an empty `ListSessionsRequest` and returns `list(resp.sessions)` (491–510); `watch_signals` sends **no auth metadata** (695–707); `watch_policies`/`watch_mode_registry`/`watch_roots` likewise (645–693); `initialize()` (292–304) sends client caps from `_default_capabilities()` (72–84); `client_version` default hardcoded `"0.4.0"` (216) |
| Bidi stream | `src/macp_sdk/client.py` `MacpStream` (87–193); `send_subscribe(session_id, after_sequence=0)` (160–171) | stream errors surface as `MacpTransportError(details)` with **no status code** (146–149, 173–180) |
| Session helpers | `src/macp_sdk/base_session.py` (`start` 96–128, `commit` 130–159) + per-mode files | `start()` has no `max_suspend_ms`; `commit()` echoes `self.policy_version` (default `"policy.default"`, `constants.py:5`) |
| Payload builders | `src/macp_sdk/envelope.py` (`build_session_start_payload` 53–75, `build_commitment_payload` 94–130) | no `max_suspend_ms`; uses `_has_*_field()` descriptor probes for proto-version tolerance |
| Proto type registry | `src/macp_sdk/proto_registry.py` | `MODE_MAP[MODE_MULTI_ROUND]["Contribute"] = "__json__"` (68–70); `_ensure_pb2_imports` (77–93) does **not** import `multi_round_pb2` |
| Projections | `base_projection.py`, `projections.py` (Decision), `proposal.py`, `task.py`, `handoff.py`, `quorum.py` | `HandoffProjection._apply_mode_message` parses `HandoffAcceptPayload` (handoff.py:79–87) but `HandoffRecord` (19–29) has no `implicit`; **no multi_round projection exists** |
| Watchers | `src/macp_sdk/watchers.py` | `SessionLifecycle` already covers all six events (41–79); `SignalWatcher` accepts an `auth` kwarg but **never forwards it** (131–139) — and `client.watch_signals()` has no auth parameter to forward to |
| Agent framework | `src/macp_sdk/agent/` | `GrpcTransportAdapter.start()` subscribes from 0 (transports.py:52–67); `InitiatorConfig` (participant.py:54–72) has no `max_suspend_ms`; `TERMINAL_PHASES` includes `"Accepted"` (participant.py:~48) so a handoff synthetic accept will fire `on_terminal` |
| Policy builders | `src/macp_sdk/policy.py` | `QuorumThreshold(type: str = "n_of_m", value: float = 0)` (146–152) — canonical `quorum-rules.schema.json` types `threshold.value` as **integer**, ≤100 when `type == "percentage"` |
| Validation | `src/macp_sdk/validation.py` | `_UUID_RE` (13) lowercase-canonical shape, no v4/v7 check; `_BASE64URL_RE` (14) `[A-Za-z0-9_-]{22,}` already accepts 36-char base64url with `-`; `validate_participants` (86–95) does **not** require initiator membership |
| Auth | `src/macp_sdk/auth.py` | bearer-only; `for_dev_agent` emits `Authorization: Bearer <agent_id>` (27–43); no `x-macp-agent-id` anywhere in the repo |
| Errors/retry | `errors.py`, `retry.py` | `MacpTransportError` carries no gRPC code; `RetryPolicy.retryable_codes` = `{RATE_LIMITED, INTERNAL_ERROR}` |

### 1.3 Fixture-consumption mechanics (verified)

- Vendored copies live in `tests/conformance/*.json` (13 fixtures). Harness:
  `tests/conformance/test_conformance_projections.py`.
- `make sync-fixtures` copies `../multiagentcoordinationprotocol/schemas/conformance/*.json`
  into `tests/conformance/`; `make verify-fixtures` is a **byte-for-byte drift
  gate** run by CI on every PR/push (`.github/workflows/conformance-fixtures.yml`,
  which also runs the canonical `lint_fixtures.py`).
- **CI is red right now** (or will be on the next run): every one of the 13
  canonical fixtures differs from its vendored copy (verified with `diff -q`
  across all 13 — all `DIFF`), and canonical adds `schema.json` which has no
  local copy (the Makefile's reverse check flags `EXTRA`/missing both ways).
- Canonical format changes (verified by diffing `decision_happy_path.json`):
  - `payload_type` renamed shorthand → fully qualified
    (`decision.Proposal` → `macp.modes.decision.v1.ProposalPayload`,
    `Commitment` → `macp.v1.CommitmentPayload`,
    `multi_round.Contribute` → `macp.modes.multi_round.v1.ContributePayload`)
  - commitment `policy_version` echoes `"policy.default"` (was `""`) — valid
    either way under the new echo contract (runtime A3)
  - reject messages carry `expected_error_code` (runtime-harness-only; this
    SDK's harness skips rejects — `expect != "accept"` at line 134)
  - `schemas/conformance/schema.json` (draft 2020-12) now defines the format
- The harness **breaks on the canonical rename**: `PAYLOAD_BUILDERS`
  (test_conformance_projections.py:32–57) is keyed on the old shorthand and
  `_build_payload` raises `ValueError("Unknown payload_type: …")` for
  fully-qualified names.
- Second breakage: `_load_fixtures()` globs `*.json` (line 105); after
  `make sync-fixtures` copies `schema.json` into `tests/conformance/`,
  the harness would try to treat the JSON Schema as a fixture and crash on
  `fixture["mode"]`.
- multi_round fixtures are **skipped** (no projection registered —
  `MODE_PROJECTIONS.get(mode) is None` → `pytest.skip`, lines 119–124). The
  canonical README says fixtures are "replayed by both SDKs"; the skip is the
  current lawful behavior, and adding a `MultiRoundProjection` is optional
  scope (see task F-4).

---

## 2. Impact matrix — every inventory item

Legend: **IMPACT** = code/docs/tests change here; **NO IMPACT** = evidence-backed no change.

| # | Item | Verdict | Evidence | Action (task ref) |
|---|---|---|---|---|
| 1 | multi_round `ContributePayload` proto (0.1.4) | **IMPACT** | `proto_registry.py:68–70` maps `Contribute → "__json__"` (JSON on the wire); `_ensure_pb2_imports` (77–93) omits `multi_round_pb2`; `tests/unit/test_proto_registry.py:157` pins JSON behavior; agent decode path (`agent/transports.py:140–170`) goes through the registry | B-1: encode Contribute as proto with canonical `payload_type`; decode legacy-JSON-first-then-proto; import the module; update tests |
| 2 | `SessionStartPayload.max_suspend_ms` (0.1.5) | **IMPACT** | `envelope.py:53–75` and `base_session.py:96–128` have no such parameter; `agent/participant.py` `InitiatorConfig` (54–72) and `ParticipantActions.start_session` lack it | B-2: thread `max_suspend_ms: int = 0` through builder → `BaseSession.start` → agent surfaces; reject negatives client-side; document 0 = runtime default (7 days) |
| 3a | `HandoffAcceptPayload.implicit` (0.1.6) | **IMPACT** | `handoff.py:206–210` builds accepts without `implicit` (correct — must stay that way); `HandoffRecord` (19–29) and the `HandoffAccept` branch (79–87) don't surface it | B-3: never set on send (add a regression test pinning this); surface on decode: `HandoffRecord.implicit`, projection captures `p.implicit` |
| 3b | `ListSessions` pagination (0.1.6) | **IMPACT** | `client.py:491–510` sends an empty request and returns `list(resp.sessions)` — silently assumes completeness | B-4: add `page_size`/`page_token`; auto-paginate the existing method; add a single-page variant exposing `next_page_token` |
| 4 | Commitment `policy_version` echo (empty matches bound) | **IMPACT (docs + fixture-adjacent), no code required** | `BaseSession.commit` (130–159) echoes `self.policy_version`; both `start` and `commit` share the same default (`constants.py:5`), so helpers were already self-consistent. Users who pass `policy_version=""` now stop being rejected — a runtime-side relaxation the SDK inherits for free. Runtime review A3 confirms empty-matches / non-empty-must-equal | D-1: document the echo contract in `docs/policy.md`-adjacent pages and `commit()` docstring; canonical decision fixture echoing `policy.default` replays fine |
| 5 | Passive-subscribe `after_sequence` contract | **IMPACT (docs + error surfacing; optional resume helper)** | `client.py:160–171` docstring says "replays … from `after_sequence` onwards" (ambiguous); `docs/guides/streaming.md:52–90` describes 0=start and "17 → resume from 18" — consistent with the new **exclusive** semantics but silent on 1-based accepted-envelope ordinals, compaction stability, and `FAILED_PRECONDITION` below the compacted base; `MacpStream` discards the gRPC status code (`client.py:146–149, 173–180`) so callers can't distinguish `FAILED_PRECONDITION` from other failures; `GrpcTransportAdapter` always subscribes from 0 (transports.py:60) — safe | C-1: docs + docstrings state the contract precisely; preserve status code on stream errors; integration test: resume with `after_sequence=N` must NOT re-deliver envelope N; optional: ordinal tracking on `GrpcTransportAdapter` for reconnect |
| 6a | Watch-stream lag → `RESOURCE_EXHAUSTED` | **IMPACT (error surfacing + docs)** | all watch RPCs collapse `grpc.RpcError` into `MacpTransportError(str(exc))` (client.py:537–538, 656–657, 680–681, 692–693, 706–707) — code lost, reconnect guidance absent | C-2: attach `code` to `MacpTransportError` (or subclass `MacpStreamLagError`); document reconnect loop in `docs/guides/streaming.md` and `watchers.py` docstrings |
| 6b | `WatchSignals` requires authentication | **IMPACT — breaks against v0.5.0 today** | `client.watch_signals` (695–707) has **no auth parameter and sends no metadata**; `SignalWatcher.__init__` takes `auth` but `signals()` calls `self._client.watch_signals()` bare (watchers.py:131–139). Against a 0.5.0 runtime every `WatchSignals` call gets `UNAUTHENTICATED` | C-3: add `auth` kwarg + metadata to `watch_signals` (and, for consistency, `watch_policies` / `watch_mode_registry` / `watch_roots`); make all watcher classes forward their stored `auth` |
| 6c | `SessionLifecycleEvent` six states | **NO IMPACT** | `watchers.py:41–79` already defines CREATED/RESOLVED/EXPIRED/CANCELLED/SUSPENDED/RESUMED predicates + terminal set (added in SDK 0.4.0); enum names derived dynamically via `EventType.Name()` (156–168) so no hardcoded table to extend | verify with a live-runtime integration pass; note the no-more-duplicate-Created fix in docs |
| 7 | Task external orchestrator (initiator ∉ participants) | **IMPACT (docs + test only)** | **No SDK-side initiator∈participants validation exists**: `validation.py:86–95` checks non-empty/dupes/count only; `task.py` has none; `BaseSession.start` has none. The Decision-mode docstring note (`decision.py:26`) remains RFC-correct for Decision (RFC-0007 §2) and must NOT be copied to task | D-2: document the pattern in `docs/modes/task.md` (incl. the ≥1 non-initiator assignee rule); integration test: task session with orchestrator not in participants |
| 8 | Quorum policy `threshold` = approval bar; `percentage` integer 0–100 | **IMPACT** | `policy.py:146–152` types `value: float = 0`; canonical `schemas/json/policy/quorum-rules.schema.json` types `value` as `integer` with `maximum: 100` when `type=="percentage"` — a fractional `0.75` would be rejected at registration (`INVALID_POLICY_DEFINITION`); `docs/modes/quorum.md` describes `required_approvals` but doesn't warn about the percentage convention | B-5: retype to `int`, validate `0–100` for percentage at build time, docstring "threshold is the approval bar, not a participation quorum"; parity check vs macp-sdk-typescript builder |
| 9 | Ext modes: Commitment terminal required; promote-to-`macp.mode.*` rejected; empty `mode_version` binds descriptor version | **IMPACT (docs + optional client guard)** | `client.register_ext_mode`/`promote_mode` (540–581) pass descriptors through unvalidated; `docs/architecture.md:180–199` example already sets `terminal_message_types=["Commitment"]` (good) but documents none of the three constraints | D-3: document all three; optional cheap guard in `register_ext_mode` (raise `MacpSessionError` if `"Commitment"` not in `terminal_message_types`) |
| 10 | Initialize: roots `list_changed:false`; MACP_POLICIES_DIR read-only registry (`register_policy:false`, mutating RPCs → `FAILED_PRECONDITION`) | **IMPACT** | `initialize()` returns the raw response; nothing interprets server caps. `register_policy`/`unregister_policy` (585–613) have **no `grpc.RpcError` handling at all** — a read-only registry surfaces as a raw `grpc._channel._InactiveRpcError`, unlike every other error path in the SDK. `_default_capabilities()` (72–84) advertises the *client's* capabilities and is unaffected by the server-side change | B-6: wrap policy-mutation RPC errors; map `FAILED_PRECONDITION` to a clear `MacpAckError`/`POLICY_REGISTRY_READ_ONLY`-style failure; docs (`docs/architecture.md`, `examples/policy_registration.py` note) on checking `InitializeResponse.capabilities.policy_registry.register_policy` first |
| 11 | HS256 off default JWT allowlist | **NO IMPACT (evidence) + 1-line doc** | repo-wide grep: no HS256 minting, no JWT construction in docs/examples/src; `docs/auth.md:75` defers algorithm/JWKS specifics to the runtime docs by link | D-4 (optional): one sentence in `docs/auth.md` noting HS256 now requires `MACP_AUTH_JWT_ALGS=HS256` opt-in |
| 12 | Dev mode: refuse-to-start without auth unless `MACP_ALLOW_INSECURE=1`; dev auth bearer-only | **NO IMPACT (code) / minor docs** | `auth.py` is already bearer-only (`metadata()` at 73–74 emits only `authorization`); no `x-macp-agent-id` anywhere; CLAUDE.md/docs already instruct `MACP_ALLOW_INSECURE=1` and note the removed header path (CLAUDE.md:44–48, 164–168) | D-5: doc audit — add "runtime refuses to start without auth config unless `MACP_ALLOW_INSECURE=1`; Docker image no longer bakes it in" to CLAUDE.md/README runtime-startup snippets |
| 13 | 36-char base64url session IDs with `-` accepted | **NO IMPACT for the headline case; small alignment gap found** | `_BASE64URL_RE` (`validation.py:14`) permits `-` and ≥22 chars, so the SDK never rejected these. Gap discovered while verifying: the runtime now applies **strict no-fall-through** UUID rules to UUID-*parseable* strings (change review A4) — an uppercase UUID-shaped ID passes the SDK's base64url branch but is rejected by the runtime; the SDK also doesn't check v4/v7. Client validation is advisory (only on explicit `session_id=`), so this is polish, not breakage | D-6 (optional): mirror the no-fall-through rule; add regression tests incl. 36-char base64url with `-` |
| 14 | Canonical conformance fixtures | **IMPACT — CI drift gate is failing** | all 13 vendored fixtures differ byte-wise from canonical; harness keyed on shorthand breaks on canonical names; `schema.json` breaks both `verify-fixtures` (missing local copy) and `_load_fixtures()` glob if synced | A-1/A-2: re-key harness to fully-qualified names, exclude `schema.json` from fixture loading, vendor `schema.json`, `make sync-fixtures`, add a format-guard test |
| 15 | Upcoming: runtime-emitted handoff synthetic accepts in histories | **IMPACT (forward-proofing)** | `HandoffProjection` already processes any accepted `HandoffAccept` (79–87) — a synthetic accept (sender = target, `implicit=true`, `message_id = implicit-accept:<handoff_id>`) flows through the same branch and sets phase `Accepted`; `TERMINAL_PHASES` includes `Accepted` so `Participant.on_terminal` fires. Missing: `implicit` not captured (3a), and no test feeding a synthetic accept | B-3 covers capture + test; note in docs that runtime v0.5.0 does **not yet** emit these (change review A6 explicitly deferred the timer; spec PR #50 defines the contract) — this is compatibility work ahead of the runtime feature |
| 16 | SDK versioning / release | **IMPACT** | version `0.4.1` in `pyproject.toml`; `client.py:216` hardcodes `client_version="0.4.0"` (already stale); `docs/contributing.md:26–46` documents the bump process and the tight-upper-bound policy; parity-script coordination caveat in §1.1 | E-1: release **0.5.0** (minor — additive API, aligned with runtime 0.5.0); floors `macp-proto>=0.1.6,<0.2.0`, `grpcio>=1.82.0`, `protobuf>=7.35.0`; CHANGELOG; bump `client_version`; update contributing note |

---

## 3. Work plan

Ordering rationale: A unblocks CI and needs **no proto bump** (the
fully-qualified names in the harness resolve against pb2 classes that exist in
0.1.3, and multi_round fixtures are skipped before payload construction, so
slice A is independently mergeable even before the dependency bump). B is the
proto bump plus all new-surface work in one PR (the floor move and the code
that needs it belong together). C is v0.5.0 behavioral alignment that does not
need the proto bump but does need a v0.5.0 runtime to integration-test. D is
docs/polish. E is the release.

Effort: S ≈ ≤2h, M ≈ half-day, L ≈ 1–2 days.

### Slice A — conformance fixture resync (mergeable alone; fixes red CI) — **M**

**A-1. Re-key the projection harness to canonical payload types.**
- `tests/conformance/test_conformance_projections.py`: rebuild
  `PAYLOAD_BUILDERS` keyed on fully-qualified names
  (`macp.modes.decision.v1.ProposalPayload`, …, `macp.v1.CommitmentPayload`).
  Prefer deriving the mapping from `macp_sdk.proto_registry.MODE_MAP`/`CORE_MAP`
  values → pb2 classes via `symbol_database` so the harness can't drift from
  the SDK's own registry again (one source of truth).
- Change `_load_fixtures()` to skip non-fixture JSON:
  `glob("*.json")` → filter out `schema.json` (or require a `"mode"` key).
- Add a **format-guard test** (mirrors the runtime's): assert every
  `messages[].payload_type` in every vendored fixture matches
  `^(macp\.v1\.[A-Za-z]+|macp\.modes\.[a-z_]+\.v\d+\.[A-Za-z]+Payload)$`
  (the pattern in canonical `schema.json`) — prevents drift back to shorthand.
- Optional (cheap, recommended): validate each fixture against the vendored
  `schema.json` with `jsonschema` if importable, else skip.
- **DoD:** `make test-conformance` green against the *canonical* files copied
  locally; multi_round fixtures still skip with the existing message.
- **Test:** the suite itself + format-guard.

**A-2. Sync vendored fixtures and vendor `schema.json`.**
- `make sync-fixtures` (copies all canonical `*.json`, now including
  `schema.json` — A-1 must land in the same PR so the loader tolerates it).
- Review `git diff tests/conformance/` — expect payload_type renames,
  `expected_error_code` additions on reject fixtures, decision fixtures'
  commitment `policy_version: "policy.default"`, and the new `schema.json`.
- **DoD:** `make verify-fixtures` exits 0 with
  `SPEC_CONFORMANCE_DIR=../multiagentcoordinationprotocol/schemas/conformance`;
  `python3 ../multiagentcoordinationprotocol/schemas/conformance/lint_fixtures.py`
  passes; `.github/workflows/conformance-fixtures.yml` green.
- **Rollback:** revert the fixture files + harness commit as a unit; CI returns
  to its current red-vs-canonical state, nothing else regresses.

### Slice B — proto 0.1.6 bump + new wire surface — **L**

**B-0. Dependency floors (the gate itself).**
- `pyproject.toml`: `macp-proto>=0.1.6,<0.2.0`; `grpcio>=1.82.0`;
  `protobuf>=7.35.0` — with a comment explaining the gencode-derived floors
  (macp-proto 0.1.6's own METADATA floors are too loose; see §1.1 evidence).
- Refresh `.venv` (`pip install -e ".[dev]" --upgrade`), rerun
  `make lint typecheck test test-conformance`. Watch for mypy fallout from
  0.1.6 dropping `.pyi` stubs (expected benign per existing overrides).
- Update `docs/contributing.md` "Bumping `macp-proto`" 0.1.3 note → 0.1.6 note.
- **DoD:** clean-venv install resolves and imports; full unit suite green.
- **Rollback:** floors revert cleanly as long as B-1..B-4 revert with them
  (they reference 0.1.6-only symbols) — keep slice B one PR.

**B-1. multi_round Contribute goes proto (item 1).**
- `proto_registry.py`:
  - `MODE_MAP[MODE_MULTI_ROUND]["Contribute"] = "macp.modes.multi_round.v1.ContributePayload"`.
  - Add `macp.modes.multi_round.v1.multi_round_pb2` to `_ensure_pb2_imports`.
  - `decode_known_payload` for multi_round Contribute: **try legacy JSON first,
    then proto** — mirroring the runtime's permanent JSON-first acceptance
    order, and because proto3 `ParseFromString` on JSON bytes fails loudly
    while `json.loads` on proto bytes usually fails loudly too; JSON-first
    matches the runtime and keeps old histories/replays decoding identically.
    Keep the existing `_try_decode_utf8` shape (`{"encoding":"json","json":…}`)
    for legacy payloads so current consumers of decoded dicts don't break;
    proto-decoded payloads return `{"value": …}` via `MessageToDict`.
- New clients emit proto: `encode_known_payload(MODE_MULTI_ROUND, "Contribute", {"value": …})`
  now serializes `ContributePayload`. Envelope `payload_type` metadata is not a
  wire field (`Envelope` has no payload_type; the canonical name matters for
  fixtures and registry lookups) — no envelope change needed.
- Update `tests/unit/test_proto_registry.py` (the `__json__` escape-hatch test
  at line 157 becomes: encode→proto bytes round-trip; decode of legacy JSON
  bytes still yields the JSON shape).
- Optional: a tiny `build_contribute_payload(value: str)` helper in
  `envelope.py` + export, for parity with other payload builders.
- **DoD:** encode produces bytes that `ContributePayload.ParseFromString`
  round-trips; decode of `b'{"value":"x"}'` still returns the legacy JSON shape.
- **Tests:** unit round-trips both encodings; integration (Tier: live runtime
  v0.5.0): start `ext.multi_round.v1` session, send proto Contribute, runtime
  accepts; send legacy JSON Contribute, runtime still accepts.

**B-2. `max_suspend_ms` on session start (item 2).**
- `envelope.py::build_session_start_payload`: add `max_suspend_ms: int = 0`;
  raise `MacpSessionError` if negative (runtime rejects negatives at
  SessionStart — fail client-side with a clear message).
- `base_session.py::BaseSession.start`: pass-through kwarg (default 0 =
  runtime default, currently 7 days).
- `agent/participant.py`: add to `InitiatorConfig` and
  `ParticipantActions.start_session`; `agent/runner.py::from_bootstrap` maps a
  bootstrap key if the bootstrap schema carries one (check TS parity; if TS
  doesn't map it, leave bootstrap alone and note it).
- Docs: `docs/guides/session-discovery.md` / mode pages mention the suspension
  cap next to the existing suspend/resume docs; `suspend_session` docstring
  (client.py:405–434) gains "suspension longer than the session-bound
  `max_suspend_ms` expires the session (SUSPENDED→EXPIRED)".
- **DoD:** field set on the wire when non-zero (unit: descriptor check +
  serialized round-trip); default 0 keeps byte-compat with old payloads
  (proto3 scalar default is not serialized).
- **Tests:** unit builder/session/agent plumbing; integration: start with
  small `max_suspend_ms`, suspend, observe EXPIRED lifecycle event after cap.

**B-3. Handoff `implicit` (items 3a + 15).**
- `handoff.py`:
  - `HandoffRecord`: add `implicit: bool = False`.
  - `HandoffAccept` branch: `handoff.implicit = p.implicit`; also tolerate the
    record-missing case (synthetic accept for an offer the projection never
    saw — keep current `if handoff is not None` behavior, but still set
    `self.phase = "Accepted"`, which the code already does).
  - Add `is_implicitly_accepted(handoff_id) -> bool` query helper (parity
    naming with TS to be confirmed at execution).
  - `accept_handoff()` MUST NOT gain an `implicit` parameter — add a comment
    citing RFC-0010 §5.1 (client-submitted `implicit=true` is rejected) and a
    **regression unit test** asserting the built payload has `implicit == False`.
- **DoD:** projection replays a synthetic accept envelope
  (sender = target, `message_id="implicit-accept:h-1"`, payload
  `implicit=True`) → record status `accepted`, `implicit=True`, phase
  `Accepted`; a hand-built client accept keeps `implicit=False`.
- **Tests:** unit as above (this is the item-15 forward-proofing test — note
  in the test docstring that runtime v0.5.0 does not emit these yet);
  `Participant.on_terminal` fires on the synthetic accept (existing
  TERMINAL_PHASES covers `Accepted` — add an explicit test).

**B-4. `ListSessions` pagination (item 3b).**
- `client.py::list_sessions`: keep signature/return type (`list[SessionMetadata]`)
  but **auto-paginate**: loop `ListSessionsRequest(page_size=…, page_token=…)`
  until `next_page_token == ""`; new kwarg `page_size: int = 0` (0 = server
  default). This keeps every existing caller (including
  `watchers`/examples/tests) correct against a paginating runtime.
- Add `list_sessions_page(page_size=0, page_token="") -> tuple[list[SessionMetadata], str]`
  for callers that want manual paging. Export decision: keep it on the client
  only (no package-root export needed).
- Docs: `docs/guides/session-discovery.md` — "don't assume complete lists
  unless `next_page_token` is empty; `list_sessions()` drains pages for you."
- **DoD:** unit test with a stubbed stub returning two pages → concatenated
  result; single-page runtime behavior unchanged (empty token short-circuits).
- **Tests:** unit (stub) + integration: create >page_size sessions with
  `page_size=1` and verify pagination drains (runtime honors page_size ≥ 1).

**B-5. Quorum policy builder alignment (item 8).**
- `policy.py::QuorumThreshold`: `value: int = 0`; in `build_quorum_policy`,
  validate `type == "percentage"` ⇒ `0 <= value <= 100` (raise
  `MacpSessionError`), and document: *threshold is strictly the approval bar —
  there is no participation quorum in schema_version ≤ 2*.
- Keep JSON emission `{"type": t.type, "value": t.value}` (now int → integer
  in JSON, satisfying the canonical schema's `"type": "integer"`).
- Check byte-parity with `macp-sdk-typescript`'s quorum builder before
  changing emission (the repo's own history shows parity is a tracked
  invariant — `plans/policy-builder-json-parity-python-typescript.md`).
- **API-compat note:** narrowing `float → int` is technically breaking for
  callers passing `0.75` — but such descriptors were already rejected by the
  runtime's schema validation, so this converts a server-side
  `INVALID_POLICY_DEFINITION` into an immediate client-side error. Call it out
  in the CHANGELOG.
- **DoD/tests:** unit — percentage 75 accepted, 150 and 0.75 rejected;
  serialized rules JSON has integer `value`; parity fixture if TS has one.

**B-6. Read-only policy registry surfacing (item 10).**
- `client.py::register_policy` / `unregister_policy` (and `promote_mode`,
  `register_ext_mode`, `unregister_ext_mode` for the ext-mode analog): wrap
  `grpc.RpcError`; map `FAILED_PRECONDITION` → `MacpAckError(AckFailure(
  code="FAILED_PRECONDITION", message=<details>))` with a docstring note that a
  runtime configured with `MACP_POLICIES_DIR` advertises
  `policy_registry.register_policy: false` in `Initialize` and refuses all
  mutating policy RPCs.
- Consider a convenience: `MacpClient.server_capabilities` cache from the last
  `initialize()` response, or simply document reading
  `resp.capabilities.policy_registry.register_policy`. Minimum bar: docs +
  error wrapping.
- Docs: `examples/policy_registration.py` gets a short comment;
  `docs/architecture.md` policy section gets the read-only profile note; roots
  note: runtime now advertises `roots.list_changed: false` — `WatchRoots`
  idling is now *advertised* (CLAUDE.md "Runtime limitations" bullet updated).
- **DoD/tests:** unit — stub raising `FAILED_PRECONDITION` → typed error with
  code preserved; integration (runtime with `MACP_POLICIES_DIR`) — register
  refused with clear SDK error, `initialize()` shows `register_policy: false`.

### Slice C — v0.5.0 behavior alignment (no proto dependency) — **M**

**C-1. `after_sequence` contract (item 5).**
- Docstrings: `MacpStream.send_subscribe` (client.py:160–171) — "1-based
  ordinal over *accepted envelopes*, **exclusive** (0 = from start); ordinals
  are contiguous and stable across compaction and restart; resuming below a
  compacted base fails with gRPC `FAILED_PRECONDITION`."
- `docs/guides/streaming.md` §"Session subscription + replay": same contract
  + a resume recipe: count delivered envelopes (or track your own ordinal
  counter) and pass the last-seen count as `after_sequence` on reconnect;
  on `FAILED_PRECONDITION`, restart from 0 and reconcile.
- `docs/auth.md:79` observer paragraph: no change needed (already says
  `after_sequence=0` replays history) — verify wording.
- Optional enhancement (S, separately mergeable): `GrpcTransportAdapter`
  counts delivered envelopes per session and resubscribes with
  `after_sequence=<count>` on stream error — only if we also add reconnect
  logic; today it has none, so defer unless cheap.
- **Tests:** integration — accept N envelopes, subscribe with
  `after_sequence=N-1` → exactly envelope N onward; `after_sequence=N` → live
  only; unit — frame fields already covered (`tests/unit/test_client_stream.py:50–77`).

**C-2. Stream/watch error codes + lag (item 6a).**
- `errors.py`: give `MacpTransportError` an optional `code: str | None`
  attribute (constructor kwarg, default None) — additive.
- `client.py`: every `except grpc.RpcError as exc: raise MacpTransportError(...)`
  in watch/stream paths passes `code=exc.code().name`; `MacpStream.read`
  (173–180) same for the pumped `RpcError`.
- Docs: watchers + streaming pages — "a lagging consumer is terminated with
  `RESOURCE_EXHAUSTED`; reconnect (watchers re-sync: `WatchSessions` replays a
  `CREATED` snapshot without duplicates)".
- Optionally extend `RetryPolicy.retryable_codes` docs to mention that
  `RESOURCE_EXHAUSTED` on a *stream* means reconnect, not retry-send.
- **Tests:** unit — stubbed `RpcError` with `RESOURCE_EXHAUSTED` surfaces
  `code == "RESOURCE_EXHAUSTED"` through `SignalWatcher.signals()` and
  `MacpStream.read()`.

**C-3. `WatchSignals` authentication (item 6b) — highest-urgency code fix.**
- `client.py::watch_signals`: add `auth: AuthConfig | None = None`, call
  `self._require_auth(auth)` and pass `metadata=self._metadata(auth_cfg)`
  (mirror `watch_sessions`, 512–538). Same additive treatment for
  `watch_policies`, `watch_mode_registry`, `watch_roots` (harmless
  server-side; keeps the surface uniform; use `auth or self.auth` **without**
  `_require_auth` for these three if we want them to stay usable
  unauthenticated — decision: require auth only where the runtime does, i.e.
  `watch_signals` uses `_require_auth`, the others pass metadata when
  available).
- `watchers.py`: `SignalWatcher.signals()` → `self._client.watch_signals(auth=self._auth)`;
  same forwarding fix in `PolicyWatcher`/`ModeRegistryWatcher`/`RootsWatcher`
  (their stored `auth` is dead code today).
- **API-compat:** additive kwargs; `client.auth` fallback means existing
  code that configured client-level auth starts working against 0.5.0 with no
  changes.
- **DoD/tests:** unit — metadata captured by a stub contains the bearer
  header; integration — `WatchSignals` with auth receives a signal; without
  auth → `UNAUTHENTICATED` surfaced as `MacpTransportError(code="UNAUTHENTICATED")`.

### Slice D — docs, guards, polish — **M**

- **D-1 (item 4):** echo-contract docs: `BaseSession.commit` docstring +
  `docs/guides/error-handling.md` (commitment version mismatch row) — "empty
  `policy_version` matches the bound policy; non-empty must equal the resolved
  id (e.g. `policy.default`)."
- **D-2 (item 7):** `docs/modes/task.md`: external-orchestrator section
  (initiator may be outside `participants`; pool must contain ≥1 non-initiator
  assignee); integration test in `tests/integration/test_modes.py`.
- **D-3 (item 9):** ext-mode constraints in `docs/architecture.md` (three
  bullets); optional client-side `Commitment`-terminal guard in
  `register_ext_mode` + unit test; `promote_mode` docstring: promoting into
  `macp.mode.*` is rejected.
- **D-4 (item 11):** one-line HS256 opt-in note in `docs/auth.md` JWT section.
- **D-5 (item 12):** CLAUDE.md/README runtime-startup snippets: "runtime 0.5.0
  refuses to start with no auth configured unless `MACP_ALLOW_INSECURE=1`;
  the Docker image no longer bakes that env in" (the existing docker command in
  CLAUDE.md already passes it — verify README's equivalent).
- **D-6 (item 13, optional):** `validate_session_id` no-fall-through: if the
  string is UUID-shaped (36 chars, hyphens at 8-13-18-23), apply strict
  lowercase v4/v7; else base64url ≥22. Unit tests: 36-char base64url with `-`
  accepted; uppercase UUID rejected; v1 UUID rejected. Advisory validator —
  low risk, but skippable if time-boxed.
- **CLAUDE.md sweep:** runtime-version references (0.4.0 → 0.5.0 where they
  describe the *required* runtime), new max_suspend_ms/pagination surface in
  the module inventory, fixtures section mention of `schema.json`.

### Slice E — release — **S**

- **E-1.** Version **0.5.0** in `pyproject.toml`; `client.py:216`
  `client_version: str = "0.5.0"` (fix the stale 0.4.0 while at it — per
  `docs/contributing.md:50` this is part of the minor-release checklist).
- CHANGELOG 0.5.0 entry structured as Added (max_suspend_ms, pagination,
  handoff implicit surfacing, multi_round proto, watch auth, error codes) /
  Changed (dependency floors — call out **protobuf>=7.35 / grpcio>=1.82** as
  the consumer-visible requirement bump; quorum threshold int; auto-paginating
  `list_sessions`) / Fixed (SignalWatcher auth forwarding; conformance harness
  canonical format).
- Confirm `proto-drift.yml` stays green (it upgrades to latest macp-proto —
  after the floor bump it exercises exactly the shipped config).
- Cross-repo: ping spec repo re parity script (stale `python-sdk/` path +
  `packages/proto-python` version metadata 0.1.3 vs PyPI 0.1.6) — rule 3 as
  written will FAIL against our `>=0.1.6` floor until their metadata syncs.
- Tag + publish per `docs/contributing.md` release checklist.

### API-compatibility summary

| Change | Class |
|---|---|
| Dependency floors (macp-proto/grpcio/protobuf) | **Environment-breaking** for consumers on older protobuf/grpcio — headline CHANGELOG item |
| `max_suspend_ms`, pagination kwargs, `auth` kwargs on watch RPCs, `HandoffRecord.implicit`, `MacpTransportError.code` | Additive |
| `list_sessions()` auto-pagination | Behavioral, strictly-more-complete results — additive in practice |
| multi_round Contribute encodes proto instead of JSON | Wire-behavioral; runtime accepts both forever — safe; decoders of raw payload bytes outside the SDK would need the registry (documented) |
| `QuorumThreshold.value: float → int` (+ range check) | Narrowing; only affects descriptors the runtime already rejected |
| No removals or signature breaks | — |

### Test matrix (Definition of Done for the release)

1. `make lint typecheck test` — green on the new floors.
2. `make test-conformance` + `make verify-fixtures` — green against canonical.
3. `make test-integration` against a **runtime v0.5.0** container
   (`docker build ../macp-runtime`, run with `MACP_ALLOW_INSECURE=1`), including
   the new tests: subscribe-resume exclusivity, WatchSignals auth, task
   external orchestrator, multi_round proto+legacy Contribute, pagination,
   `max_suspend_ms` expiry, read-only policy registry (needs a second runtime
   config with `MACP_POLICIES_DIR`).
4. Fresh-venv smoke: `pip install macp-sdk-python==0.5.0` in a clean env →
   import + `examples/decision_smoke.py` against the local runtime.

---

## 4. Sequencing

```
A (fixtures+harness)  ──────────────►  independently mergeable NOW; unblocks red CI; no proto bump
B (proto 0.1.6 bump + new surface) ──►  one PR: floors + B-1..B-6 (they reference 0.1.6 symbols)
C (v0.5.0 behavior alignment)  ─────►  code-independent of B; integration tests need runtime v0.5.0
                                        C-3 (WatchSignals auth) is the most urgent runtime-breakage fix
D (docs/guards/polish) ─────────────►  after B+C so docs describe shipped behavior
E (release 0.5.0)  ─────────────────►  single release absorbing everything
```

- **Proto bump → API surface → fixtures/projections?** Inverted deliberately
  for fixtures: slice A has zero proto dependency (verified: harness classes
  exist in 0.1.3; multi_round fixtures skip before payload build), and the
  drift gate is failing today, so fixtures go **first**. Everything else
  follows proto-bump → API surface → docs.
- **Single-release feasibility: yes.** All items are additive or
  environment-level; nothing needs a deprecation cycle. One `0.5.0` release
  ships the entire absorption. The only external dependency is the spec repo's
  proto-python version-metadata sync for the parity gate (coordination, not a
  blocker for publishing to PyPI).
- **Riskiest item — B-0 dependency floors:** consumers pinned to protobuf 6.x
  cannot upgrade. Mitigation: this is forced by macp-proto 0.1.6's gencode,
  not by us; document loudly; there is no way to support both (gencode
  validation is a hard error, demonstrated in §1.1). Rollback: yank/skip the
  release and stay on 0.4.x + proto 0.1.3 — which is why slice A (CI fix) is
  kept independent of the bump.

---

## Revision log

### Pass 1 — completeness (inventory re-walk + targeted greps)
- Greps run: `payload_type`/mode-string tables (found `proto_registry.py` as a
  second payload-type table beyond the conformance harness — both are now in
  scope, item 1/14); `EVENT_TYPE`/state enums (only `watchers.py` +
  client docstrings — six-state coverage confirmed, item 6c NO IMPACT);
  sequence tracking (`after_sequence` in client/stream/tests/docs;
  `HttpTransportAdapter._last_seq` is a non-MACP HTTP shim — out of scope,
  noted); auth metadata (found `SignalWatcher`'s dead `auth` field and the
  four metadata-less watch RPCs — item 6b widened to all watchers);
  policy builders (found `QuorumThreshold.value: float` vs canonical
  `integer` schema — item 8 confirmed with the actual JSON schema, not just
  the changelog); fixtures (13/13 DIFF + `schema.json` double-breakage:
  Makefile `sync-fixtures` copies `*.json` and the harness globs `*.json` —
  added the loader-exclusion requirement to A-1, which the original inventory
  did not call out).
- Added beyond the inventory: `client.py:216` stale `client_version="0.4.0"`
  (E-1); `register_policy`/`unregister_policy` have no `RpcError` handling at
  all (B-6); macp-proto 0.1.6 drops `.pyi` stubs (B-0 watch item); the
  `conformance-fixtures.yml` CI gate is failing *now*, which re-ordered the
  sequencing (fixtures first).

### Pass 2 — adversarial verification (re-read code behind every claim)
- **Proto version re-verified from the environment**: no lockfile exists —
  claim rewritten to cite `.venv` (`pip show macp-proto` → 0.1.3) and the
  dist-info directory; PyPI availability of 0.1.4–0.1.6 verified via the JSON
  API; 0.1.6 wheel downloaded and inspected (fields listed in §1.1 come from
  executing against the wheel, not from the spec's `.proto` files).
- **Corrected:** an early draft assumed the floor bump was
  `macp-proto`-only. Empirically falsified: importing 0.1.6 under
  protobuf 6.33.6 raises `VersionError` (gencode 7.35.0) — added
  protobuf>=7.35.0 and grpcio>=1.82.0 (from `GRPC_GENERATED_VERSION = 1.82.0`
  in the generated stubs) as first-class floor changes and flagged as the
  riskiest item.
- **Corrected:** item 13 was headed for a plain NO IMPACT; re-reading
  `validation.py:13–14` against runtime change-review A4 exposed the
  uppercase-UUID fall-through divergence (SDK accepts via base64url branch,
  runtime strictly rejects) — recorded as optional D-6 rather than silently
  claiming full alignment.
- **Corrected:** item 7 originally planned "drop SDK-side initiator∈participants
  validation" per the inventory wording — verified none exists
  (`validation.py:86–95`, `task.py`, `base_session.py`), so the action is
  docs+test only; also verified `decision.py:26`'s initiator note must stay
  (RFC-0007 §2 differs from RFC-0009).
- **Verified:** `docs/guides/streaming.md`'s "17 → resume from 18" example
  already matches the new exclusive semantics (the old runtime was the
  nonconforming side), so C-1 is additive documentation, not a rewrite.
- **Verified:** spec-repo parity script paths (`python-sdk/`) are stale post-
  rename and canonical `packages/proto-python` metadata reads 0.1.3 vs PyPI
  0.1.6 — the §1.1 coordination caveat is based on reading the script line by
  line, including its "lower bound ahead of canonical ⇒ FAIL" branch.
- **Verified:** runtime v0.5.0 does **not** yet emit handoff synthetic accepts
  (change-review A6 "What this is NOT" + A7 shows the timer contract was
  deferred upstream) — item 15 reframed as forward-proofing with the
  runtime-emission explicitly marked future.

### Pass 3 — executability (slices, DoD, risk, rollback)
- Re-cut the work into five independently mergeable slices with per-task DoD
  and named tests; verified slice A truly has no proto dependency (harness
  pb2 classes all exist in 0.1.3; multi_round fixtures skip before
  `_build_payload`), which makes the red-CI fix shippable immediately and
  keeps the risky floor bump isolated in one revertable PR (B).
- Expanded the riskiest item (B-0 floors) with the demonstrated failure mode,
  the impossibility of straddling protobuf majors (hard `VersionError`), and a
  rollback path (stay on 0.4.x/0.1.3; slice A independent).
- Added rollback notes to A-2 (fixtures+harness revert as a unit) and made
  B one PR so floors and 0.1.6-symbol references can't be split.
- Tightened B-4 to an auto-paginate-by-default design after checking every
  in-repo `list_sessions` caller assumes completeness; added the two-page stub
  unit test as its DoD.
- Added the fresh-venv install smoke and the second runtime config
  (`MACP_POLICIES_DIR`) to the release test matrix — B-6's integration test is
  unreachable without it.
- Decision recorded in C-3 (require auth strictly only on `watch_signals`,
  pass-when-present elsewhere) so implementers don't have to relitigate it.
