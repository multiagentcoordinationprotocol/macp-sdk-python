"""Coverage for the centralized validation helpers (Q-7 ripple).

These helpers are the pre-flight gate for every session action. The
mode-helper tests cover the happy paths implicitly — this file locks
every branch.
"""

from __future__ import annotations

import re

import pytest

from macp_sdk.errors import MacpSessionError
from macp_sdk.validation import (
    validate_commitment_hash,
    validate_confidence,
    validate_participant_count,
    validate_participants,
    validate_progress_scope,
    validate_recommendation,
    validate_required_field,
    validate_session_id,
    validate_session_start,
    validate_severity,
    validate_signal_type,
    validate_ttl_ms,
    validate_vote,
)
from tests.conftest import VALID_COMMITMENT_HASH, VALID_SESSION_ID


class TestCommitmentHash:
    def test_valid_ok(self):
        validate_commitment_hash(VALID_COMMITMENT_HASH)

    def test_empty_raises(self):
        with pytest.raises(MacpSessionError, match="commitment_hash"):
            validate_commitment_hash("")

    def test_missing_prefix_raises(self):
        with pytest.raises(MacpSessionError, match="commitment_hash"):
            validate_commitment_hash("abc123")

    def test_uppercase_hex_raises(self):
        with pytest.raises(MacpSessionError, match="commitment_hash"):
            validate_commitment_hash("sha256:" + "A" * 64)

    def test_short_digest_raises(self):
        with pytest.raises(MacpSessionError, match="commitment_hash"):
            validate_commitment_hash("sha256:" + "a" * 63)

    def test_long_digest_raises(self):
        with pytest.raises(MacpSessionError, match="commitment_hash"):
            validate_commitment_hash("sha256:" + "a" * 65)


class TestSessionId:
    def test_uuid_v4_ok(self):
        validate_session_id(VALID_SESSION_ID)

    def test_base64url_ok(self):
        validate_session_id("A1b2C3d4E5f6G7h8I9j0K1")  # 22 chars

    def test_invalid_raises(self):
        with pytest.raises(MacpSessionError, match="session_id must be"):
            validate_session_id("not-a-uuid")


class TestVote:
    @pytest.mark.parametrize("v", ["APPROVE", "reject", "abstain"])
    def test_valid_returns_uppercase(self, v):
        assert validate_vote(v) == v.upper()

    def test_invalid_raises(self):
        with pytest.raises(MacpSessionError, match="invalid vote"):
            validate_vote("maybe")


class TestRecommendation:
    @pytest.mark.parametrize("v", ["APPROVE", "review", "block", "REJECT"])
    def test_valid(self, v):
        assert validate_recommendation(v) == v.upper()

    def test_invalid_raises(self):
        with pytest.raises(MacpSessionError, match="invalid recommendation"):
            validate_recommendation("later")


class TestConfidence:
    @pytest.mark.parametrize("v", [0.0, 0.5, 1.0])
    def test_valid(self, v):
        validate_confidence(v)

    @pytest.mark.parametrize("v", [-0.01, 1.01, 10.0])
    def test_invalid(self, v):
        with pytest.raises(MacpSessionError, match="confidence"):
            validate_confidence(v)


class TestSeverity:
    @pytest.mark.parametrize("v", ["Critical", "HIGH", "medium", "low"])
    def test_valid_normalises(self, v):
        assert validate_severity(v) == v.lower()

    def test_invalid(self):
        with pytest.raises(MacpSessionError, match="invalid severity"):
            validate_severity("extreme")


class TestParticipantCount:
    def test_zero_is_ok_here(self):
        # this helper only guards the *upper* bound; validate_participants
        # is the one that requires non-empty.
        validate_participant_count(0)

    def test_under_limit(self):
        validate_participant_count(999)

    def test_at_limit(self):
        validate_participant_count(1000)

    def test_over_limit(self):
        with pytest.raises(MacpSessionError, match="Maximum 1000"):
            validate_participant_count(1001)


