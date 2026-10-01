from __future__ import annotations

import importlib
import warnings

from macp.modes.proposal.v1 import proposal_pb2
from macp.v1 import core_pb2

from macp_sdk.constants import MODE_PROPOSAL
from macp_sdk.proposal import ProposalProjection
from tests.conftest import make_envelope


class TestProposalProjection:
    def _proj(self) -> ProposalProjection:
        return ProposalProjection()

    def test_initial_state(self):
        p = self._proj()
        assert p.phase == "Negotiating"
        assert not p.is_committed
        assert p.accepted_proposal() is None

    def test_proposal(self):
        p = self._proj()
        env = make_envelope(
            MODE_PROPOSAL,
            "Proposal",
            proposal_pb2.ProposalPayload(proposal_id="p1", title="Plan A", summary="first"),
            sender="alice",
        )
        p.apply_envelope(env)
        assert "p1" in p.proposals
        assert p.proposals["p1"].status == "open"

    def test_late_terminal_reject_after_commitment_does_not_regress_phase(self):
        """Issue #93 item 5: a mode message arriving after Commitment must
        not move ``phase`` back out of "Committed" -- exercised end-to-end
        here rather than only via the synthetic projection in
        test_base_projection.py.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1", action="commit", authority_scope="session"
                ),
            )
        )
        assert p.phase == "Committed"
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", reason="late", terminal=True),
                sender="bob",
            )
        )
        assert p.phase == "Committed"
        assert p.proposals["p1"].status == "rejected"  # the reject's own effect is not suppressed

    def test_counter_proposal_does_not_retire_original(self):
        """Counter-proposal does NOT retire the original — both stay live."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "CounterProposal",
                proposal_pb2.CounterProposalPayload(
                    proposal_id="p2", supersedes_proposal_id="p1", title="B"
                ),
                sender="bob",
            )
        )
        assert p.proposals["p1"].status == "open"
        assert p.proposals["p2"].status == "open"
        assert len(p.live_proposals()) == 2

    def test_accept_convergence(self):
        p = self._proj()
        for sender in ["alice", "bob"]:
            p.apply_envelope(
                make_envelope(
                    MODE_PROPOSAL,
                    "Accept",
                    proposal_pb2.AcceptPayload(proposal_id="p1", reason="agreed"),
                    sender=sender,
                )
            )
        assert p.accepted_proposal() == "p1"

    def test_accept_does_not_set_record_status(self):
        """By design (issue #112): an Accept never writes ProposalRecord.status.

        Acceptance is a per-sender, supersedable relation (RFC-MACP-0008 §5
        rule 5) tracked in ``accepts`` / ``_latest_accept_by_sender``, not a
        per-proposal fact. Mirrors the runtime (ProposalDisposition is
        {Live, Withdrawn}) and typescript-sdk's projections/proposal.ts.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        for sender in ["alice", "bob"]:
            p.apply_envelope(
                make_envelope(
                    MODE_PROPOSAL,
                    "Accept",
                    proposal_pb2.AcceptPayload(proposal_id="p1"),
                    sender=sender,
                )
            )
        assert p.accepted_proposal() == "p1"
        assert p.proposals["p1"].status == "open"
        assert p.proposals["p1"] in p.active_proposals()

    def test_accept_divergence(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Accept",
                proposal_pb2.AcceptPayload(proposal_id="p1"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Accept",
                proposal_pb2.AcceptPayload(proposal_id="p2"),
                sender="bob",
            )
        )
        assert p.accepted_proposal() is None

    def test_terminal_rejection_of_known_proposal(self):
        """Issue #119: a terminal Reject only ends the negotiation (moves
        ``phase`` to "TerminalRejected") when it names a proposal this
        projection has actually seen (RFC-MACP-0008 §5 rule 3).
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", terminal=True, reason="no deal"),
                sender="bob",
            )
        )
        assert p.has_terminal_rejection()
        assert p.phase == "TerminalRejected"
        assert p.proposals["p1"].status == "rejected"

    def test_terminal_rejection_of_unknown_proposal_does_not_move_phase(self):
        """Issue #119: a terminal Reject naming a proposal_id this projection
        never saw must not flip ``phase`` to "TerminalRejected" -- that phase
        is in agent/participant.py's TERMINAL_PHASES, and moving into it ends
        a live Participant.run() loop for a session that never actually
        terminated. typescript-sdk's projections/proposal.ts already fixes
        this identically.

        The rejection is still recorded (audit trail is unconditional, and
        has_terminal_rejection()/is_terminally_rejected() deliberately still
        read self.rejections, not phase) -- only the phase transition and the
        (nonexistent) record mutation are gated on the proposal being known.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(
                    proposal_id="p-unknown", terminal=True, reason="no deal"
                ),
                sender="bob",
            )
        )
        assert p.phase == "Negotiating"
        assert "p-unknown" not in p.proposals
        assert len(p.rejections) == 1
        assert p.rejections[0].terminal is True
        assert p.has_terminal_rejection() is True
        assert p.is_terminally_rejected("p-unknown") is True
        assert p.anomalies == []

    def test_terminal_rejection_unknown_then_known_moves_phase_on_second(self):
        """Edge case (issue #119): an unknown-id terminal Reject followed by
        a known-id one is an ordering a replay could produce. The first
        leaves phase unmoved; the second moves it, exactly as it would if it
        had arrived alone.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p-unknown", terminal=True, reason="n/a"),
                sender="bob",
            )
        )
        assert p.phase == "Negotiating"
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", terminal=True, reason="no deal"),
                sender="bob",
            )
        )
        assert p.phase == "TerminalRejected"
        assert p.proposals["p1"].status == "rejected"
        assert len(p.rejections) == 2

    def test_rejection_audit_trail(self):
        """Both terminal and non-terminal rejections are tracked."""
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", terminal=False, reason="maybe not"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Reject",
                proposal_pb2.RejectPayload(proposal_id="p1", terminal=True, reason="no deal"),
                sender="bob",
            )
        )
        assert len(p.rejections) == 2
        assert p.rejections[0].terminal is False
        assert p.rejections[1].terminal is True
        assert sum(1 for r in p.rejections if r.terminal) == 1

    def test_accept_supersession_same_sender(self):
        """A later Accept from the same sender supersedes an earlier one from
        them (RFC-MACP-0008 §5 rule 5) — the sender's old choice stops
        counting, even though the audit trail keeps both.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Accept",
                proposal_pb2.AcceptPayload(proposal_id="p1"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Accept",
                proposal_pb2.AcceptPayload(proposal_id="p2"),
                sender="alice",
            )
        )
        assert p.is_accepted("p1") is False
        assert p.is_accepted("p2") is True
        assert p.accepted_proposal() == "p2"
        # Audit trail keeps both entries, unmodified, in delivery order.
        assert [a.proposal_id for a in p.accepts] == ["p1", "p2"]
        assert all(a.sender == "alice" for a in p.accepts)

    def test_withdraw(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Proposal",
                proposal_pb2.ProposalPayload(proposal_id="p1", title="A"),
                sender="alice",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_PROPOSAL,
                "Withdraw",
                proposal_pb2.WithdrawPayload(proposal_id="p1", reason="changed mind"),
                sender="alice",
            )
        )
        assert p.proposals["p1"].status == "withdrawn"
        assert len(p.live_proposals()) == 0


class TestReplayIdempotence:
    """Regression coverage for issue #43 Phase 2 — replay inflation.

    Separate bug from vote/ballot cardinality: BaseProjection.apply_envelope's
    message_id dedup guard (Phase 1) also fixes seven previously-unguarded
    ``.append(`` sites across Decision/Proposal/Task, including this file's
    ``accepts`` (proposal.py:97) and ``rejections`` (proposal.py:109).

    Real-world trigger: src/macp_sdk/agent/transports.py:60 subscribes with
    after_sequence defaulting to 0, so every (re)subscribe replays the full
    accepted history, and Participant.run() (participant.py:483) has no
    re-entry guard — a supervisor restarting run() re-feeds the whole history
    into the same projection object.

    | Test type   | Requires                         | How                                   |
    |-------------|-----------------------------------|----------------------------------------|
    | Redelivery  | the SAME non-empty message_id     | reuse the same envelope object, or an  |
    |             |                                    | explicit shared message_id=            |
    | Distinctness| two DIFFERENT non-empty ids       | two make_envelope(...) calls (default) |

    Distinctness is not exercised by this class — that coverage lives in
    tests/unit/test_base_projection.py::TestIdempotentApply::
    test_distinct_message_ids_both_applied.

    Every test below is a redelivery test, so every one reuses the same
    envelope object — a test that calls make_envelope twice gets two
    different uuid4 message_ids, dedup never engages, and the test would
    pass while proving nothing.
    """

    def _proj(self) -> ProposalProjection:
        return ProposalProjection()

    def test_redelivered_accept_is_noop(self):
        # Trigger: agent/transports.py:60 (after_sequence=0 full replay) +
        # participant.py:483 (run() has no re-entry guard).
        p = self._proj()
        env = make_envelope(
            MODE_PROPOSAL,
            "Accept",
            proposal_pb2.AcceptPayload(proposal_id="p1", reason="looks good"),
            sender="alice",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        assert len(p.accepts) == 1
        assert len(p.transcript) == 1

    def test_redelivered_reject_is_noop(self):
        # Trigger: agent/transports.py:60 + participant.py:483.
        p = self._proj()
        env = make_envelope(
            MODE_PROPOSAL,
            "Reject",
            proposal_pb2.RejectPayload(proposal_id="p1", terminal=False, reason="no deal"),
            sender="bob",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        assert len(p.rejections) == 1
        assert len(p.transcript) == 1

    def test_replayed_supersession_is_deterministic(self):
        """Replaying the same two-Accept transcript twice (dedup by
        message_id) must not double-apply the supersession — the final
        state after redelivery matches the state after a single delivery.
        """
        p = self._proj()
        first = make_envelope(
            MODE_PROPOSAL,
            "Accept",
            proposal_pb2.AcceptPayload(proposal_id="p1"),
            sender="alice",
        )
        second = make_envelope(
            MODE_PROPOSAL,
            "Accept",
            proposal_pb2.AcceptPayload(proposal_id="p2"),
            sender="alice",
        )
        p.apply_envelope(first)
        p.apply_envelope(second)
        # Redeliver both (same envelope objects — same message_ids).
        p.apply_envelope(first)
        p.apply_envelope(second)
        assert p.accepted_proposal() == "p2"
        assert p.is_accepted("p1") is False
        assert len(p.accepts) == 2
        assert len(p.transcript) == 2


class TestDeprecatedAliases:
    """Issue #103: ``RejectRecord``/``AcceptRecord`` -> ``ProposalRejectRecord``/
    ``ProposalAcceptRecord``, kept as deprecated lazy aliases.

    ``macp_sdk.proposal`` is a plain module (no ``__path__``): a `from ...
    import OldName` resolves via a single ``getattr`` call, one warning.
    ``macp_sdk`` (top-level) is a package: CPython's import machinery probes
    it with an internal ``hasattr`` call before the statement's own getattr,
    so the same import shape fires the module's ``__getattr__`` twice —
    verified empirically in Phase 1, see ``src/macp_sdk/__init__.py``'s own
    alias comment. Both counts are asserted here, not "exactly one"
    uniformly.
    """

    def test_reject_record_alias_from_proposal_module(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.proposal import RejectRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 1
        assert "ProposalRejectRecord" in str(deprecation_warnings[0].message)
        proposal_module = importlib.import_module("macp_sdk.proposal")
        assert RejectRecord is proposal_module.ProposalRejectRecord

    def test_accept_record_alias_from_proposal_module(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.proposal import AcceptRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 1
        assert "ProposalAcceptRecord" in str(deprecation_warnings[0].message)
        proposal_module = importlib.import_module("macp_sdk.proposal")
        assert AcceptRecord is proposal_module.ProposalAcceptRecord

    def test_reject_record_alias_from_macp_sdk_package(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk import RejectRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 2
        assert all("ProposalRejectRecord" in str(w.message) for w in deprecation_warnings)
        macp_sdk = importlib.import_module("macp_sdk")
        assert RejectRecord is macp_sdk.ProposalRejectRecord

    def test_accept_record_alias_from_macp_sdk_package(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk import AcceptRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 2
        assert all("ProposalAcceptRecord" in str(w.message) for w in deprecation_warnings)
        macp_sdk = importlib.import_module("macp_sdk")
        assert AcceptRecord is macp_sdk.ProposalAcceptRecord

    def test_repeated_plain_attribute_access_warns_each_time(self):
        proposal_module = importlib.import_module("macp_sdk.proposal")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = proposal_module.RejectRecord
            _ = proposal_module.RejectRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 2

    def test_unrecognized_name_still_raises_attribute_error(self):
        proposal_module = importlib.import_module("macp_sdk.proposal")
        macp_sdk = importlib.import_module("macp_sdk")
        try:
            _ = proposal_module.TotallyBogusName
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")
        try:
            _ = macp_sdk.TotallyBogusName
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")
