from __future__ import annotations

import pytest

from macp_sdk.auth import AuthConfig
from macp_sdk.errors import MacpSdkError, MacpSessionError


class TestAuthConfig:
    def test_for_dev_agent(self):
        auth = AuthConfig.for_dev_agent("alice")
        assert auth.bearer_token == "alice"
        assert auth.sender_hint == "alice"
        assert auth.sender == "alice"
        assert auth.expected_sender == "alice"

    def test_for_dev_agent_explicit_expected_sender(self):
        auth = AuthConfig.for_dev_agent("alice", expected_sender="alice-v2")
        assert auth.expected_sender == "alice-v2"

    def test_for_bearer(self):
        auth = AuthConfig.for_bearer("tok-123", sender_hint="bob")
        assert auth.bearer_token == "tok-123"
        assert auth.sender_hint == "bob"
        assert auth.sender == "bob"
        assert auth.expected_sender is None

    def test_for_bearer_no_hint(self):
        auth = AuthConfig.for_bearer("tok-123")
        assert auth.sender is None
        assert auth.expected_sender is None

    def test_for_bearer_expected_sender(self):
        """expected_sender sets both the guardrail and the default sender_hint."""
        auth = AuthConfig.for_bearer("tok-123", expected_sender="alice")
        assert auth.expected_sender == "alice"
        # sender_hint defaults to expected_sender when not provided explicitly
        assert auth.sender_hint == "alice"
        assert auth.sender == "alice"

    def test_for_bearer_expected_and_explicit_hint(self):
        auth = AuthConfig.for_bearer(
            "tok-123", sender_hint="alice-envelope", expected_sender="alice"
        )
        assert auth.sender_hint == "alice-envelope"
        assert auth.expected_sender == "alice"

    def test_missing_bearer_raises(self):
        with pytest.raises(MacpSessionError, match="bearer_token is required") as exc_info:
            AuthConfig()
        assert isinstance(exc_info.value, MacpSdkError)

    def test_metadata_dev_agent(self):
        auth = AuthConfig.for_dev_agent("alice")
        assert auth.metadata() == [("authorization", "Bearer alice")]

    def test_metadata_bearer(self):
        auth = AuthConfig.for_bearer("tok-123")
        assert auth.metadata() == [("authorization", "Bearer tok-123")]

    def test_metadata_bearer_is_stable(self):
        """Repeated metadata() calls return the same bearer header (no drift)."""
        auth = AuthConfig.for_bearer("tok-abc", expected_sender="alice")
        assert auth.metadata() == [("authorization", "Bearer tok-abc")]

    def test_frozen(self):
        auth = AuthConfig.for_dev_agent("alice")
        with pytest.raises(AttributeError):
            auth.bearer_token = "bob"  # type: ignore[misc]
