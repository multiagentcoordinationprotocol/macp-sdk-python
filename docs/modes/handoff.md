# Handoff Mode

**Mode URI:** `macp.mode.handoff.v1`
**Status:** provisional
**RFC:** RFC-MACP-0010

Transfer scoped responsibility or authority from one agent (current owner) to another (target participant).

> **Runtime semantics:** the serial-offer constraint and late-context handling are defined in [Runtime Modes § Handoff Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#handoff-mode). This page covers the SDK API.

## When to use

Use Handoff mode when ownership or responsibility needs to transfer between agents:

- On-call rotation (transferring incident ownership)
- Service ownership transfer (team reorganization)
- License or authority delegation
- Escalation from one agent tier to another

## Participant model: delegated

The **current owner** initiates the handoff; the **target participant** accepts or declines. This is an asymmetric, directed transfer.

## Determinism: context-frozen

Determinism depends on the **exact bound context** at SessionStart. Same messages + same frozen context = same outcome. The context captures the authority state being transferred, which must be reproduced exactly for replay.

## Message flow

```
SessionStart (with frozen context)
  ↓
HandoffOffer (owner proposes transfer to target)
  ↓
HandoffContext (supplemental context attached to offer)
  ↓
HandoffAccept / HandoffDecline (target responds)
  ↓
Commitment → RESOLVED
```

### Key semantics

- Multiple serial offers are allowed (if the first is declined, offer to another target)
- HandoffAccept/Decline **must** come from the offer's `target_participant`
- HandoffContext attaches supplemental information (runbooks, credentials, state) to an offer
- Only one Commitment resolves the session

## Authorization & termination

Per-message authorization (only the target can `Accept`/`Decline`) and commitment-readiness rules are defined in [Runtime Modes § Handoff Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#handoff-mode).

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.handoff import HandoffSession

# Per-agent auth configs
owner_a_auth = AuthConfig.for_dev_agent("owner-a")
owner_b_auth = AuthConfig.for_dev_agent("owner-b")

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=owner_a_auth)
session = HandoffSession(client, auth=owner_a_auth)
session.start(
    intent="transfer service-xyz oncall to owner-b",
    participants=["owner-a", "owner-b"],
    ttl_ms=60_000,
    context_id="service-xyz",
)

# Owner-A offers the handoff
session.offer(
    "h1", "owner-b",
    scope="service-xyz-oncall",
    reason="scheduled rotation",
)

# Owner-A attaches context (runbooks, current state, etc.)
session.add_context(
    "h1",
    content_type="application/json",
    context=b'{"runbook": "https://wiki/service-xyz", "recent_incidents": [], "dashboard": "https://grafana/xyz"}',
)

# Owner-B accepts
session.accept_handoff("h1", sender="owner-b", auth=owner_b_auth)

# Owner-A commits the transfer
proj = session.handoff_projection
if proj.is_accepted("h1"):
    session.commit(
        action="handoff.accepted",
        authority_scope="service-ownership",
        reason="owner-b accepted service-xyz oncall",
    )
```

## Projection queries

```python
proj = session.handoff_projection

# Per-handoff current state (offer fields + live settlement)
proj.handoffs                                 # dict[str, HandoffRecord] -- every handoff_id seen
proj.get_handoff("h1")                        # HandoffRecord or None
proj.get_handoff("h1").handoff_id             # "h1"
proj.get_handoff("h1").target_participant     # "owner-b" -- who was offered the handoff
proj.get_handoff("h1").scope                  # "service-xyz-oncall"
proj.get_handoff("h1").reason                 # "scheduled rotation"
proj.get_handoff("h1").sender                 # the HandoffOffer envelope's sender (the owner)
proj.get_handoff("h1").status                 # "offered" | "context_sent" | "accepted" |
                                               # "declined"
proj.get_handoff("h1").context_content_type   # "application/json", or None if no
                                               # HandoffContext arrived
proj.get_handoff("h1").accepted_by            # str or None -- HandoffAcceptPayload.accepted_by
proj.get_handoff("h1").declined_by            # str or None -- HandoffDeclinePayload.declined_by
proj.get_handoff("h1").implicit               # True only for a runtime-synthesized
                                               # implicit accept (RFC-MACP-0010 §5.1)

# Convenience reads
proj.active_offer()                           # HandoffRecord or None -- the last-inserted
                                               # handoff still "offered" or "context_sent"
proj.pending_handoffs()                       # list[HandoffRecord] with status in
                                               # {"offered", "context_sent"}
proj.is_accepted("h1")                        # status == "accepted"
proj.is_declined("h1")                        # status == "declined"
proj.has_accepted_offer()                     # True if ANY handoff is accepted
proj.has_accepted_offer("h1")                 # same as is_accepted("h1")
proj.is_implicitly_accepted("h1")             # accepted AND implicit

# Lifecycle
proj.phase                                    # "Pending" | "OfferPending" |
                                               # "ContextSharing" | "Accepted" |
                                               # "Declined" | "Committed"
proj.is_committed                             # True after Commitment
```

> **There is no separate `offers` or `contexts` collection.** Everything about a
> handoff — the offer's fields, the attached context's content type, and the
> settlement — lives on one `HandoffRecord` in `proj.handoffs`. Only the
> *content type* of a `HandoffContext` is projected; the context **bytes are not
> retained** by the projection, so read them from the `HandoffContext` envelope in
> `proj.transcript` if you need them. And only the **latest** content type survives:
> a second `HandoffContext` for the same `handoff_id` overwrites it.

> **`status` has four values, and `"context_sent"` is the one readers miss.** A
> `HandoffContext` for a still-`"offered"` handoff moves it to `"context_sent"` (and,
> whenever `phase` is `"OfferPending"`, moves `phase` to `"ContextSharing"`), so
> `status == "offered"` is **not** the same test as
> "still pending". Use `active_offer()`, `pending_handoffs()`, `is_accepted()` or
> `is_declined()` rather than comparing `status` to `"offered"`.

> **`phase` is not monotonic — only `"Committed"` is sticky.** Every `HandoffOffer`
> sets `phase` back to `"OfferPending"`, so the decline-and-re-offer flow below moves
> it from `"Declined"` to `"OfferPending"` again. One consequence is worth knowing: the
> agent framework's `Participant` treats both `"Accepted"` and `"Declined"` as terminal
> phases and ends `run()` on entering one, so a decline-then-re-offer sequence is a
> `HandoffSession`-level pattern, not something a single `Participant.run()` loop
> carries through.

> **An accept or decline that arrives after the handoff already settled is silently
> discarded.** `status`, `accepted_by`/`declined_by` and `phase` all keep their
> first-settled values (RFC-MACP-0010 §5 rule 4 / §5.1(4)). Nothing raises and no return value
> changes — the only signal is a `settled_handoff` entry in `proj.anomalies` (inherited
> from `BaseProjection`; see the API reference). By contrast, an accept or decline
> naming a `handoff_id` this projection never saw is a plain no-op that records **no**
> anomaly, deliberately: a projection that joined mid-session may legitimately never
> have seen the offer.

> **`proj.handoffs`, `get_handoff()`, `active_offer()` and `pending_handoffs()` hand
> back the projection's own live record objects, not copies.** Treat them as read-only
> — mutating a returned `HandoffRecord` mutates the projection's internal state
> directly (same as `TaskProjection`'s and `ProposalProjection`'s equivalent
> accessors).

## Handling declines and re-offers

```python
owner_c_auth = AuthConfig.for_dev_agent("owner-c")

# First target declines
session.decline("h1", reason="on vacation", sender="owner-b", auth=owner_b_auth)

# Offer to a different target
session.offer("h2", "owner-c", scope="service-xyz-oncall", reason="owner-b unavailable")
session.accept_handoff("h2", sender="owner-c", auth=owner_c_auth)

# Commit with the second target
session.commit(
    action="handoff.accepted",
    authority_scope="service-ownership",
    reason="owner-c accepted after owner-b declined",
)
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` on HandoffAccept | Sender is not the target participant | Only the named target can accept |
| `FORBIDDEN` on HandoffDecline | Sender is not the target participant | Only the named target can decline |
| `INVALID_ENVELOPE` | Accept/Decline references non-existent handoff_id | Verify the handoff_id |

## API Reference

::: macp_sdk.handoff.HandoffSession

::: macp_sdk.handoff.HandoffProjection
