# DECISIONS

Durable record of reconciled assumptions. Each entry is one `ASSUMPTIONS.md` entry taken
through `/reconcile`: what was assumed, what the recommending agent advised, the verdict,
and the resulting status. `/ship` and later reconciliation passes read this file instead
of replaying the conversation that produced it.

Append-only. An entry is superseded by a later dated entry, never edited in place.

---

## 2026-09-06 — `apply_envelope` rolls back on failure rather than leaving a wedged dedup entry

- **Plan:** `plans/first-wins-vote-cardinality.md`
- **Entry:** `ASSUMPTIONS.md` — "`apply_envelope` rolls back on failure…"
- **Blast radius / tier:** One-way door → **Fable**. `BaseProjection` is exported for
  third-party subclassing (`src/macp_sdk/__init__.py:151`), and this behaviour had already
  shipped in **0.8.0** (PR #45, `070f6f5`) and again in **0.9.0**. Reconciled *post-hoc*:
  a `CHANGE` here would have meant a second behaviour change to a released public contract
  across two versions, not a pre-ship adjustment.

### The assumption

`apply_envelope` records the `message_id` and appends to `transcript` **before** dispatching
the effect. If the effect raises, the envelope is marked-as-seen but unapplied, so every
retry is silently swallowed by the dedup gate — permanent data loss where, pre-dedup, a
retry recovered. Chosen fix: roll back exactly those two mutations and re-raise unchanged.
Deliberately narrower than "atomic" — subclass state and `self.phase` are not restored.

### Recommendation (Fable)

**CONFIRM as-is**, on three findings:

1. **The invariant still holds.** Verified per branch against the current tree rather than
   assumed. This mattered: the first-wins/anomaly code from PR #47/#48 landed *inside* the
   `_apply_mode_message` implementations after the assumption was written. It preserved
   raise-before-mutate in all five modes.
2. **The entry overstated one thing.** It implied the docstring promises a rollback the
   base class does not deliver. It does not — `base_projection.py:145-151` states the
   narrow scope explicitly. Docstring and code agree. Claim withdrawn.
3. **The real gap was documentation placement, not behaviour.** The invariant lived only
   in an internal code comment; `_apply_mode_message`'s abstract docstring — the one
   surface a third-party subclasser reads in source (mkdocstrings' default filters
   exclude underscore members, so it is absent from the published API reference) — said
   nothing about it, and no test
   pinned it. Base-class *enforcement* was assessed as not achievable generically:
   snapshotting `__dict__` fails on slotted subclasses, and deep-copy per envelope is a
   real per-message regression to shipped behaviour.

Also noted: the rollback is **not** dead code. The docstring blesses hand-built fixtures
and captured logs as valid feeds, and those are exactly where a malformed payload is
reachable in production.

### Verdict — confirm, and close the documentation gap in the same pass

Confirmed as-is, with both additive follow-ups applied rather than filed:

- `_apply_mode_message`'s abstract docstring now states the raise-before-mutate contract
  and why the narrow rollback depends on it.
- `tests/unit/test_projection_rollback_invariant.py` pins the parse-then-mutate ordering
  across all 23 dispatch arms, plus seeded Decision and Quorum cases that distinguish
  "rolled back" from "never had any state." It asserts via an unparseable payload, so it
  pins mutate-before-`ParseFromString` specifically — the docstring carries the broader
  rule. A table-exhaustiveness test fails if a projection grows a branch nobody adds a row
  for, and discovers SDK projections by walking `macp_sdk` so a *new* projection with no
  rows fails too.
- Both guards were **mutation-tested**, not merely observed green: moving a mutation ahead
  of the parse fails `decision-Vote` specifically, and dropping a branch row fails the
  exhaustiveness check.
- Stale citations corrected (`__init__.py:143` → `:151`; `task.py:103`/`handoff.py:71` →
  `:102`/`:70`).

Neither follow-up changes the public contract — they pin the property the confirmation
rests on, so the next person to touch `_apply_mode_message` is told the rule by the code
rather than expected to rediscover it.

**Resulting status:** `CONFIRMED (2026-09-06)`

---

## 2026-09-27 — `from_bootstrap` hard-fails on a missing runtime address instead of defaulting to localhost

- **Plan:** `plans/sdk-parity-python-fixes.md` (Phase 2, item 5)
- **Entry:** `ASSUMPTIONS.md` — "`from_bootstrap` hard-fails on a missing runtime
  address…"
- **Blast radius / tier:** Reversible → **Opus** (auto-settled, not brought to the
  user). An internal default-behavior choice on a factory function, not a schema/wire-
  format/auth-model change; the entry's own analysis already noted it's "reversible in a
  commit by restoring the `or 'localhost:50051'` fallback" if a real caller needed it.

### The assumption

`runner.py`'s old `ctx.get("runtime_url") or ctx.get("runtime_address") or
"localhost:50051"` default was judged an accident of `or`-chaining, not a documented or
intentional contract. Chosen fix: raise `ValueError` naming both accepted keys when
neither is present (`runner.py:67-72`), matching the exception type `from_bootstrap`
already raises for a missing bootstrap path.

### Recommendation (Opus)

**CONFIRM as-is**, on:

1. **Code matches the entry exactly** — `runner.py:67-72` raises `ValueError` naming
   both keys, no implicit default remains.
2. **Independent cross-SDK parity evidence, not present when the assumption was first
   written**: `../macp-sdk-typescript/src/agent/runner.ts:81-82` does the identical
   fail-fast (`runtime_address ?? runtime_url ?? ''`, throw if empty) with no localhost
   fallback — an independently-implemented sibling SDK made the same choice, which is
   strong evidence this isn't an accidental Python-only tightening.
3. **No documented example breaks**: `docs/guides/agent-framework.md:55`'s canonical
   bootstrap example already sets `runtime_url` explicitly; `docs/guides/direct-agent-auth.md`
   already treats the key as required. Nothing in the docs relied on the removed default.
4. **Low real-world exposure**: `pyproject.toml:7,16` — version `0.9.1`,
   `Development Status :: 3 - Alpha` — pre-1.0, no stability promise broken.

### Verdict — confirm, no further changes needed

Nothing found during verification weakens the original call; the parity evidence
actively strengthens it. No code or doc follow-up required.

**Resulting status:** `CONFIRMED (2026-09-27)`
