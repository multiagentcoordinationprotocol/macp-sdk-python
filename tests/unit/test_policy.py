"""Tests for policy builders — validates JSON output matches Runtime rule schemas."""

from __future__ import annotations

import json

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

# ── Decision mode ────────────────────────────────────────────────────


class TestBuildDecisionPolicy:
    def test_defaults_match_runtime(self):
        desc = build_decision_policy("pol-1", "A test policy")
        assert desc.policy_id == "pol-1"
        assert desc.mode == "macp.mode.decision.v1"
        # RFC-MACP-0012 schema_version 2 (adds decline-over-approval + the
        # critical objection action). Only Decision is bumped.
        assert desc.schema_version == 2
        rules = json.loads(desc.rules)
        # Voting — matches Runtime VotingRules defaults
        assert rules["voting"]["algorithm"] == "none"
        assert rules["voting"]["threshold"] == 0.5
        assert rules["voting"]["quorum"]["type"] == "count"
        assert rules["voting"]["quorum"]["value"] == 0
        assert "weights" not in rules["voting"]
        # Objection handling — matches Runtime defaults
        assert rules["objection_handling"]["critical_severity_vetoes"] is False
        assert rules["objection_handling"]["veto_threshold"] == 1
        assert rules["objection_handling"]["critical_objection_action"] == "deny"
        # Evaluation — matches Runtime defaults
        assert rules["evaluation"]["minimum_confidence"] == 0.0
        assert rules["evaluation"]["required_before_voting"] is False
        # Commitment — matches Runtime CommitmentRules defaults
        assert rules["commitment"]["authority"] == "initiator_only"
        assert rules["commitment"]["designated_roles"] == []
        assert rules["commitment"]["require_vote_quorum"] is False
        # schema_version 2: decline-over-approval, default off (backward compatible)
        assert rules["commitment"]["allow_decline_over_approval"] is False

    def test_custom_voting(self):
        desc = build_decision_policy(
            "pol-2",
            "weighted vote",
            voting=VotingRules(
                algorithm="supermajority",
                threshold=0.67,
                quorum_type="percentage",
                quorum_value=0.75,
                weights={"lead": 3.0, "member": 1.0},
            ),
        )
        rules = json.loads(desc.rules)
        assert rules["voting"]["algorithm"] == "supermajority"
        assert rules["voting"]["threshold"] == 0.67
        assert rules["voting"]["quorum"]["type"] == "percentage"
        assert rules["voting"]["quorum"]["value"] == 0.75
        assert rules["voting"]["weights"]["lead"] == 3.0

    def test_custom_objection_handling(self):
        desc = build_decision_policy(
            "pol-3",
            "strict vetoes",
            objection_handling=ObjectionHandlingRules(
                critical_severity_vetoes=True, veto_threshold=2
            ),
        )
        rules = json.loads(desc.rules)
        assert rules["objection_handling"]["critical_severity_vetoes"] is True
        assert rules["objection_handling"]["veto_threshold"] == 2

    def test_custom_evaluation(self):
        desc = build_decision_policy(
            "pol-4",
            "high confidence",
            evaluation=EvaluationRules(minimum_confidence=0.8, required_before_voting=True),
        )
        rules = json.loads(desc.rules)
        assert rules["evaluation"]["minimum_confidence"] == 0.8
        assert rules["evaluation"]["required_before_voting"] is True

    def test_schema_version_2_fields(self):
        """RFC-MACP-0012 schema_version 2: negative committed outcomes."""
        desc = build_decision_policy(
            "pol-neg",
            "decline over approval",
            objection_handling=ObjectionHandlingRules(critical_objection_action="finalize_decline"),
            commitment=CommitmentRules(allow_decline_over_approval=True),
        )
        assert desc.schema_version == 2
        rules = json.loads(desc.rules)
        assert rules["commitment"]["allow_decline_over_approval"] is True
        assert rules["objection_handling"]["critical_objection_action"] == "finalize_decline"

    def test_custom_commitment_with_roles(self):
        desc = build_decision_policy(
            "pol-5",
            "role-based commit",
            commitment=CommitmentRules(
                authority="designated_role",
                designated_roles=["lead", "admin"],
                require_vote_quorum=True,
            ),
        )
        rules = json.loads(desc.rules)
        assert rules["commitment"]["authority"] == "designated_role"
        assert rules["commitment"]["designated_roles"] == ["lead", "admin"]
        assert rules["commitment"]["require_vote_quorum"] is True

    def test_all_sections_combined(self):
        desc = build_decision_policy(
            "pol-full",
            "full policy",
            voting=VotingRules(
                algorithm="unanimous",
                threshold=1.0,
                quorum_type="count",
                quorum_value=3,
                weights={"a": 1.0},
            ),
            objection_handling=ObjectionHandlingRules(
                critical_severity_vetoes=True, veto_threshold=1
            ),
            evaluation=EvaluationRules(minimum_confidence=0.9, required_before_voting=True),
            commitment=CommitmentRules(authority="any_participant", require_vote_quorum=True),
        )
        rules = json.loads(desc.rules)
        assert rules["voting"]["algorithm"] == "unanimous"
        assert rules["voting"]["weights"]["a"] == 1.0
        assert rules["objection_handling"]["critical_severity_vetoes"] is True
        assert rules["evaluation"]["minimum_confidence"] == 0.9
        assert rules["commitment"]["authority"] == "any_participant"

    def test_json_roundtrip(self):
        desc = build_decision_policy(
            "pol-rt",
            "roundtrip",
            voting=VotingRules(algorithm="weighted", weights={"a": 1.0, "b": 2.0}),
        )
        rules_1 = json.loads(desc.rules)
        rules_2 = json.loads(json.dumps(rules_1).encode())
        assert rules_1 == rules_2


