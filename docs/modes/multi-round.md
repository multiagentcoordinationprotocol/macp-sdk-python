# Multi-Round Mode

**Mode URI:** `ext.multi_round.v1`
**Status:** runtime-shipped extension (not standards-track), `mode_version` `1.0.0`
**Spec:** the normative comment block in [`multi_round.proto`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/schemas/proto/macp/modes/multi_round/v1/multi_round.proto) and the example [`examples/multi-round-session.json`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/examples/multi-round-session.json)

Declared participants contribute string values, changing them as often as they
like, until every participant's latest value is the same. The initiator then
commits. Use it when agents need to settle on one value by repeated
adjustment: an option, a parameter, a label.

The helper is client sugar only. The runtime enforces every rule, and the
projection's convergence is advisory. No governance policy is evaluated for this
mode: commit authority is initiator-only whatever the bound policy says.

## Rules the runtime enforces

| Rule | Runtime result |
|---|---|
| SessionStart needs `mode_version` `1.0.0` and at least one participant | `INVALID_ENVELOPE` |
| Only a declared participant may `Contribute`, including the initiator unless it is listed | `FORBIDDEN` |
| An empty value encodes to zero bytes and is refused | `INVALID_ENVELOPE` |
| A sender's first value, or a changed value, advances the round; re-sending the same value does not | accepted |
| Convergence (`all_equal`): every participant has contributed and all latest values are equal | sticky |
| Any `Contribute` after convergence, even the converged value | `INVALID_ENVELOPE` |
| Convergence does not resolve the session; it stays OPEN until a Commitment | — |
| Only the initiator may commit | `FORBIDDEN` |
| A Commitment before convergence | `INVALID_ENVELOPE` |

The round counts value changes across all participants. It is not a synchronized step.

## Example

```python
from macp_sdk import AuthConfig, MacpClient, MultiRoundSession

coordinator = MacpClient(target="127.0.0.1:50051", allow_insecure=True,
                         auth=AuthConfig.for_dev_agent("coordinator"))
session = MultiRoundSession(coordinator)
session.start(intent="Converge on one rollout option",
              participants=["alice", "bob"], ttl_ms=300_000)

def as_(agent: str) -> dict:
    return {"sender": agent, "auth": AuthConfig.for_dev_agent(agent)}

session.contribute("option_a", **as_("alice"))  # round 1
session.contribute("option_b", **as_("bob"))    # round 2
session.contribute("option_a", **as_("bob"))    # round 3: converged

proj = session.multi_round_projection
assert proj.converged and proj.converged_value == "option_a"
proj.round  # 3
proj.phase  # "Pending" | "Contributing" | "Converged" | "Committed"

session.commit(authority_scope="rollout", reason="All participants agree")
```

`AsyncMultiRoundSession` has the same methods as coroutines.

## Session helper

- **`start(...)`** is the shared `start`. It refuses a `mode_version` other than
  `"1.0.0"` before sending (`MULTI_ROUND_MODE_VERSION`).
- **`contribute(value, *, sender=None, auth=None)`** sends a `ContributePayload`
  as canonical protobuf. It raises `MacpSessionError` before sending when the
  value is empty, when the projection has converged, or when the projection
  knows the participants and the sender is not one of them.
- **`commit(*, authority_scope, reason, action="multi_round.converged",
  commitment_id=None, outcome_positive=None, supersedes=None, sender=None,
  auth=None)`** fills `mode_version`, `configuration_version` and
  `policy_version` from the session and generates `commitment_id` when it is
  omitted. It raises `MacpSessionError` before sending when:
    - the projection knows the initiator and the sender is someone else;
    - `action`, `authority_scope`, `reason`, or an explicit `commitment_id` is
      blank;
    - `outcome_positive` contradicts the action's suffix (negative: `rejected`,
      `failed`, `declined`; positive: `selected`, `accepted`, `completed`,
      `approved`; case-sensitive, as the runtime matches);
    - `supersedes` has a blank `session_id` or a non-canonical
      `commitment_hash`.

    `multi_round.converged` (`MULTI_ROUND_CONVERGED_ACTION`) matches no suffix,
    so its `outcome_positive` is unconstrained; it defaults to `True`. `commit()`
    does not wait for convergence: the runtime checks that and answers
    `INVALID_ENVELOPE`.

## Projection

`MultiRoundProjection` tracks:

| Attribute | Meaning |
|---|---|
| `participants` | The declared participants, from SessionStart |
| `initiator` | The SessionStart sender, `None` until seen (a SessionStart with an empty sender keeps it) |
| `contributions` | The latest value per participant |
| `round` | The number of value changes so far |
| `converged` | Whether all participants agree (sticky) |
| `converged_value` | The agreed value, `None` until converged |

`phase` moves `"Pending"` → `"Contributing"` (SessionStart) → `"Converged"` →
`"Committed"`.

A projection that never sees the SessionStart (a participant joining mid-session)
can be seeded: `MultiRoundProjection(participants=[...], initiator="...")`. A later
SessionStart overwrites both. Without participants, convergence is never
evaluated.

A `Contribute` from a non-participant, one after convergence, and one that
decodes to nothing are no-ops. The first two never appear in accepted history.

The value is decoded exactly as the runtime's `parse_contribute_value` decodes
it. Legacy JSON is read the way serde reads `{"value": <string>}`: an object with
one `value` string, or a one-element array holding it. Only the top-level keys
and the value itself must be free of lone surrogates, and other members are
syntax-checked but otherwise ignored. That JSON wins unless the bytes are also
the canonical protobuf encoding of the same message, the tie-break the proto
specifies. Everything else is read the way prost reads it. The last `value`
field wins, invalid UTF-8 in any `value` field is refused, and so is a wrong
wire type. Bytes that neither reader accepts decode to nothing. A non-empty
payload that decodes to `""` is a real contribution, as it is in the runtime.
Among payloads whose value is empty, only a zero-byte one is refused, which is
why `contribute("")` raises.

**A session's own projection needs a sender it can resolve.** It is fed the
envelopes the session built, and under bearer auth without a `sender_hint` those
carry an empty sender: the runtime stamps the authenticated sender only on its
own copy. Such Contributes are accepted by the runtime but dropped by the local
projection. Pass `sender=`, use `AuthConfig.for_bearer(token, sender_hint=...)`,
or follow the session stream instead. The projection cannot see a session's `semantics_rev`, so for a session
recorded below revision 3, a protobuf value of byte length 13, 32 or 123 decodes
here under the tie-break, not the JSON-first reading the runtime used then.
