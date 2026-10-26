# PROGRESS — Runtime-issued decision receipts client (Wave 5 S1)

Plan: `plans/runtime-receipts.md` (local, gitignored `plans/`). The previous PROGRESS log, for RFC-MACP-0013 commitment hash, is archived at `plans/archive/PROGRESS-rfc-macp-0013-commitment-hash.md`.

## Repo map

- `src/macp_sdk/client.py`:
  - `_ClientCore` holds `server_capabilities` (:400), `_what_if_replay_request` (:583) and `_require_what_if_replay` (:588-603, which reads capabilities via getattr). `_transport_error_from_rpc` and `_require_auth` live on `_ClientCore` too.
  - `initialize` caches capabilities (:755). `get_manifest` (:879) shows the optional-auth pattern. `what_if_replay` (~:1190) shows the lazy-initialize, gate and RpcError mapping pattern.
- `src/macp_sdk/async_client.py`: `initialize` caches capabilities (:274). `what_if_replay` (~:627) is the async twin.
- `src/macp_sdk/receipt.py`:
  - `DecisionReceipt` TypedDict (:91).
  - `verify_receipt(receipt, trusted_keys: Mapping[kid, x])` (:458) never raises.
  - `_decode_b64url` (:161).
  - `__all__` (:494).
- `src/macp_sdk/errors.py`: the code constants (:9-31). `HISTORY_UNAVAILABLE` is the latest, and the new code goes after it.
- `src/macp_sdk/__init__.py`: the receipt imports (:138) and the `errors` imports (~:55), plus `__all__`.
- `pyproject.toml:55`: `macp-proto>=0.1.6,<0.1.13`, which must become `<0.1.14`. The installed version is 0.1.12, so upgrade the dev venv to 0.1.13.
- `tests/parity/test_contract.py`: `_EXPORTED` holds the 21 permanent codes. Do NOT add the provisional `RECEIPT_UNAVAILABLE`.
- `tests/unit/test_what_if_replay.py` is the test pattern to copy (`client_with_stub`, `FakeRpcError` with trailing `macp-error-code`, async clients built inside `asyncio.run`).
- `tests/unit/test_async_client.py:138-172` (`_public_methods`) checks that sync and async signatures match.
- `tests/unit/test_receipt.py:40` has `_vector(name)`, which loads `tests/vectors/receipt/<name>.json`. receipt_010 is the runtime-shaped attested vector, signed with kid `example-1`.
- `tests/vectors/receipt/` is byte-identical to the spec (no new vectors).
- Spec example: `../multiagentcoordinationprotocol/examples/discovery/receipt_keys.json` → `tests/fixtures/receipt_keys.json` (a new dir).
- `.github/workflows/ci.yml:33,36` pins runtime image 0.9.0 (Phase 3). `tests/integration/conftest.py` provides `make_client` and `RUNTIME_TARGET`.
- Docs: `docs/protocol.md` (What-if replay subsection, the model for the new one), `docs/guides/error-handling.md` (`HISTORY_UNAVAILABLE` paragraph), README (receipts section), `CLAUDE.md` (runtime-limitations list).

PR strategy: P1+P2 in one PR now (a coherent public feature, unblocked; release as a minor); P3 as a follow-up PR when runtime R1 ships, because it needs the R1 image.

## Phase status

- Phase 1: DONE
- Phase 1b: DONE
- Phase 2: DONE
- Phase 3: TODO (BLOCKED-ON runtime R1)

## Log