# ── Quorum mode ──────────────────────────────────────────────────────


class TestBuildQuorumPolicy:
    def test_defaults_match_runtime(self):
        desc = build_quorum_policy("qp-1", "Quorum default")
        assert desc.mode == "macp.mode.quorum.v1"
        assert desc.schema_version == 1
        rules = json.loads(desc.rules)
        # threshold — matches Runtime QuorumThreshold defaults
        assert rules["threshold"]["type"] == "n_of_m"
        assert rules["threshold"]["value"] == 0
        # abstention — matches Runtime AbstentionRules defaults
        assert rules["abstention"]["counts_toward_quorum"] is False
        assert rules["abstention"]["interpretation"] == "neutral"
        # commitment — shared CommitmentRules defaults
        assert rules["commitment"]["authority"] == "initiator_only"

    def test_custom(self):
        desc = build_quorum_policy(
            "qp-2",
            "Custom quorum",
            threshold=QuorumThreshold(type="percentage", value=0.75),
            abstention=AbstentionRules(counts_toward_quorum=True, interpretation="implicit_reject"),
            commitment=CommitmentRules(authority="any_participant"),
        )
        rules = json.loads(desc.rules)
        assert rules["threshold"]["type"] == "percentage"
        assert rules["threshold"]["value"] == 0.75
        assert rules["abstention"]["counts_toward_quorum"] is True
        assert rules["abstention"]["interpretation"] == "implicit_reject"
        assert rules["commitment"]["authority"] == "any_participant"


# ── Proposal mode ────────────────────────────────────────────────────


class TestBuildProposalPolicy:
    def test_defaults_match_runtime(self):
        desc = build_proposal_policy("pp-1", "Proposal default")
        assert desc.mode == "macp.mode.proposal.v1"
        rules = json.loads(desc.rules)
        assert rules["acceptance"]["criterion"] == "all_parties"
        assert rules["counter_proposal"]["max_rounds"] == 0
        assert rules["rejection"]["terminal_on_any_reject"] is False
        assert rules["commitment"]["authority"] == "initiator_only"

    def test_custom(self):
        desc = build_proposal_policy(
            "pp-2",
            "Custom proposal",
            acceptance=ProposalAcceptanceRules(criterion="counterparty"),
            counter_proposal=CounterProposalRules(max_rounds=5),
            rejection=RejectionRules(terminal_on_any_reject=True),
            commitment=CommitmentRules(
                authority="designated_role",
                designated_roles=["chair"],
            ),
        )
        rules = json.loads(desc.rules)
        assert rules["acceptance"]["criterion"] == "counterparty"
        assert rules["counter_proposal"]["max_rounds"] == 5
        assert rules["rejection"]["terminal_on_any_reject"] is True
        assert rules["commitment"]["designated_roles"] == ["chair"]


# ── Task mode ────────────────────────────────────────────────────────


