"""Tests for policy builders — validates JSON output matches Runtime rule schemas."""

from __future__ import annotations

import json

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

# ── Decision mode ────────────────────────────────────────────────────


class TestBuildDecisionPolicy:
    def test_defaults_match_runtime(self):
        desc = build_decision_policy("pol-1", "A test policy")
        assert desc.policy_id == "pol-1"
        assert desc.mode == "macp.mode.decision.v1"
        # RFC-MACP-0012 schema_version 3 default (issue #65): fail-closed empty
        # tallies for binding algorithms. Only Decision is bumped.
        assert desc.schema_version == 3
        rules = json.loads(desc.rules)
        # Voting — matches Runtime VotingRules defaults
        assert rules["voting"]["algorithm"] == "none"
        assert rules["voting"]["threshold"] == 0.5
        # Optional voting fields are omitted when unset (byte-parity with the
        # typescript-sdk builder and the canonical example descriptors).
        assert "quorum" not in rules["voting"]
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
        """RFC-MACP-0012 schema_version 2: negative committed outcomes.

        Explicit ``schema_version=2`` (issue #65 moved the default to 3) --
        these fields are version-agnostic in the builder, but the docstring's
        claim is specifically about v2 semantics, so pin the version being
        tested rather than relying on whatever the default happens to be.
        """
        desc = build_decision_policy(
            "pol-neg",
            "decline over approval",
            objection_handling=ObjectionHandlingRules(critical_objection_action="finalize_decline"),
            commitment=CommitmentRules(allow_decline_over_approval=True),
            schema_version=2,
        )
        assert desc.schema_version == 2
        rules = json.loads(desc.rules)
        assert rules["commitment"]["allow_decline_over_approval"] is True
        assert rules["objection_handling"]["critical_objection_action"] == "finalize_decline"

    def test_schema_version_3_explicit(self):
        """RFC-MACP-0012 schema_version 3 (spec PR #99): fail-closed empty
        tallies. Also the default since issue #65 -- passing it explicitly
        still works and is the recommended way to make the choice visible at
        the call site."""
        desc = build_decision_policy("pol-v3", "fail-closed", schema_version=3)
        assert desc.schema_version == 3

    def test_schema_version_default_is_3(self):
        """Issue #65: the default flipped from 2 to 3 -- RFC-MACP-0012's
        authoring guidance for new non-'none' policies, and the old default
        was fail-open on an empty tally for callers who asked for a binding
        algorithm. Not retroactive: a stored policy always evaluates under
        its own recorded schema_version (RFC-MACP-0012 §8)."""
        desc = build_decision_policy("pol-default", "default version")
        assert desc.schema_version == 3

    def test_schema_version_1_still_supported(self):
        desc = build_decision_policy("pol-v1", "legacy", schema_version=1)
        assert desc.schema_version == 1

    def test_schema_version_invalid_rejected(self):
        with pytest.raises(MacpSessionError, match="schema_version"):
            build_decision_policy("pol-bad", "bad version", schema_version=4)

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