- 2026-10-26 Phase 1 PASS (Opus verifier, 2 rounds; complex: dependency pins and the CI drift gate).
  - Round 1 gaps:
    - The duplicate-kid rule differed from TS. Resolved with the stricter rule, which TS adopted.
    - Explicit null tests were missing.
    - R11 (schema validation of the fixture) was not done.
    - The Makefile used `=` instead of `?=`.
  - Files: Makefile, pyproject.toml, docs/contributing.md, src/macp_sdk/{receipt,errors,__init__}.py, tests/fixtures/receipt_keys.json, tests/unit/{test_receipt_keys,test_fixture_drift_gate}.py, plus local CLAUDE.md.
  - Next: Phase 1b (vendor tests/otel/macp-semconv.json for check-sdk-sync, at the spec session's request), then Phase 2.
- 2026-10-26 Phase 1b PASS (Opus verifier, 1 round; tagged simple and gated alone because Phase 2 is complex). Two optional items were applied: a semconv assertion in test 21, and the workflow header comment. Files: Makefile, tests/otel/macp-semconv.json, tests/unit/test_fixture_drift_gate.py, docs/contributing.md, .github/workflows/conformance-fixtures.yml. Next: Phase 2.
- 2026-10-26 Phase 2 PASS (Opus verifier, 2 rounds; complex because it adds a public client API and touches the gRPC boundary). Round 1 found no code defects; its gaps were RECEIPT_UNAVAILABLE docs overstating when the error clears, async coverage for get_receipt_keys init and errors plus auth, and the shared-name list in the plan. Files: src/macp_sdk/{client,async_client,receipt,__init__}.py, tests/unit/test_receipt_client.py, README.md, docs/{protocol,security}.md, docs/guides/error-handling.md, docs/api/index.md, plus local CLAUDE.md. The gate refactor is a separate commit (9137afe). Next: finalization, then /ship. Phase 3 is BLOCKED-ON runtime R1.
- 2026-10-26 PR #198 opened (P1+P1b+P2; final whole-feature verify PASS).
- 2026-10-26 merged #198. It was rebased on the bot's macp-proto 0.1.13 bump (#197), and only the pin comment conflicted. It was rebase-merged so that each conventional commit drives its own package bump. Release #199 then published macp-sdk-python 0.21.0, langgraph-macp 0.1.2 and claude-agent-sdk-macp 0.1.2 to PyPI. Phase 3 is still BLOCKED-ON runtime R1 and will be a follow-up PR.

---

# S2 — OpenTelemetry send span (Wave 5)

Plan: `plans/otel-send-span.md`. Branch: `feat/otel-send-span`.

## Repo map (S2)
- `src/macp_sdk/client.py`:
  - `_ClientCore._metadata` (~:402) is the single metadata choke point.
  - `MacpClient.send` is at :797.
  - `_send_ack_or_raise` (:519) treats a duplicate as success.
  - `_send_error_from_rpc` (:531) maps a coded status, or the pre-0.9 fallback, to `MacpAckError`.
- `src/macp_sdk/async_client.py`: `AsyncMacpClient.send` is at :283.
- `src/macp_sdk/retry.py`: `retry_send` calls `client.send` once per attempt.
- `tests/otel/macp-semconv.json` (vendored in S1): the `span.macp.send` group is the assertion source.
- `pyproject.toml`: the extras are at :60. Add `otel`, and add the OTel packages to `dev`.
- `integrations/*/pyproject.toml`: the adapter pins are `<0.22`. Widen them to `<0.23` in this PR.

PR strategy: P1+P2 in one PR (one coherent opt-in feature), released as a minor (0.22.0).

## Phase status (S2)
- Phase 1: DONE (2026-10-26)
- Phase 2: DONE (2026-10-26)

## Log (S2)
- 2026-10-26 Contract agreed with macp-sdk-typescript-62 (span name, attributes, outcome and status mapping, propagation, the 9 shared test names).
- 2026-10-26 Phase 1 PASS, verifier Opus (complex: packaging extra plus the metadata path every RPC uses), 2 rounds.
  - Round 1 GAPS, all closed in round 2:
    - untested `inject` and version-fallback branches;
    - `_cache: Any` with a `type: ignore`;
    - no assertion that the test provider is the global provider.
  - Files: `_otel.py` (new), `client.py` `_metadata`, `pyproject.toml` (the `otel` extra and dev), `tests/unit/test_otel.py`. `_otel.py` coverage is 100%.
  - Next: Phase 2.
- 2026-10-26 Phase 2 PASS, verifier Opus (complex: cross-SDK span contract), 2 rounds.
  - Round 1 GAPS, all closed:
    - the baggage claim overstated, given gRPC auto-instrumentation;
    - the README overstated;
    - the cancellation test had no identity check and no real `task.cancel()`;
    - the privacy scan was too narrow;
    - no test for attach failing.
  - Mutation check: 4 deliberate breaks, all caught.
  - Files: `_otel.py`, `client.py`/`async_client.py` `send`, `test_otel.py`, `docs/guides/observability.md` (new), `mkdocs.yml`, `README.md`, `docs/security.md`, local `CLAUDE.md`.
- 2026-10-26 Finalization: integration test `tests/integration/test_otel.py` passes against the CI-pinned runtime (`make test-integration-docker`: 67 passed, 3 skipped). It covers the runtime accepting `traceparent` on unary, Send and session RPCs, and the spans recording real accepted / duplicate / rejected outcomes.
