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

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=AuthConfig.for_dev_agent("owner-a"))
session = HandoffSession(client)
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
session.accept_handoff("h1", sender="owner-b")

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

# Offers
proj.offers                      # dict[handoff_id, HandoffOfferRecord]
proj.offers["h1"].target_participant  # "owner-b"
proj.offers["h1"].scope          # "service-xyz-oncall"
proj.offers["h1"].disposition    # "offered" | "accepted" | "declined"

# Active offer (most recent with disposition="offered")
proj.active_offer()              # HandoffOfferRecord or None

# Acceptance/decline
proj.is_accepted("h1")           # True
proj.is_declined("h1")           # False

# Attached context
proj.contexts                    # dict[handoff_id, list[HandoffContextRecord]]
proj.contexts["h1"][0].content_type  # "application/json"

# Lifecycle
proj.phase                       # "Pending" | "OfferPending" | "Accepted" | "Declined" | "Committed"
```

## Handling declines and re-offers

```python
# First target declines
session.decline("h1", reason="on vacation", sender="owner-b")

# Offer to a different target
session.offer("h2", "owner-c", scope="service-xyz-oncall", reason="owner-b unavailable")
session.accept_handoff("h2", sender="owner-c")

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
