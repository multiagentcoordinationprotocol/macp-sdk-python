"""Direct unit tests for the shared ``BaseSession`` ABC.

The concrete mode sessions exercise this logic indirectly; these tests pin
the shared contract (sender resolution, ack tracking, id validation,
lifecycle delegation) so a regression fails here first with a clear name.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from macp_sdk.auth import AuthConfig
from macp_sdk.base_projection import BaseProjection
from macp_sdk.base_session import BaseSession
from macp_sdk.errors import MacpIdentityMismatchError, MacpSessionError
from tests.conftest import VALID_SESSION_ID, make_ack

TEST_MODE = "macp.mode.test.v1"


class _Projection(BaseProjection):
    MODE = TEST_MODE

    def _apply_mode_message(self, envelope):  # pragma: no cover - trivial
        pass


class _Session(BaseSession):
    MODE = TEST_MODE

    def _create_projection(self) -> BaseProjection:
        return _Projection()


def _mock_client(auth: AuthConfig | None = None) -> MagicMock:
    client = MagicMock()
    client.auth = auth

    def send_side_effect(envelope, *, auth=None, timeout=None, raise_on_nack=True):
        return make_ack(ok=True, session_id=envelope.session_id, message_id=envelope.message_id)

    client.send.side_effect = send_side_effect
    return client


class TestConstruction:
    def test_generates_session_id_when_omitted(self):
        session = _Session(_mock_client())
        assert session.session_id

    def test_explicit_session_id_kept(self):
        session = _Session(_mock_client(), session_id=VALID_SESSION_ID)
        assert session.session_id == VALID_SESSION_ID

    def test_invalid_session_id_rejected(self):
        with pytest.raises(MacpSessionError):
            _Session(_mock_client(), session_id="not-a-uuid")


class TestSenderResolution:
    def test_explicit_sender_matching_expected_passes(self):
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(_mock_client(), auth=auth)
        assert session._sender_for("alice") == "alice"

    def test_explicit_sender_mismatching_expected_raises(self):
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(_mock_client(), auth=auth)
        with pytest.raises(MacpIdentityMismatchError):
            session._sender_for("mallory")

    def test_falls_back_to_auth_sender(self):
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(_mock_client(), auth=auth)
        assert session._sender_for(None) == "alice"

    def test_per_call_auth_overrides_session_auth(self):
        session = _Session(_mock_client(), auth=AuthConfig.for_dev_agent("alice"))
        bob = AuthConfig.for_dev_agent("bob")
        assert session._sender_for(None, auth=bob) == "bob"

    def test_empty_when_no_auth_anywhere(self):
        session = _Session(_mock_client(auth=None))
        assert session._sender_for(None) == ""

    def test_uses_client_auth_when_session_has_none(self):
        client = _mock_client(auth=AuthConfig.for_dev_agent("carol"))
        session = _Session(client)
        assert session._sender_for(None) == "carol"


class TestStartAndTracking:
    def test_start_appends_to_projection_on_ok_ack(self):
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(_mock_client(), auth=auth)
        ack = session.start(intent="x", participants=["alice"], ttl_ms=1000)
        assert ack.ok
        assert len(session.projection.transcript) == 1
        assert session.projection.transcript[0].message_type == "SessionStart"

    def test_nack_is_not_applied_to_projection(self):
        auth = AuthConfig.for_dev_agent("alice")
        client = _mock_client()
        client.send.side_effect = None
        client.send.return_value = make_ack(ok=False)
        session = _Session(client, auth=auth)
        ack = session.start(intent="x", participants=["alice"], ttl_ms=1000)
        assert not ack.ok
        assert session.projection.transcript == []

    def test_commit_sets_projection_commitment(self):
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(_mock_client(), auth=auth)
        session.start(intent="x", participants=["alice"], ttl_ms=1000)
        session.commit(action="done", authority_scope="test", reason="ok")
        assert session.projection.is_committed
        assert session.projection.phase == "Committed"


class TestLifecycleDelegation:
    def test_cancel_delegates_to_client(self):
        client = _mock_client()
        session = _Session(client, auth=AuthConfig.for_dev_agent("alice"))
        session.cancel(reason="bye")
        client.cancel_session.assert_called_once()
        assert client.cancel_session.call_args.args[0] == session.session_id

    def test_suspend_and_resume_delegate_to_client(self):
        client = _mock_client()
        session = _Session(client, auth=AuthConfig.for_dev_agent("alice"))
        session.suspend(reason="pause")
        session.resume(reason="go")
        assert client.suspend_session.call_args.args[0] == session.session_id
        assert client.resume_session.call_args.args[0] == session.session_id

    def test_metadata_delegates_to_get_session(self):
        client = _mock_client()
        session = _Session(client, auth=AuthConfig.for_dev_agent("alice"))
        session.metadata()
        assert client.get_session.call_args.args[0] == session.session_id

    def test_open_stream_uses_session_auth(self):
        client = _mock_client()
        auth = AuthConfig.for_dev_agent("alice")
        session = _Session(client, auth=auth)
        session.open_stream()
        client.open_stream.assert_called_once_with(auth=auth)
