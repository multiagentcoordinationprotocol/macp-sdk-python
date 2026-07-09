"""Tests for max participants safety limit."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from macp_sdk.decision import DecisionSession
from macp_sdk.errors import MacpSessionError
from tests.conftest import VALID_SESSION_ID

_SID = VALID_SESSION_ID


@pytest.fixture
def mock_client():
    mock = MagicMock()
    mock.auth = MagicMock()
    mock.auth.sender = "test"
    mock.auth.metadata.return_value = []
    ack = MagicMock()
    ack.ok = True
    mock.send.return_value = ack
    return mock


class TestMaxParticipants:
    def test_1000_participants_ok(self, mock_client):
        s = DecisionSession(mock_client, session_id=_SID)
        participants = [f"agent-{i}" for i in range(1000)]
        s.start(intent="test", participants=participants, ttl_ms=60000)

    def test_1001_participants_raises(self, mock_client):
        s = DecisionSession(mock_client, session_id=_SID)
        participants = [f"agent-{i}" for i in range(1001)]
        with pytest.raises(MacpSessionError, match="Maximum 1000"):
            s.start(intent="test", participants=participants, ttl_ms=60000)