class TestBuildTaskPolicy:
    def test_defaults_match_runtime(self):
        desc = build_task_policy("tp-1", "Task default")
        assert desc.mode == "macp.mode.task.v1"
        rules = json.loads(desc.rules)
        assert rules["assignment"]["allow_reassignment_on_reject"] is False
        assert rules["completion"]["require_output"] is False
        assert rules["commitment"]["authority"] == "initiator_only"

    def test_custom(self):
        desc = build_task_policy(
            "tp-2",
            "Custom task",
            assignment=TaskAssignmentRules(allow_reassignment_on_reject=True),
            completion=TaskCompletionRules(require_output=True),
            commitment=CommitmentRules(authority="any_participant"),
        )
        rules = json.loads(desc.rules)
        assert rules["assignment"]["allow_reassignment_on_reject"] is True
        assert rules["completion"]["require_output"] is True
        assert rules["commitment"]["authority"] == "any_participant"


# ── Handoff mode ─────────────────────────────────────────────────────


class TestBuildHandoffPolicy:
    def test_defaults_match_runtime(self):
        desc = build_handoff_policy("hp-1", "Handoff default")
        assert desc.mode == "macp.mode.handoff.v1"
        rules = json.loads(desc.rules)
        assert rules["acceptance"]["implicit_accept_timeout_ms"] == 0
        assert rules["commitment"]["authority"] == "initiator_only"

    def test_custom(self):
        desc = build_handoff_policy(
            "hp-2",
            "Custom handoff",
            acceptance=HandoffAcceptanceRules(implicit_accept_timeout_ms=30000),
            commitment=CommitmentRules(
                authority="designated_role",
                designated_roles=["oncall"],
            ),
        )
        rules = json.loads(desc.rules)
        assert rules["acceptance"]["implicit_accept_timeout_ms"] == 30000
        assert rules["commitment"]["designated_roles"] == ["oncall"]


# ── CommitmentRules shared across all modes ──────────────────────────


class TestCommitmentRulesShared:
    """Verify CommitmentRules works identically across all mode builders."""

    def _assert_commitment(self, rules: dict, authority: str, roles: list[str]) -> None:
        assert rules["commitment"]["authority"] == authority
        assert rules["commitment"]["designated_roles"] == roles
        assert rules["commitment"]["require_vote_quorum"] is False
        # The schema_version 2 decline switch is Decision-only and must NOT leak
        # into the still version-1 quorum/proposal/task/handoff commitment rules.
        assert "allow_decline_over_approval" not in rules["commitment"]

    def test_non_decision_modes_stay_schema_version_1(self):
        for build in (
            build_quorum_policy,
            build_proposal_policy,
            build_task_policy,
            build_handoff_policy,
        ):
            desc = build("sv1", "version-1 mode")
            assert desc.schema_version == 1, desc.mode
            assert "allow_decline_over_approval" not in json.loads(desc.rules)["commitment"]

    def test_quorum_with_commitment(self):
        desc = build_quorum_policy(
            "c-q",
            "test",
            commitment=CommitmentRules(authority="designated_role", designated_roles=["admin"]),
        )
        rules = json.loads(desc.rules)
        self._assert_commitment(rules, "designated_role", ["admin"])

    def test_proposal_with_commitment(self):
        desc = build_proposal_policy(
            "c-p",
            "test",
            commitment=CommitmentRules(authority="designated_role", designated_roles=["chair"]),
        )
        rules = json.loads(desc.rules)
        self._assert_commitment(rules, "designated_role", ["chair"])

    def test_task_with_commitment(self):
        desc = build_task_policy(
            "c-t",
            "test",
            commitment=CommitmentRules(authority="designated_role", designated_roles=["manager"]),
        )
        rules = json.loads(desc.rules)
        self._assert_commitment(rules, "designated_role", ["manager"])

    def test_handoff_with_commitment(self):
        desc = build_handoff_policy(
            "c-h",
            "test",
            commitment=CommitmentRules(authority="designated_role", designated_roles=["oncall"]),
        )
        rules = json.loads(desc.rules)
        self._assert_commitment(rules, "designated_role", ["oncall"])


# ── Dataclass immutability ───────────────────────────────────────────


class TestDataclassImmutability:
    def test_voting_rules_frozen(self):
        v = VotingRules()
        try:
            v.algorithm = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_commitment_rules_frozen(self):
        c = CommitmentRules()
        try:
            c.authority = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_quorum_threshold_frozen(self):
        q = QuorumThreshold()
        try:
            q.type = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_abstention_rules_frozen(self):
        a = AbstentionRules()
        try:
            a.interpretation = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_proposal_acceptance_frozen(self):
        p = ProposalAcceptanceRules()
        try:
            p.criterion = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_task_assignment_frozen(self):
        t = TaskAssignmentRules()
        try:
            t.allow_reassignment_on_reject = True  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_handoff_acceptance_frozen(self):
        h = HandoffAcceptanceRules()
        try:
            h.implicit_accept_timeout_ms = 5000  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass
