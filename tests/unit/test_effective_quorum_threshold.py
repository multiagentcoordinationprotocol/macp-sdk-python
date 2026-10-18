"""effective_quorum_threshold: a port of the runtime's QuorumThreshold::effective
(runtime >= 0.9.0, S4 / RFC-MACP-0011 §5 rule 6, RFC-MACP-0012 §4.2)."""

from __future__ import annotations

import json

import pytest
from macp.modes.quorum.v1 import quorum_pb2

from macp_sdk import build_quorum_policy, effective_quorum_threshold
from macp_sdk.constants import MODE_QUORUM
from macp_sdk.policy import QuorumThreshold
from macp_sdk.quorum import QuorumProjection
from tests.conftest import make_envelope


def _rules(ttype: str, value: object) -> dict:
    return {"threshold": {"type": ttype, "value": value}}


class TestEffectiveQuorumThreshold:
    def test_n_of_m_override_above_participant_count_is_returned_as_given(self):
        assert effective_quorum_threshold(_rules("n_of_m", 5), 3, 2) == 5

    def test_count_alias_matches_n_of_m(self):
        assert effective_quorum_threshold(_rules("count", 4), 3, 2) == 4

    def test_fractional_n_of_m_rounds_up(self):
        assert effective_quorum_threshold(_rules("n_of_m", 1.2), 3, 2) == 2

    def test_percentage_100_over_3(self):
        assert effective_quorum_threshold(_rules("percentage", 100), 3, 2) == 3

    def test_percentage_floor_of_one(self):
        assert effective_quorum_threshold(_rules("percentage", 1), 3, 2) == 1

    @pytest.mark.parametrize(("value", "n", "expected"), [(7, 100, 7), (28, 25, 7)])
    def test_percentage_cases_a_float_division_gets_wrong(self, value, n, expected):
        assert effective_quorum_threshold(_rules("percentage", value), n, 1) == expected

    def test_percentage_matches_exact_integer_ceiling_everywhere(self):
        for value in range(1, 101):
            for n in range(1, 101):
                expected = max(1, (value * n + 99) // 100)
                assert effective_quorum_threshold(_rules("percentage", value), n, 1) == expected

    def test_integral_float_percentage_uses_integer_path(self):
        assert effective_quorum_threshold(_rules("percentage", 7.0), 100, 1) == 7

    def test_fractional_percentage_multiplies_before_dividing(self):
        assert effective_quorum_threshold(_rules("percentage", 50.5), 2, 1) == 2

    def test_percentage_with_no_participants_is_unsatisfiable(self):
        assert effective_quorum_threshold(_rules("percentage", 50), 0, 1) is None

    def test_unknown_type_is_unsatisfiable(self):
        assert effective_quorum_threshold(_rules("weighted", 2), 3, 2) is None

    @pytest.mark.parametrize(
        "rules",
        [
            None,
            {},
            {"threshold": None},
            {"threshold": "n_of_m"},
            _rules("n_of_m", 0),
            _rules("n_of_m", -1),
            _rules("n_of_m", float("nan")),
            _rules("n_of_m", True),
            _rules("n_of_m", "5"),
            {"threshold": {"type": 7, "value": 5}},
            b"",
            b"{not json",
        ],
    )
    def test_inert_or_unparseable_falls_back_to_required_approvals(self, rules):
        assert effective_quorum_threshold(rules, 3, 2) == 2

    def test_missing_type_defaults_to_n_of_m(self):
        assert effective_quorum_threshold({"threshold": {"value": 4}}, 3, 2) == 4

    def test_huge_values_saturate(self):
        assert effective_quorum_threshold(_rules("n_of_m", float("inf")), 3, 2) == 2**32 - 1
        assert effective_quorum_threshold(_rules("n_of_m", 10**12), 3, 2) == 2**32 - 1
        assert effective_quorum_threshold(_rules("percentage", 10**12), 3, 2) == 2**32 - 1

    def test_accepts_descriptor_bytes_and_str(self):
        desc = build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=5))
        assert effective_quorum_threshold(desc, 3, 2) == 5
        assert effective_quorum_threshold(desc.rules, 3, 2) == 5
        assert effective_quorum_threshold(json.dumps(_rules("n_of_m", 5)), 3, 2) == 5


class TestProjectionHelpersHonourEffectiveThreshold:
    def _proj(self, required: int = 2) -> QuorumProjection:
        p = QuorumProjection()
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM,
                "ApprovalRequest",
                quorum_pb2.ApprovalRequestPayload(request_id="r1", required_approvals=required),
                sender="coordinator",
            )
        )
        return p

    def _approve(self, p: QuorumProjection, sender: str) -> None:
        p.apply_envelope(
            make_envelope(
                MODE_QUORUM, "Approve", quorum_pb2.ApprovePayload(request_id="r1"), sender=sender
            )
        )

    def test_over_count_override_is_unreachable_before_any_ballot(self):
        p = self._proj()
        t = effective_quorum_threshold(_rules("n_of_m", 5), 3, 2)
        assert not p.is_threshold_unreachable("r1", 3)
        assert p.is_threshold_unreachable("r1", 3, effective_threshold=t)

    def test_override_replaces_required_approvals_everywhere(self):
        p = self._proj()
        self._approve(p, "alice")
        self._approve(p, "bob")
        assert p.has_quorum("r1") and p.commitment_ready("r1")
        assert not p.has_quorum("r1", effective_threshold=3)
        assert not p.commitment_ready("r1", effective_threshold=3)
        assert p.threshold("r1", effective_threshold=3) == 3
        assert p.remaining_votes_needed("r1", effective_threshold=3) == 1
        assert p.threshold("r1") == 2
        assert p.remaining_votes_needed("r1") == 0

    def test_unknown_request_ignores_override(self):
        p = QuorumProjection()
        assert not p.has_quorum("nope", effective_threshold=1)
        assert not p.is_threshold_unreachable("nope", 3, effective_threshold=5)
        assert p.threshold("nope", effective_threshold=5) == 0
        assert p.remaining_votes_needed("nope", effective_threshold=5) == 0
