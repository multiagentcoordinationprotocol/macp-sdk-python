# Plan: Cross-SDK parity & dependency-yank follow-ups

**Date:** 2026-07-08
**Repo:** `macp-sdk-python` (`0.5.0`, branch `chore/test-ci-hardening` / PR #15)
**Sibling:** `macp-sdk-typescript` (`0.5.0`, main)
**Source:** Cross-SDK conformance & parity audit, 2026-07-08 (report artifact
"SDK Parity & Conformance Report"). Full test suites executed in both repos;
conformance fixtures byte-compared across spec `origin/main`, Python, and TS.

This plan tracks the *actionable residue* of that audit — the things that are
not yet fixed. Everything not listed here was verified equal/correct and needs
no work. The headline conclusion: **both SDKs are conformant and at feature
parity for the runtime v0.5.0 surface.** What remains is one time-boxed
dependency wart, one Python-side conformance gap, and two minor cross-SDK
deltas, plus two upstream issues to file.

---

## 0. Verified-equal baseline (no action — context for reviewers)

- All 13 conformance fixtures + `schema.json` are **byte-identical** across
  spec `origin/main` (`141287b`), `macp-sdk-python/tests/conformance/`, and
  `macp-sdk-typescript/tests/conformance/`. Zero extra/missing files. Both
  `verify-fixtures` CI gates diff against the spec on every PR.
- Entire runtime v0.5.0 surface present in both SDKs: pagination plumbing,
  `max_suspend_ms`, handoff implicit-accept (decode-only), multi_round
  canonical proto encode + legacy-JSON-first decode, `watch_signals` auth,
  suspend/resume, subscribe `after_sequence`, 5-mode policy builders /
  sessions / projections, agent framework, watchers.
- **Retry policy is exact parity** — `{RATE_LIMITED, INTERNAL_ERROR}`,
  base `0.1`, max `2.0`, 3 retries, deterministic (no jitter). Python now
  pins the exact schedule with backoff tests (PR #15).
- The `voting.quorum` policy-JSON diff from the earlier (2026-07-01) audit is
  **fixed** (`src/macp_sdk/policy.py:103-109`, comment cites TS parity).
- The `plans/policy-builder-json-parity-python-typescript.md` file referenced
  in older notes was **never committed** to either repo — do not cite it.

---

## 1. [P0 · time-boxed] Re-widen dependency floors when grpcio stabilises

**Context.** grpcio `1.82.0` stable was yanked from PyPI on 2026-07-07
(metadata-only error — bad protobuf range, grpc/grpc#42906) with no newer
stable published. pip skips yanked versions when resolving ranges, so a
`grpcio>=1.82.0` floor is unsatisfiable in any fresh environment. Combined
with `macp-proto 0.1.8` declaring exactly that floor (spec-repo commit
`8bd8c14`, "declare the real dependency floors"), the SDK became uninstallable
from scratch. PR #15 applied the temporary workaround:

- `pyproject.toml:41` — `grpcio>=1.82.0rc2` (a pre-release in the specifier
  lets pip resolve the rc, which satisfies the gencode's
  `GRPC_GENERATED_VERSION=1.82.0` import check).
- `pyproject.toml:55` — `macp-proto>=0.1.6,<0.1.8` (0.1.8's metadata floor is
  unsatisfiable; 0.1.6/0.1.7 declare `grpcio>=1.74` and resolve).
- `grpcio-tools` removed from `[dev]` (unused here; caps `protobuf<7`).

TypeScript is **not** exposed: it ships raw `.proto` files loaded dynamically
via `@grpc/grpc-js` + proto-loader from npm/GitHub Packages — no gencode, no
version gate — and runs proto `0.1.8` today.

**Trigger.** A stable `grpcio >= 1.82.1` is published to PyPI (watch
grpc/grpc#42906), **or** a `macp-proto 0.1.9` ships with an installable floor.

**Actions when triggered.**
1. `grpcio>=1.82.0rc2` → `grpcio>=1.82.1` (or the actual stable).
2. `macp-proto>=0.1.6,<0.1.8` → `macp-proto>=0.1.6,<0.2.0` (re-widen the cap;
   confirm 0.1.8's metadata is now satisfiable, or move the floor to 0.1.9).
3. Delete the two workaround comment blocks in `pyproject.toml` and the
   grpcio-yank paragraph in `docs/contributing.md` ("Bumping macp-proto").
4. Verify in a from-scratch venv: `python -m venv /tmp/v && /tmp/v/bin/pip
   install -e ".[dev]"` resolves and `import grpc, macp, macp_sdk` succeeds;
   `make test` green.
5. Note the re-widening in `CHANGELOG.md`.

**Files:** `pyproject.toml`, `docs/contributing.md`, `CHANGELOG.md`.

### 1a. [P1] Absorb the daily `proto-drift` failure until §1 clears
`.github/workflows/proto-drift.yml` force-upgrades to the latest published
`macp-proto` (`>=0.1.0,<1.0.0`), which now resolves to **0.1.8** and fails at
install with `ResolutionImpossible` — the same wall. This will open a
`proto-drift`/`maintenance` tracking issue daily. **Do not chase it** — it is a
known symptom of §1, not a regression. Optional: add a one-line note to the
issue-body template (or a `continue-on-error` guard scoped to the install step)
referencing this plan so the noise is self-explanatory. Remove any such guard
when §1 lands.

---

## 2. [P1] Close the multi_round conformance gap (Python trails TS)

**Finding.** The Python conformance harness **skips** the two `multi_round`
fixtures (`tests/conformance/test_conformance_projections.py:139-143`) because
no SDK projection exists for `ext.multi_round.v1`. The TypeScript harness
**replays them with real assertions** via a transcript-only `BaseProjection`
subclass (`macp-sdk-typescript/tests/conformance/conformance.test.ts:82-93`,
landed post-0.5.0). This is the only place where the two SDKs' conformance
coverage genuinely differs, and Python is the one behind.

**Action.** Port the TS approach: a minimal transcript-only projection (append
every accepted envelope, no mode-specific state) registered for
`ext.multi_round.v1`, so the two fixtures replay and assert transcript length,
commitment detection, and `expected_resolution` scalars like every other mode.
Do **not** build a full multi_round session/projection surface — that is out of
scope; the goal is conformance-replay parity, not a new public API.

**Files:** `tests/conformance/test_conformance_projections.py` (unskip +
transcript-only projection). Verify: `make test-conformance` replays 40, skips 0.

---

## 3. [P2] Decide the read-only-registry error shape cross-SDK

**Delta.** Registering against a read-only policy registry surfaces as:
- Python: `MacpAckError(code="FAILED_PRECONDITION")`
- TS: `MacpTransportError` with `.code === 'FAILED_PRECONDITION'`

Same code string (callers keying on the code are portable today), different
error **class**. This is a cross-SDK API-shape decision, not a bug in either.

**Action.** Agree one canonical class with the TS maintainer(s) and align.
Recommendation: keep it an *ack*-layer error where the runtime returned a
NACK/ack with the code (Python's shape), since the read-only refusal is a
gRPC `FAILED_PRECONDITION` status the SDK maps — confirm which layer the
runtime actually uses and standardise there. Low urgency; do only alongside
other error-mapping work. **Cross-repo — needs TS agreement before touching
`src/macp_sdk/client.py`'s `_map_registry_mutation_error`.**

---

## 4. [P3] Cosmetic policy-JSON diff (`minimum_confidence`)

The single surviving byte-level policy-builder diff: Python emits
`minimum_confidence: 0.0`, TS emits `0`; Python's `json.dumps` also uses
`", "`/`": "` separators vs TS's compact `JSON.stringify`, so `rules` strings
are not byte-identical regardless. Both schema-valid and semantically equal.

**Action.** Only worth doing if anything ever byte-compares `rules` across
SDKs (nothing does today). If pursued: emit an int when the confidence is a
whole number *and* match separators — but note the separator difference alone
defeats byte-equality, so this is all-or-nothing and probably not worth it.
**Recommend: WONTFIX unless a concrete byte-compare consumer appears.** Logged
here so the audit trail is closed rather than silently dropped.

---

## 5. [P1] Runtime-side changes the SDKs need → see dedicated plan

The audit surfaced runtime changes (not SDK changes) that both SDKs are already
forward-compatible with. Detailed in **`plans/runtime-changes-needed.md`**;
summary of the corrected upstream status (checked against `../macp-runtime`
`9b20791` and tag `v0.5.0`):

1. **ListSessions pagination not implemented server-side.** The handler
   (`macp-runtime/src/server.rs::list_sessions` ~:1261) ignores `page_size`/
   `page_token` and returns everything with an empty token. **Already tracked**
   upstream as `macp-runtime/plans/defer/follow_ons.md` #2 — not a new issue to
   file. Python's pagination integration test self-skips until it lands.
2. **Runtime self-reports version `"0.4.0"` at the `v0.5.0` tag**
   (`src/server.rs:812` literal). **Already fixed** on the runtime's WIP branch
   `chore/test-ci-improvements` (`server.rs:814` now uses
   `env!("CARGO_PKG_VERSION")`) — just needs merge + a release. No new issue.
3. **Synthetic handoff implicit-accepts** are decode-ready in both SDKs but the
   runtime doesn't emit them yet — tracked as follow_ons #1.

**Action.** No new issues needed (both real items are already in the runtime's
defer list). Instead: nudge the runtime's `chore/test-ci-improvements` branch
to merge+release for the version fix, and reference `runtime-changes-needed.md`
when prioritising the pagination work. Revisit this SDK's docs/CLAUDE.md
"reports 0.4.0" and pagination-caveat notes once a fixed runtime ships.

---

## Priority summary

| # | Item | Priority | Blocked on |
|---|------|----------|-----------|
| 1 | Re-widen grpcio / macp-proto floors | P0 (time-boxed) | stable grpcio ≥ 1.82.1 upstream |
| 1a | Absorb proto-drift noise meanwhile | P1 | — |
| 2 | multi_round conformance replay | P1 | — (self-contained) |
| 5 | Runtime changes (→ `runtime-changes-needed.md`) | P1 | upstream runtime merge/release |
| 3 | Read-only-registry error shape | P2 | TS-maintainer agreement |
| 4 | `minimum_confidence` JSON cosmetic | P3 | likely WONTFIX |

Only §2 is pure new SDK work with no external blocker; do it first. §1 is the
most important but waits on upstream — the workaround is holding and CI is
green in the meantime.
