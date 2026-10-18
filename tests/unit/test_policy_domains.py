"""Policy-builder domain checks and S1 extension/annotation keys.

Runtime >= 0.9.0 closes every rule object and refuses an off-domain value at
RegisterPolicy (INVALID_POLICY_DEFINITION); the builders refuse it first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from macp_sdk.errors import MacpSessionError
from macp_sdk.policy import (
    AbstentionRules,
    CommitmentRules,
    CounterProposalRules,
    EvaluationRules,
    HandoffAcceptanceRules,
    ObjectionHandlingRules,
    ProposalAcceptanceRules,
    QuorumThreshold,
    RejectionRules,
    TaskAssignmentRules,
    TaskCompletionRules,
    VotingRules,
    build_decision_policy,
    build_handoff_policy,
    build_proposal_policy,
    build_quorum_policy,
    build_task_policy,
)

_BUILDERS = {
    "decision": build_decision_policy,
    "quorum": build_quorum_policy,
    "proposal": build_proposal_policy,
    "task": build_task_policy,
    "handoff": build_handoff_policy,
}


def _rules(desc: Any) -> dict[str, Any]:
    return json.loads(desc.rules)


class TestOffDomainValuesRaise:
    @pytest.mark.parametrize("mode", sorted(_BUILDERS))
    @pytest.mark.parametrize("authority", ["initiator", "anyone", "", "INITIATOR_ONLY"])
    def test_commitment_authority(self, mode, authority):
        with pytest.raises(MacpSessionError, match=r"commitment\.authority"):
            _BUILDERS[mode]("p", "d", commitment=CommitmentRules(authority=authority))

    @pytest.mark.parametrize("quorum_type", ["n_of_m", "weighted", ""])
    def test_voting_quorum_type(self, quorum_type):
        with pytest.raises(MacpSessionError, match=r"voting\.quorum_type"):
            build_decision_policy("p", "d", voting=VotingRules(quorum_type=quorum_type))

    @pytest.mark.parametrize("value", [-1, -0.5, float("nan"), float("inf"), True])
    def test_voting_quorum_value(self, value):
        with pytest.raises(MacpSessionError, match=r"voting\.quorum_value"):
            build_decision_policy("p", "d", voting=VotingRules(quorum_value=value))

    @pytest.mark.parametrize("value", [0, -1, 1.5, True])
    def test_veto_threshold(self, value):
        with pytest.raises(MacpSessionError, match=r"objection_handling\.veto_threshold"):
            build_decision_policy(
                "p", "d", objection_handling=ObjectionHandlingRules(veto_threshold=value)
            )

    @pytest.mark.parametrize("action", ["block", "decline", "HOLD"])
    def test_critical_objection_action(self, action):
        with pytest.raises(MacpSessionError, match=r"critical_objection_action"):
            build_decision_policy(
                "p",
                "d",
                objection_handling=ObjectionHandlingRules(critical_objection_action=action),
            )

    @pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), True])
    def test_minimum_confidence(self, value):
        with pytest.raises(MacpSessionError, match=r"evaluation\.minimum_confidence"):
            build_decision_policy("p", "d", evaluation=EvaluationRules(minimum_confidence=value))

    @pytest.mark.parametrize("interpretation", ["reject", "abstain", ""])
    def test_abstention_interpretation(self, interpretation):
        with pytest.raises(MacpSessionError, match=r"abstention\.interpretation"):
            build_quorum_policy("p", "d", abstention=AbstentionRules(interpretation=interpretation))

    @pytest.mark.parametrize("criterion", ["majority", "all", ""])
    def test_proposal_acceptance_criterion(self, criterion):
        with pytest.raises(MacpSessionError, match=r"acceptance\.criterion"):
            build_proposal_policy("p", "d", acceptance=ProposalAcceptanceRules(criterion=criterion))

    @pytest.mark.parametrize("value", [-1, 2.0, True])
    def test_counter_proposal_max_rounds(self, value):
        with pytest.raises(MacpSessionError, match=r"counter_proposal\.max_rounds"):
            build_proposal_policy("p", "d", counter_proposal=CounterProposalRules(max_rounds=value))

    @pytest.mark.parametrize("value", [-1, 10.5, False])
    def test_handoff_implicit_accept_timeout_ms(self, value):
        with pytest.raises(MacpSessionError, match=r"implicit_accept_timeout_ms"):
            build_handoff_policy(
                "p", "d", acceptance=HandoffAcceptanceRules(implicit_accept_timeout_ms=value)
            )


class TestBoundaryValuesAccepted:
    def test_decision_boundaries(self):
        for mc in (0.0, 1.0):
            build_decision_policy(
                "p",
                "d",
                voting=VotingRules(quorum_type="percentage", quorum_value=0),
                objection_handling=ObjectionHandlingRules(veto_threshold=1),
                evaluation=EvaluationRules(minimum_confidence=mc),
            )

    def test_zero_counts_accepted(self):
        build_proposal_policy("p", "d", counter_proposal=CounterProposalRules(max_rounds=0))
        build_handoff_policy(
            "p", "d", acceptance=HandoffAcceptanceRules(implicit_accept_timeout_ms=0)
        )

    @pytest.mark.parametrize("authority", ["initiator_only", "any_participant"])
    def test_every_authority_accepted(self, authority):
        for build in _BUILDERS.values():
            build("p", "d", commitment=CommitmentRules(authority=authority))

    @pytest.mark.parametrize("interpretation", ["neutral", "implicit_reject", "ignored"])
    def test_every_interpretation_accepted(self, interpretation):
        build_quorum_policy("p", "d", abstention=AbstentionRules(interpretation=interpretation))

    @pytest.mark.parametrize("criterion", ["all_parties", "counterparty", "initiator"])
    def test_every_criterion_accepted(self, criterion):
        build_proposal_policy("p", "d", acceptance=ProposalAcceptanceRules(criterion=criterion))

    @pytest.mark.parametrize("action", ["deny", "finalize_decline", "hold"])
    def test_every_critical_objection_action_accepted(self, action):
        build_decision_policy(
            "p", "d", objection_handling=ObjectionHandlingRules(critical_objection_action=action)
        )


class TestExtensionsAndAnnotations:
    @pytest.mark.parametrize("mode", sorted(_BUILDERS))
    def test_extension_and_annotation_emitted_at_top_level(self, mode):
        desc = _BUILDERS[mode](
            "p",
            "d",
            extensions={"x-com.acme.audit": {"k": 1}},
            annotations={"_note": "x", "$comment": "y"},
        )
        rules = _rules(desc)
        assert rules["x-com.acme.audit"] == {"k": 1}
        assert rules["_note"] == "x"
        assert rules["$comment"] == "y"
        # Emitted after the standard sections, extensions before annotations.
        assert list(rules)[-3:] == ["x-com.acme.audit", "_note", "$comment"]

    @pytest.mark.parametrize(
        ("extensions", "match"),
        [
            ({"x-Acme": {}}, "must match"),
            ({"x-acme": {}}, "must match"),
            ({"acme.audit": {}}, "must match"),
            ({"x-com.acme": 1}, "must be an object"),
            ({"voting": {}}, "collides"),
            ({"commitment": {}}, "collides"),
        ],
    )
    def test_bad_extension_raises(self, extensions, match):
        with pytest.raises(MacpSessionError, match=match):
            build_decision_policy("p", "d", extensions=extensions)

    @pytest.mark.parametrize(
        ("annotations", "match"),
        [({"note": "x"}, "must start with"), ({"threshold": 1}, "collides")],
    )
    def test_bad_annotation_raises(self, annotations, match):
        with pytest.raises(MacpSessionError, match=match):
            build_quorum_policy("p", "d", annotations=annotations)

    def test_empty_mappings_change_nothing(self):
        assert (
            build_task_policy("p", "d", extensions={}, annotations={}).rules
            == build_task_policy("p", "d").rules
        )


_GOLDEN = json.loads((Path(__file__).parent / "golden" / "policy_builders.json").read_text())

_CASES = {
    "decision_default": lambda: build_decision_policy("p", "d"),
    "decision_full": lambda: build_decision_policy(
        "p",
        "d",
        voting=VotingRules(
            algorithm="weighted",
            threshold=0.6,
            quorum_type="percentage",
            quorum_value=50,
            weights={"a": 2.0, "b": 1.0},
        ),
        objection_handling=ObjectionHandlingRules(
            critical_severity_vetoes=True, veto_threshold=2, critical_objection_action="hold"
        ),
        evaluation=EvaluationRules(minimum_confidence=0.7, required_before_voting=True),
        commitment=CommitmentRules(
            authority="designated_role",
            designated_roles=["lead"],
            require_vote_quorum=True,
            allow_decline_over_approval=True,
        ),
        schema_version=2,
    ),
    "quorum_default": lambda: build_quorum_policy("p", "d"),
    "quorum_full": lambda: build_quorum_policy(
        "p",
        "d",
        threshold=QuorumThreshold(type="percentage", value=75),
        abstention=AbstentionRules(counts_toward_quorum=True, interpretation="implicit_reject"),
        commitment=CommitmentRules(authority="any_participant"),
    ),
    "proposal_default": lambda: build_proposal_policy("p", "d"),
    "proposal_full": lambda: build_proposal_policy(
        "p",
        "d",
        acceptance=ProposalAcceptanceRules(criterion="counterparty"),
        counter_proposal=CounterProposalRules(max_rounds=3),
        rejection=RejectionRules(terminal_on_any_reject=True),
    ),
    "task_default": lambda: build_task_policy("p", "d"),
    "task_full": lambda: build_task_policy(
        "p",
        "d",
        assignment=TaskAssignmentRules(allow_reassignment_on_reject=True),
        completion=TaskCompletionRules(require_output=True),
    ),
    "handoff_default": lambda: build_handoff_policy("p", "d"),
    "handoff_full": lambda: build_handoff_policy(
        "p", "d", acceptance=HandoffAcceptanceRules(implicit_accept_timeout_ms=5000)
    ),
}


class TestGoldenOutputUnchanged:
    """Without the new arguments every builder's ``rules`` stay byte-identical
    to the pre-0.9 output, snapshotted in golden/policy_builders.json before
    the domain checks landed (the TS byte-identity tests rely on this)."""

    def test_every_case_is_snapshotted(self):
        assert set(_CASES) == set(_GOLDEN)

    @pytest.mark.parametrize("case", sorted(_CASES))
    def test_rules_bytes_unchanged(self, case):
        desc = _CASES[case]()
        golden = _GOLDEN[case]
        assert desc.rules == golden["rules"]
        assert desc.schema_version == golden["schema_version"]
        assert desc.mode == golden["mode"]