class TestDecisionVotingValidation:
    """decision-rules.schema.json tightenings (spec PR #99 / #101): a bad
    descriptor should fail client-side, matching build_quorum_policy's
    existing pre-validation of threshold values (issue #61 comment)."""

    def test_invalid_algorithm_rejected(self):
        with pytest.raises(MacpSessionError, match="algorithm"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="consensus"))

    @pytest.mark.parametrize("threshold", [0, -0.1, 1.1])
    def test_threshold_out_of_bounds_rejected(self, threshold):
        with pytest.raises(MacpSessionError, match="threshold"):
            build_decision_policy("q", "d", voting=VotingRules(threshold=threshold))

    def test_majority_below_half_rejected(self):
        with pytest.raises(MacpSessionError, match="majority"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="majority", threshold=0.4))

    def test_majority_at_half_accepted(self):
        desc = build_decision_policy(
            "q", "d", voting=VotingRules(algorithm="majority", threshold=0.5)
        )
        assert json.loads(desc.rules)["voting"]["threshold"] == 0.5

    def test_supermajority_default_threshold_rejected(self):
        # threshold defaults to 0.5, which is a bare majority wearing the
        # supermajority name (schema requires exclusiveMinimum 0.5).
        with pytest.raises(MacpSessionError, match="supermajority"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="supermajority"))

    def test_supermajority_above_half_accepted(self):
        desc = build_decision_policy(
            "q", "d", voting=VotingRules(algorithm="supermajority", threshold=0.67)
        )
        assert json.loads(desc.rules)["voting"]["threshold"] == 0.67

    def test_weighted_without_weights_rejected(self):
        with pytest.raises(MacpSessionError, match="weighted"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="weighted"))

    def test_weighted_with_empty_weights_rejected(self):
        with pytest.raises(MacpSessionError, match="non-empty"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="weighted", weights={}))

    def test_empty_weights_rejected_even_off_weighted_algorithm(self):
        # The non-empty check on 'weights' applies whenever the map is
        # supplied at all, independent of the 'weighted algorithm requires
        # weights' check above (which only fires for algorithm='weighted').
        with pytest.raises(MacpSessionError, match="non-empty"):
            build_decision_policy("q", "d", voting=VotingRules(algorithm="none", weights={}))

    @pytest.mark.parametrize("algorithm", ["none", "majority", "unanimous", "plurality"])
    def test_zero_weight_rejected_at_every_algorithm(self, algorithm):
        # The electorate rule (weights minProperties 1, values exclusiveMinimum
        # 0) is normative at every schema_version and every algorithm, not
        # only 'weighted' -- a weight-0 observer is omitted, not zeroed.
        with pytest.raises(MacpSessionError, match="weights"):
            build_decision_policy(
                "q",
                "d",
                voting=VotingRules(algorithm=algorithm, threshold=1.0, weights={"a": 0}),
            )

    def test_designated_role_without_roles_rejected(self):
        with pytest.raises(MacpSessionError, match="designated_role"):
            build_decision_policy("q", "d", commitment=CommitmentRules(authority="designated_role"))


class TestSharedDesignatedRoleValidation:
    """authority='designated_role' with empty designated_roles names no one
    (spec PR #121) -- validated once in _commitment_dict, shared by all five
    mode builders."""

    def test_quorum_rejects_empty_designated_roles(self):
        with pytest.raises(MacpSessionError, match="designated_role"):
            build_quorum_policy("q", "d", commitment=CommitmentRules(authority="designated_role"))

    def test_proposal_rejects_empty_designated_roles(self):
        with pytest.raises(MacpSessionError, match="designated_role"):
            build_proposal_policy("q", "d", commitment=CommitmentRules(authority="designated_role"))

    def test_task_rejects_empty_designated_roles(self):
        with pytest.raises(MacpSessionError, match="designated_role"):
            build_task_policy("q", "d", commitment=CommitmentRules(authority="designated_role"))

    def test_handoff_rejects_empty_designated_roles(self):
        with pytest.raises(MacpSessionError, match="designated_role"):
            build_handoff_policy("q", "d", commitment=CommitmentRules(authority="designated_role"))

    def test_non_designated_role_authority_unaffected(self):
        # Default authority never triggers the check, regardless of roles.
        desc = build_quorum_policy("q", "d", commitment=CommitmentRules())
        assert json.loads(desc.rules)["commitment"]["designated_roles"] == []


# ── Quorum mode ──────────────────────────────────────────────────────


