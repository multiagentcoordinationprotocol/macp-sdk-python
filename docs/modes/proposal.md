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

client = MacpClient(target="127.0.0.1:50051", allow_insecure=True, auth=AuthConfig.for_dev_agent("coordinator"))
session = ProposalSession(client)
session.start(
    intent="negotiate service contract terms",
    participants=["coordinator", "buyer", "seller"],
    ttl_ms=120_000,
)

# Seller's initial offer
session.propose("p1", "Standard Package", summary="$100k/year, basic SLA", sender="seller")

# Buyer counter-proposes
session.counter_propose(
    "p2", "p1", "Enhanced Package",
    summary="$80k/year, premium SLA, 24/7 support",
    sender="buyer",
)

# Seller accepts the counter
session.accept("p2", reason="terms acceptable", sender="seller")

# Buyer confirms
session.accept("p2", reason="agreed", sender="buyer")

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
# Round 1: Initial offers
session.propose("p1", "Plan A", summary="$50k, 6-month term", sender="vendor")

# Round 2: Counter
session.counter_propose("p2", "p1", "Plan A Revised", summary="$45k, 12-month term", sender="client")

# Round 3: Final counter
session.counter_propose("p3", "p2", "Plan A Final", summary="$47k, 12-month, quarterly reviews", sender="vendor")

# Both accept the final version
session.accept("p3", sender="client")
session.accept("p3", sender="vendor")

# At this point, proj.accepted_proposal() == "p3"
```

## API Reference

::: macp_sdk.proposal.ProposalSession

::: macp_sdk.proposal.ProposalProjection
