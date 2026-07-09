# Plan: Runtime changes the SDKs need (ecosystem view)

**Date:** 2026-07-08
**Perspective:** `macp-sdk-python` / `macp-sdk-typescript` maintainers, looking
*down* at `macp-runtime`.
**Companion to:** `plans/cross-sdk-parity-followups.md` §5 (this file is the
detailed version of that stub).
**Runtime state inspected:** `../macp-runtime` @ `9b20791` (branch
`chore/test-ci-improvements`, clean tree) and released tag `v0.5.0`.

Both SDKs thread the full runtime v0.5.0 proto surface and are forward-compatible
with these changes — nothing here *blocks* the SDKs, but each item is a place
where the SDK ships plumbing the runtime does not yet honour. Two of the three
are **already tracked** in the runtime's own `plans/defer/follow_ons.md`; this
plan cross-references that so we don't file duplicate issues, and records exact
file:line evidence + SDK-side impact.

---

## 1. [P0] Implement ListSessions server-side pagination

**Status upstream:** already tracked — `macp-runtime/plans/defer/follow_ons.md`
**#2** ("ListSessions pagination (runtime implementation)", master plan §3.4).
Deliberately deferred out of v0.5.0.

**Evidence.** The proto fields exist and are documented as a server option:
`schemas/proto/macp/v1/core.proto` — `ListSessionsRequest { int32 page_size=1;
string page_token=2; }`, `ListSessionsResponse { repeated SessionMetadata
sessions=1; string next_page_token=2; }` (with a comment: "Servers MAY cap the
effective size; clients MUST NOT assume the response is complete unless
next_page_token is empty"). But the handler ignores both fields:

```rust
// macp-runtime/src/server.rs  (list_sessions, ~:1261)
let sessions = self.runtime.registry.get_all_sessions().await;
let metadata = sessions.iter().map(Self::session_to_metadata).collect();
Ok(Response::new(ListSessionsResponse { sessions: metadata }))
//                                        ^ no next_page_token → always ""
```

So the runtime returns the *entire* session set in one page with an empty
token, regardless of `page_size`.

**Change needed.**
1. In `server.rs::list_sessions`: apply `page_size` (0 = server default cap),
   sort/order sessions deterministically, and emit an opaque `page_token` /
   `next_page_token` continuation. A stale/invalid token must return
   `INVALID_ARGUMENT` (per the proto comment). `get_all_sessions()` lives at
   `crates/macp-storage/src/registry.rs:241` — decide whether to page in the
   registry (preferred for large sets) or slice at the server boundary
   (simpler; acceptable while session counts are small).
2. Apply the same capping to the `watch_sessions` initial-sync snapshot so the
   startup `CREATED` burst is bounded too (follow_ons §2 calls this out).

**SDK-side impact / how we'll know it landed.** Both SDKs already send the
fields and auto-drain (`list_sessions()` / `listSessions()`). The Python SDK
has an integration test that **self-skips** until the runtime honours
`page_size`:
`macp-sdk-python/tests/integration/test_progress_and_pagination.py`
(`TestListSessionsPageTokenThreading`) — it goes live automatically once the
runtime returns a non-empty `next_page_token` for a capped request. That is the
acceptance signal. No SDK change required when this lands; just re-run
integration tests against the new runtime.

---

## 2. [P1] Release the runtime version-string fix

**Status upstream: already fixed on the WIP branch, not yet released.**

- Released `v0.5.0` tag: `src/server.rs:812` returns a **literal**
  `version: "0.4.0"` in `runtime_info`, and `main.rs` logged the same — so the
  0.5.0 runtime self-reports as 0.4.0.
- WIP branch `chore/test-ci-improvements` (`9b20791`) has **already fixed both
  sites** to derive from the crate version:
  - `src/server.rs:814` → `version: env!("CARGO_PKG_VERSION").into()`
    (with a comment at `:813` explaining the prior 0.4.0 bug)
  - `src/main.rs:336` → `"macp-runtime v{} listening", env!("CARGO_PKG_VERSION")`

**Change needed.** None to author — just get `chore/test-ci-improvements`
merged and cut a runtime release/tag so a downloadable build reports `0.5.x`.

**SDK-side impact.** No SDK asserts `runtime_info.version`, so this is not an
SDK correctness issue — but the Python SDK's `CLAUDE.md` and
`plans/runtime-changes-needed.md` (this file) document the "reports 0.4.0"
quirk; delete those notes once a fixed runtime ships. Any dashboard/tooling
keying on the reported version benefits immediately.

---

## 3. [P2] Emit synthetic handoff implicit-accepts (producer side)

**Status upstream:** already tracked — `follow_ons.md` **#1** ("Handoff
implicit-accept timer", RFC-0010 §5.1, master plan §2.5). `macp-proto 0.1.6`
ships `HandoffAcceptPayload.implicit`; the runtime does **not** yet emit these
(it uses an interim in-commitment-handler check, the "A6 interim").

**SDK-side impact.** Both SDKs are already **decode-ready**: they surface
`HandoffRecord.implicit` / `is_implicitly_accepted()` and both strip `implicit`
before encoding so a client never forges one (verified in the parity audit).
This is decode-only forward-compat — nothing in either SDK produces implicit
accepts, so the runtime is the only missing half. Not blocking, but the feature
loop is incomplete until the runtime emits them (sender = target,
`implicit: true`, deterministic `message_id` `implicit-accept:<handoff_id>`,
gated on a new `semantics_rev` per follow_ons #1).

**Change needed.** Runtime-side only; see follow_ons #1 for the full contract.
No SDK change anticipated — the Python unit test
`tests/unit/test_absorb_runtime_v050.py::TestHandoffImplicit` already exercises
a *synthetic* accept to prove the SDK handles a runtime-produced one.

---

## 4. [P3] Roots population (lower priority, not SDK-blocking)

`ListRoots` returns empty and `WatchRoots` idles; since runtime v0.5.0 this is
now *advertised honestly* via `capabilities.roots.list_changed: false`, and
both SDKs handle the empty/idle case. No SDK change wanted here — noting it only
so the audit trail is complete. Track under the runtime's own roadmap, not as an
SDK-driven need.

---

## What we are NOT asking the runtime to change

- **Reject-path NACK codes** — already correct; the runtime conformance oracle
  owns them and the fixtures carry `expected_error_code`. Both SDKs
  deliberately assert only the accepted-prefix / fixture-contract side.
- **Read-only policy-registry `FAILED_PRECONDITION`** — correct runtime
  behaviour; the only open question is the *SDK-side* error class (see
  `cross-sdk-parity-followups.md` §3), not a runtime change.

---

## Priority summary

| # | Runtime change | Priority | Upstream status |
|---|----------------|----------|-----------------|
| 1 | ListSessions server-side pagination | P0 | tracked (follow_ons #2), unimplemented |
| 2 | Release the version-string fix | P1 | **fixed on WIP branch**, needs merge + tag |
| 3 | Emit synthetic handoff implicit-accepts | P2 | tracked (follow_ons #1), unimplemented |
| 4 | Roots population | P3 | runtime roadmap; not SDK-driven |

Only #1 has an SDK acceptance test waiting on it. #2 is the cheapest win
(already coded upstream — just needs a release). Neither #1 nor #3 requires any
SDK code change when it lands — only a re-run of the integration suite against
the newer runtime.