class TestParticipants:
    def test_empty_raises(self):
        with pytest.raises(MacpSessionError, match="must be non-empty"):
            validate_participants([])

    def test_duplicate_raises(self):
        with pytest.raises(MacpSessionError, match="duplicate participant: alice"):
            validate_participants(["alice", "bob", "alice"])

    def test_happy_path(self):
        validate_participants(["alice", "bob", "carol"])

    def test_over_limit_raises(self):
        with pytest.raises(MacpSessionError, match="Maximum 1000"):
            validate_participants([f"p{i}" for i in range(1001)])


class TestSignalType:
    def test_empty_data_tolerated(self):
        validate_signal_type("", data=None)
        validate_signal_type("", data=b"")

    def test_non_empty_data_requires_type(self):
        with pytest.raises(MacpSessionError, match="signal_type"):
            validate_signal_type("   ", data=b"x")

    def test_non_empty_data_and_type_ok(self):
        validate_signal_type("heartbeat", data=b"x")


class TestTtlMs:
    @pytest.mark.parametrize("v", [1, 60_000, 86_400_000])
    def test_valid(self, v):
        validate_ttl_ms(v)

    @pytest.mark.parametrize("v", [0, -1, 86_400_001])
    def test_invalid(self, v):
        with pytest.raises(MacpSessionError, match="ttl_ms"):
            validate_ttl_ms(v)


class TestRequiredField:
    def test_empty(self):
        with pytest.raises(MacpSessionError, match="intent"):
            validate_required_field("intent", "")

    def test_whitespace_only(self):
        with pytest.raises(MacpSessionError, match="reason"):
            validate_required_field("reason", "   ")

    def test_ok(self):
        validate_required_field("intent", "decide")


class TestSessionStartComposite:
    def test_happy_path(self):
        validate_session_start(
            intent="x",
            participants=["a", "b"],
            ttl_ms=1000,
            mode_version="macp.mode.decision.v1",
            configuration_version="1",
        )

    def test_bad_ttl_surfaces(self):
        with pytest.raises(MacpSessionError, match="ttl_ms"):
            validate_session_start(
                intent="x",
                participants=["a"],
                ttl_ms=0,
                mode_version="m",
                configuration_version="c",
            )

    def test_missing_mode_version_surfaces(self):
        with pytest.raises(MacpSessionError, match="mode_version"):
            validate_session_start(
                intent="x",
                participants=["a"],
                ttl_ms=1000,
                mode_version="",
                configuration_version="c",
            )


class TestValidateProgressScope:
    """RFC-MACP-0001 §6 tri-state rule for ``Progress`` (spec PR #91, issue #55).

    Mirrors macp-runtime's ``validate_envelope_shape`` (PR #137).
    """

    SID = "550e8400-e29b-41d4-a716-446655440000"

    def test_accepts_ambient_form(self):
        validate_progress_scope("", "")

    def test_accepts_session_scoped_form(self):
        validate_progress_scope(self.SID, "macp.mode.task.v1")

    def test_rejects_session_id_without_mode(self):
        with pytest.raises(MacpSessionError, match="but mode is empty"):
            validate_progress_scope(self.SID, "")

    def test_rejects_mode_without_session_id(self):
        with pytest.raises(MacpSessionError, match="but session_id is empty"):
            validate_progress_scope("", "macp.mode.task.v1")

    def test_error_names_the_offending_values(self):
        with pytest.raises(MacpSessionError, match=self.SID):
            validate_progress_scope(self.SID, "")
        with pytest.raises(MacpSessionError, match=re.escape("macp.mode.task.v1")):
            validate_progress_scope("", "macp.mode.task.v1")

    # The runtime compares ``session_id.is_empty()`` against
    # ``mode.trim().is_empty()``. That asymmetry is mirrored, not normalised,
    # so the SDK's verdict matches the runtime's on every input.
    def test_whitespace_only_mode_is_empty_like_the_runtime(self):
        validate_progress_scope("", "   ")
        with pytest.raises(MacpSessionError):
            validate_progress_scope(self.SID, "   ")

    def test_session_id_is_not_stripped_like_the_runtime(self):
        # A whitespace-only session_id is non-empty to the runtime, so pairing
        # it with a real mode passes the shape check (it fails later, on
        # session lookup -- not this rule's concern).
        validate_progress_scope("   ", "macp.mode.task.v1")
        with pytest.raises(MacpSessionError):
            validate_progress_scope("   ", "")
