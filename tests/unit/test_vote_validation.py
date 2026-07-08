"""Tests for vote, evaluation, and objection value validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from macp_sdk.decision import DecisionSession
from macp_sdk.errors import MacpSessionError
from tests.conftest import VALID_SESSION_ID

_SID = VALID_SESSION_ID


@pytest.fixture
def session():
    mock = MagicMock()
    mock.auth = MagicMock()
    mock.auth.sender = "test"
    mock.auth.metadata.return_value = []
    ack = MagicMock()
    ack.ok = True
    mock.send.return_value = ack
    return DecisionSession(mock, session_id=_SID)


class TestVoteValidation:
    @pytest.mark.parametrize("vote", ["APPROVE", "REJECT", "ABSTAIN"])
    def test_valid_votes(self, session, vote):
        session.vote("p1", vote)

    @pytest.mark.parametrize("vote", ["approve", "Reject", "abstain"])
    def test_case_insensitive_votes(self, session, vote):
        session.vote("p1", vote)

    @pytest.mark.parametrize("vote", ["MAYBE", "YES", "NO", ""])
    def test_invalid_votes_raise(self, session, vote):
        with pytest.raises(MacpSessionError, match="invalid vote"):
            session.vote("p1", vote)


class TestEvaluationValidation:
    @pytest.mark.parametrize("rec", ["APPROVE", "REVIEW", "BLOCK", "REJECT"])
    def test_valid_recommendations(self, session, rec):
        session.evaluate("p1", rec, confidence=0.5)

    @pytest.mark.parametrize("rec", ["approve", "Review", "block"])
    def test_case_insensitive_recommendations(self, session, rec):
        session.evaluate("p1", rec, confidence=0.5)

    @pytest.mark.parametrize("rec", ["SUGGEST", "PASS", ""])
    def test_invalid_recommendations_raise(self, session, rec):
        with pytest.raises(MacpSessionError, match="invalid recommendation"):
            session.evaluate("p1", rec, confidence=0.5)

    def test_confidence_below_zero_raises(self, session):
        with pytest.raises(MacpSessionError, match="confidence"):
            session.evaluate("p1", "APPROVE", confidence=-0.1)

    def test_confidence_above_one_raises(self, session):
        with pytest.raises(MacpSessionError, match="confidence"):
            session.evaluate("p1", "APPROVE", confidence=1.01)

    def test_confidence_boundaries_ok(self, session):
        session.evaluate("p1", "APPROVE", confidence=0.0)
        session.evaluate("p1", "APPROVE", confidence=1.0)


class TestObjectionValidation:
    @pytest.mark.parametrize("sev", ["critical", "high", "medium", "low"])
    def test_valid_severities(self, session, sev):
        session.raise_objection("p1", reason="test", severity=sev)

    @pytest.mark.parametrize("sev", ["Critical", "HIGH", "Medium"])
    def test_case_insensitive_severities(self, session, sev):
        session.raise_objection("p1", reason="test", severity=sev)

    @pytest.mark.parametrize("sev", ["extreme", "block", "none", ""])
    def test_invalid_severities_raise(self, session, sev):
        with pytest.raises(MacpSessionError, match="invalid severity"):
            session.raise_objection("p1", reason="test", severity=sev)
