# Proposal Mode

**Mode URI:** `macp.mode.proposal.v1`
**Status:** provisional
**RFC:** RFC-MACP-0008

Offer/counteroffer negotiation with peer refinement. Designed for bilateral or multilateral negotiations where parties iteratively refine terms until convergence or terminal rejection.

> **Runtime semantics:** convergence detection, counter-proposal supersession, and terminal-rejection handling are defined in [Runtime Modes § Proposal Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#proposal-mode). This page covers the SDK API.

## When to use

Use Proposal mode when agents need to negotiate terms through iterative offers and counteroffers:

- Contract negotiation (price, SLA, terms)
- Resource allocation (budget, capacity, scheduling)
- Configuration agreement (settings, parameters)
- Any bilateral/multilateral negotiation

## Participant model: peer

Participants are **symmetric peers** — all declared participants have equal standing to propose, counter-propose, accept, or reject. There is no designated coordinator role for mode-specific messages (though Commitment still requires an authorized sender).

## Determinism: semantic-deterministic

Same accepted envelope sequence → same negotiation outcome. Convergence, terminal rejection, and withdrawal states are fully determined by the message history.

## Message flow

```
SessionStart
  ↓
Proposal (initial offer)
  ↓
CounterProposal (supersedes previous, iterative)
  ↓
Accept / Reject / Withdraw
  ↓
Commitment → RESOLVED
```

### Key semantics

- **CounterProposal** supersedes a referenced proposal — the original becomes `withdrawn`
- **Accept** records a participant's acceptance of a specific proposal
- **Reject** with `terminal=True` signals a final rejection — no further negotiation
- **Withdraw** removes a proposal from consideration
- Convergence occurs when all participants accept the **same live proposal**

## Authorization & termination

Per-message authorization, the configurable acceptance criterion (`all_parties` / `counterparty` / `initiator`), and counter-proposal round limits are defined in [Runtime Modes § Proposal Mode](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/modes.md#proposal-mode). Override the criterion via a bound policy — see [Runtime Policy](https://github.com/multiagentcoordinationprotocol/macp-runtime/blob/main/docs/policy.md).

## Session helper

```python
from macp_sdk import AuthConfig, MacpClient
from macp_sdk.proposal import ProposalSession

# Per-agent auth configs
coordinator_auth = AuthConfig.for_dev_agent("coordinator")
buyer_auth = AuthConfig.for_dev_agent("buyer")
seller_auth = AuthConfig.for_dev_agent("seller")

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=coordinator_auth)
# The coordinator is a neutral convener, not a negotiating party, so it is
# deliberately left out of `participants`. Proposal mode's default
# acceptance criterion is `all_parties` (runtime: proposal.rs's convergence
# rule), which requires every declared participant to accept the same live
# proposal before commit is possible -- a non-voting coordinator in
# `participants` would make convergence unreachable. The initiator retains
# Commitment authority regardless of participant membership (RFC-MACP-0007 §2).
session = ProposalSession(client, auth=coordinator_auth)
session.start(
    intent="negotiate service contract terms",
    participants=["buyer", "seller"],
    ttl_ms=120_000,
)

# Seller's initial offer
session.propose(
    "p1", "Standard Package", summary="$100k/year, basic SLA", sender="seller", auth=seller_auth
)

# Buyer counter-proposes
session.counter_propose(
    "p2", "p1", "Enhanced Package",
    summary="$80k/year, premium SLA, 24/7 support",
    sender="buyer",
    auth=buyer_auth,
)

# Seller accepts the counter
session.accept("p2", reason="terms acceptable", sender="seller", auth=seller_auth)

# Buyer confirms
session.accept("p2", reason="agreed", sender="buyer", auth=buyer_auth)

# Commit the agreement
proj = session.proposal_projection
if proj.accepted_proposal() == "p2":
    session.commit(
        action="contract.agreed",
        authority_scope="procurement",
        reason="Both parties accepted p2",
    )
```

## Projection queries

```python
proj = session.proposal_projection

# Proposals
proj.proposals                  # dict[str, ProposalRecord]
proj.live_proposals()           # Only proposals with disposition="live"
proj.proposals["p1"].disposition  # "live" | "withdrawn"
proj.proposals["p2"].supersedes   # "p1"

# Accepts
proj.accepts                    # dict[sender, AcceptRecord]
proj.accepted_proposal()        # proposal_id if all accepts agree, else None

# Rejections and withdrawals
proj.terminal_rejections        # list[TerminalRejectRecord]
proj.has_terminal_rejection()   # True if any terminal rejection exists

# Lifecycle
proj.phase                      # "Negotiating" | "TerminalRejected" | "Committed"
proj.is_committed               # True after Commitment
```

## Error cases

| Error | When | How to handle |
|-------|------|---------------|
| `FORBIDDEN` | Sender not a declared participant | Verify sender |
| `INVALID_ENVELOPE` | CounterProposal references non-existent proposal | Check `supersedes_proposal_id` exists |
| `SESSION_NOT_OPEN` | Negotiation already concluded | Check session state |

## Real-world scenario: multi-round negotiation

```python
# Per-agent auth configs
vendor_auth = AuthConfig.for_dev_agent("vendor")
client_auth = AuthConfig.for_dev_agent("client")

# Round 1: Initial offers
session.propose("p1", "Plan A", summary="$50k, 6-month term", sender="vendor", auth=vendor_auth)

# Round 2: Counter
session.counter_propose(
    "p2", "p1", "Plan A Revised", summary="$45k, 12-month term", sender="client", auth=client_auth
)

# Round 3: Final counter
session.counter_propose(
    "p3",
    "p2",
    "Plan A Final",
    summary="$47k, 12-month, quarterly reviews",
    sender="vendor",
    auth=vendor_auth,
)

# Both accept the final version
session.accept("p3", sender="client", auth=client_auth)
session.accept("p3", sender="vendor", auth=vendor_auth)

# At this point, proj.accepted_proposal() == "p3"
```

## API Reference

::: macp_sdk.proposal.ProposalSession

::: macp_sdk.proposal.ProposalProjection