class TestBuildQuorumPolicy:
    def test_defaults_match_runtime(self):
        desc = build_quorum_policy("qp-1", "Quorum default")
        assert desc.mode == "macp.mode.quorum.v1"
        assert desc.schema_version == 1
        rules = json.loads(desc.rules)
        # threshold — matches Runtime QuorumThreshold defaults
        assert rules["threshold"]["type"] == "n_of_m"
        assert rules["threshold"]["value"] == 1
        # abstention — matches Runtime AbstentionRules defaults
        assert rules["abstention"]["counts_toward_quorum"] is False
        assert rules["abstention"]["interpretation"] == "neutral"
        # commitment — shared CommitmentRules defaults
        assert rules["commitment"]["authority"] == "initiator_only"

    def test_custom(self):
        desc = build_quorum_policy(
            "qp-2",
            "Custom quorum",
            threshold=QuorumThreshold(type="percentage", value=75),
            abstention=AbstentionRules(counts_toward_quorum=True, interpretation="implicit_reject"),
            commitment=CommitmentRules(authority="any_participant"),
        )
        rules = json.loads(desc.rules)
        assert rules["threshold"]["type"] == "percentage"
        assert rules["threshold"]["value"] == 75
        assert rules["abstention"]["counts_toward_quorum"] is True
        assert rules["abstention"]["interpretation"] == "implicit_reject"
        assert rules["commitment"]["authority"] == "any_participant"


class TestQuorumThresholdIntegrality:
    """build_quorum_policy rejects a non-integer threshold value (#50).

    The canonical quorum-rules.schema.json declares 'value' as an integer
    for every threshold type, and macp-sdk-typescript already throws on a
    fractional percentage. Python must match, for all three threshold
    types, not just 'percentage'.
    """

    @pytest.mark.parametrize("threshold_type", ["n_of_m", "percentage", "weighted"])
    def test_fractional_value_rejected(self, threshold_type):
        with pytest.raises(MacpSessionError, match="integer"):
            build_quorum_policy(
                "q", "d", threshold=QuorumThreshold(type=threshold_type, value=0.75)
            )

    @pytest.mark.parametrize("threshold_type", ["n_of_m", "percentage", "weighted"])
    def test_bool_value_rejected(self, threshold_type):
        # isinstance(True, int) is True in Python -- value=True must not
        # silently mean 1.
        with pytest.raises(MacpSessionError, match="integer"):
            build_quorum_policy(
                "q", "d", threshold=QuorumThreshold(type=threshold_type, value=True)
            )

    @pytest.mark.parametrize("value", [1, 3, 75, 100])
    def test_integer_values_still_accepted(self, value):
        desc = build_quorum_policy(
            "q", "d", threshold=QuorumThreshold(type="percentage", value=value)
        )
        rules = json.loads(desc.rules)
        assert rules["threshold"]["value"] == value
        assert isinstance(rules["threshold"]["value"], int)

    def test_accepting_cases_unchanged_json_bytes(self):
        # Byte-identical to the descriptor produced before this validation
        # was added -- the check must reject bad input, not alter good input.
        desc = build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=3))
        assert json.loads(desc.rules)["threshold"] == {"type": "n_of_m", "value": 3}

    def test_existing_range_checks_unchanged(self):
        with pytest.raises(MacpSessionError):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=-1))
        with pytest.raises(MacpSessionError):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="percentage", value=150))

    def test_zero_value_rejected(self):
        # quorum-rules.schema.json declares 'value' with exclusiveMinimum 0
        # (spec PR #99): a zero approval bar is trivially satisfied by any
        # ballot set, so a restrictive-looking quorum policy approves
        # everything.
        with pytest.raises(MacpSessionError, match="> 0"):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=0))

    def test_weighted_type_rejected(self):
        # 'weighted' was removed from the canonical quorum-rules schema
        # (reserved, no defined semantics) -- must not be silently accepted.
        with pytest.raises(MacpSessionError, match="reserved"):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="weighted", value=1))

    def test_unknown_type_rejected(self):
        with pytest.raises(MacpSessionError, match="n_of_m"):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="bogus", value=1))


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
        # require_vote_quorum and allow_decline_over_approval are Decision-only
        # (decision-rules.schema.json declares them; the other four modes'
        # commitment schemas declare only {authority, designated_roles} and are
        # closed with additionalProperties: false) — must NOT leak in here.
        assert "require_vote_quorum" not in rules["commitment"]
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
