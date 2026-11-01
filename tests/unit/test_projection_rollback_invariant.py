"""Pins the raise-before-mutate invariant every ``_apply_mode_message`` owes.

``BaseProjection.apply_envelope``'s rollback restores ``transcript`` and the
``message_id`` dedup set only — it cannot restore subclass state, because the
base class has no way to know what a subclass mutated. That narrow rollback is
*sufficient* only because every concrete projection does its one fallible
operation (``ParseFromString``) strictly before any mutation. Break that
ordering and the projection half-applies while ``apply_envelope`` reports a
clean rollback and invites a retry.

Until this module existed, that invariant lived only in a code comment in
``base_projection.py`` — asserted in prose, pinned by nothing. It was
re-verified by hand when the first-wins/anomaly logic landed (PR #47/#48 added
code *inside* these very methods), which is exactly the kind of check that
should not depend on someone remembering to do it.

The test feeds each mode branch a payload that cannot parse and asserts the
projection's **entire** observable state is byte-for-byte what it was before
the call. That covers both halves at once: the base rollback (transcript,
dedup set) and the subclass invariant (no collection touched, ``phase``
unmoved). A branch that mutated before parsing fails here.
"""

from __future__ import annotations

import copy

import pytest
from google.protobuf.message import DecodeError
from macp.modes.decision.v1 import decision_pb2
from macp.modes.quorum.v1 import quorum_pb2

from macp_sdk.base_projection import BaseProjection
from macp_sdk.constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_MULTI_ROUND,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)
from macp_sdk.handoff import HandoffProjection
from macp_sdk.multi_round import MultiRoundProjection
from macp_sdk.projections import DecisionProjection
from macp_sdk.proposal import ProposalProjection
from macp_sdk.quorum import QuorumProjection
from macp_sdk.task import TaskProjection
from tests.conftest import make_envelope

# Bytes chosen to fail protobuf parsing rather than to look like any
# particular payload: 0xff is not a valid wire-format tag byte, so
# ParseFromString raises DecodeError for every message type below. Note
# protobuf3 tolerates *unknown fields*, so ordinary version skew does NOT
# raise -- only genuinely corrupt bytes do, which is why this is the shape
# the test uses.
MALFORMED = b"\xff\xfe\x00not-a-parseable-payload"

# Every (mode, message_type) branch across all five concrete projections.
# Keep this list exhaustive: a new branch in any _apply_mode_message without
# a row here is an unpinned opportunity to break the invariant.
BRANCHES = [
    (DecisionProjection, MODE_DECISION, "Proposal"),
    (DecisionProjection, MODE_DECISION, "Evaluation"),
    (DecisionProjection, MODE_DECISION, "Objection"),
    (DecisionProjection, MODE_DECISION, "Vote"),
    (QuorumProjection, MODE_QUORUM, "ApprovalRequest"),
    (QuorumProjection, MODE_QUORUM, "Approve"),
    (QuorumProjection, MODE_QUORUM, "Reject"),
    (QuorumProjection, MODE_QUORUM, "Abstain"),
    (TaskProjection, MODE_TASK, "TaskRequest"),
    (TaskProjection, MODE_TASK, "TaskAccept"),
    (TaskProjection, MODE_TASK, "TaskReject"),
    (TaskProjection, MODE_TASK, "TaskUpdate"),
    (TaskProjection, MODE_TASK, "TaskComplete"),
    (TaskProjection, MODE_TASK, "TaskFail"),
    (HandoffProjection, MODE_HANDOFF, "HandoffOffer"),
    (HandoffProjection, MODE_HANDOFF, "HandoffContext"),
    (HandoffProjection, MODE_HANDOFF, "HandoffAccept"),
    (HandoffProjection, MODE_HANDOFF, "HandoffDecline"),
    (ProposalProjection, MODE_PROPOSAL, "Proposal"),
    (ProposalProjection, MODE_PROPOSAL, "CounterProposal"),
    (ProposalProjection, MODE_PROPOSAL, "Accept"),
    (ProposalProjection, MODE_PROPOSAL, "Reject"),
    (ProposalProjection, MODE_PROPOSAL, "Withdraw"),
    (MultiRoundProjection, MODE_MULTI_ROUND, "SessionStart"),
]

# Branches that deliberately treat an undecodable payload as a no-op instead of
# raising: the multi_round proto lets a history reader return "nothing
# decodable" for a Contribute (agreed with macp-sdk-typescript). The envelope
# stays in the transcript; no derived state may change.
NO_OP_BRANCHES = [
    (MultiRoundProjection, MODE_MULTI_ROUND, "Contribute"),
]


def _malformed(mode: str, message_type: str, sender: str = "alice"):
    """An envelope whose payload bytes cannot parse for any message type."""
    env = make_envelope(mode, message_type, decision_pb2.ProposalPayload(), sender=sender)
    env.payload = MALFORMED
    return env


def _state(proj) -> dict:
    """Deep snapshot of every attribute the projection holds.

    Uses ``vars()`` rather than an explicit attribute list on purpose: a new
    collection added to a projection is covered automatically, instead of
    silently escaping a hand-maintained list.
    """
    return copy.deepcopy(vars(proj))


@pytest.mark.parametrize(
    ("factory", "mode", "message_type"),
    BRANCHES,
    ids=[f"{m.rsplit('.', 2)[-2]}-{t}" for _, m, t in BRANCHES],
)
def test_malformed_payload_leaves_no_state_behind(factory, mode, message_type):
    proj = factory()
    before = _state(proj)

    env = _malformed(mode, message_type)
    with pytest.raises(DecodeError):
        proj.apply_envelope(env)

    assert _state(proj) == before, (
        f"{factory.__name__}._apply_mode_message mutated state before parsing "
        f"{message_type!r}. apply_envelope's rollback cannot restore subclass "
        f"state -- do all fallible work (ParseFromString) before any mutation."
    )
    # The dedup set specifically must have released the id, or the retry
    # below would be swallowed as a redelivery rather than applied.
    assert env.message_id not in proj._seen_message_ids


@pytest.mark.parametrize(
    ("factory", "mode", "message_type"),
    NO_OP_BRANCHES,
    ids=[f"{m.rsplit('.', 2)[-2]}-{t}" for _, m, t in NO_OP_BRANCHES],
)
def test_malformed_payload_is_a_no_op_on_no_op_branches(factory, mode, message_type):
    proj = factory()
    before = _state(proj)
    env = _malformed(mode, message_type)
    proj.apply_envelope(env)
    after = _state(proj)
    assert after.pop("transcript") == [env]
    for state in (before, after):
        state.pop("_seen_message_ids")
    before.pop("transcript")
    assert after == before


def test_decision_retains_seeded_state_across_a_failed_vote():
    """The pristine-projection case above cannot distinguish "rolled back"
    from "never had anything." This seeds real derived state first, then
    fails a Vote through the branch PR #47/#48 rewrote."""
    proj = DecisionProjection()
    proj.apply_envelope(
        make_envelope(
            MODE_DECISION,
            "Proposal",
            decision_pb2.ProposalPayload(proposal_id="p1", option="opt-a"),
        )
    )
    proj.apply_envelope(
        make_envelope(
            MODE_DECISION,
            "Vote",
            decision_pb2.VotePayload(proposal_id="p1", vote="approve"),
            sender="alice",
        )
    )
    assert proj.votes["p1"]["alice"].vote == "approve"
    before = _state(proj)

    with pytest.raises(DecodeError):
        proj.apply_envelope(_malformed(MODE_DECISION, "Vote", sender="bob"))

    assert _state(proj) == before
    # Alice's vote is intact and bob never became a voter.
    assert proj.votes["p1"]["alice"].vote == "approve"
    assert "bob" not in proj.votes["p1"]
    assert proj.anomalies == []


def test_quorum_retains_seeded_state_across_a_failed_ballot():
    """Same, for the ``_set_ballot`` funnel PR #48 introduced. ``_set_ballot``
    calls ``setdefault`` on ``self.ballots`` *before* its duplicate check, so
    it is the one place a mutation sits close to the branch logic -- but the
    parse still precedes the call, so a malformed ballot must not create the
    request key."""
    proj = QuorumProjection()
    proj.apply_envelope(
        make_envelope(
            MODE_QUORUM,
            "ApprovalRequest",
            quorum_pb2.ApprovalRequestPayload(
                request_id="r1", action="deploy", required_approvals=2
            ),
        )
    )
    proj.apply_envelope(
        make_envelope(
            MODE_QUORUM,
            "Approve",
            quorum_pb2.ApprovePayload(request_id="r1", reason="lgtm"),
            sender="alice",
        )
    )
    assert proj.approval_count("r1") == 1
    before = _state(proj)

    with pytest.raises(DecodeError):
        proj.apply_envelope(_malformed(MODE_QUORUM, "Approve", sender="bob"))

    assert _state(proj) == before
    assert proj.approval_count("r1") == 1
    assert "bob" not in proj.ballots["r1"]
    # The malformed ballot carried no parseable request_id, so _set_ballot's
    # setdefault must not have run and invented an empty-string request key.
    assert "" not in proj.ballots
    assert proj.anomalies == []


def _sdk_projections() -> list[type]:
    """Every ``BaseProjection`` subclass the SDK ships, discovered not listed.

    Walks ``macp_sdk`` so a projection added in a *new module* is found even
    though nothing here imports it -- a hardcoded tuple would let a sixth
    projection arrive with zero BRANCHES rows and pass in silence. Test
    doubles are excluded by module: pytest imports the whole suite into one
    session, so the fakes in ``test_base_projection.py`` are visible here.
    """
    import importlib
    import pkgutil

    import macp_sdk

    for mod in pkgutil.walk_packages(macp_sdk.__path__, macp_sdk.__name__ + "."):
        importlib.import_module(mod.name)

    def descendants(cls: type):
        for sub in cls.__subclasses__():
            yield sub
            yield from descendants(sub)

    return sorted(
        {c for c in descendants(BaseProjection) if c.__module__.startswith("macp_sdk.")},
        key=lambda c: c.__name__,
    )


def test_branch_table_covers_every_projection_and_dispatch_arm():
    """Guards the table above against a projection growing a new branch -- or
    a whole new projection arriving -- that nobody adds a row for. That is the
    failure mode that would let this module quietly stop being exhaustive."""
    import inspect

    factories = _sdk_projections()
    rows = BRANCHES + NO_OP_BRANCHES
    assert {f for f, _, _ in rows} == set(factories), (
        f"BRANCHES covers {sorted({f.__name__ for f, _, _ in rows})} but macp_sdk "
        f"ships {sorted(c.__name__ for c in factories)}. A projection with no rows "
        f"is an unpinned opportunity to break the invariant."
    )

    for factory in factories:
        source = inspect.getsource(factory._apply_mode_message)
        # Every dispatch arm is a literal comparison against message_type.
        arms = {
            line.split('== "')[1].split('"')[0]
            for line in source.splitlines()
            if '== "' in line and ("message_type ==" in line or "mt ==" in line)
        }
        covered = {t for f, _, t in rows if f is factory}
        assert arms == covered, (
            f"{factory.__name__} dispatch arms {sorted(arms)} != BRANCHES rows "
            f"{sorted(covered)}. Add the missing row(s) so the invariant stays pinned."
        )
