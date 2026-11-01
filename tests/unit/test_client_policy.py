"""Unit tests for MacpClient governance policy RPCs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from macp.v1 import policy_pb2

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient
from macp_sdk.errors import MacpSdkError
from macp_sdk.policy import (
    CommitmentRules,
    VotingRules,
    build_decision_policy,
)
from macp_sdk.watchers import PolicyWatcher


@pytest.fixture
def dev_auth() -> AuthConfig:
    return AuthConfig.for_dev_agent("test-agent")


@pytest.fixture
def mock_stub() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_client(dev_auth: AuthConfig, mock_stub: MagicMock) -> MagicMock:
    """A MagicMock that mimics MacpClient for policy RPC tests."""
    from macp_sdk.client import MacpClient

    client = MagicMock(spec=MacpClient)
    client.stub = mock_stub
    client.default_timeout = 5.0
    client._require_auth = MagicMock(return_value=dev_auth)
    client._metadata = MagicMock(return_value=[("x-macp-sender", "test-agent")])

    # Wire through real methods by calling stub directly
    def register_policy(descriptor, *, auth=None, timeout=None):
        return mock_stub.RegisterPolicy(
            policy_pb2.RegisterPolicyRequest(policy_descriptor=descriptor),
            metadata=client._metadata(dev_auth),
            timeout=timeout or client.default_timeout,
        )

    def unregister_policy(policy_id, *, auth=None, timeout=None):
        return mock_stub.UnregisterPolicy(
            policy_pb2.UnregisterPolicyRequest(policy_id=policy_id),
            metadata=client._metadata(dev_auth),
            timeout=timeout or client.default_timeout,
        )

    def get_policy(policy_id, *, auth=None, timeout=None):
        return mock_stub.GetPolicy(
            policy_pb2.GetPolicyRequest(policy_id=policy_id),
            metadata=client._metadata(dev_auth),
            timeout=timeout or client.default_timeout,
        )

    def list_policies(mode=None, *, auth=None, timeout=None):
        return mock_stub.ListPolicies(
            policy_pb2.ListPoliciesRequest(mode=mode or ""),
            metadata=client._metadata(dev_auth),
            timeout=timeout or client.default_timeout,
        )

    client.register_policy = register_policy
    client.unregister_policy = unregister_policy
    client.get_policy = get_policy
    client.list_policies = list_policies

    return client


class TestRegisterPolicy:
    def test_register_sends_descriptor(self, mock_client, mock_stub):
        desc = build_decision_policy("pol-test", "test policy")
        mock_stub.RegisterPolicy.return_value = policy_pb2.RegisterPolicyResponse(ok=True)

        resp = mock_client.register_policy(desc)

        assert resp.ok is True
        call_args = mock_stub.RegisterPolicy.call_args
        req = call_args[0][0]
        assert isinstance(req, policy_pb2.RegisterPolicyRequest)
        assert req.policy_descriptor.policy_id == "pol-test"
        assert req.policy_descriptor.mode == "macp.mode.decision.v1"

    def test_register_with_custom_rules(self, mock_client, mock_stub):
        desc = build_decision_policy(
            "pol-custom",
            "custom rules",
            voting=VotingRules(algorithm="supermajority", threshold=0.67),
        )
        mock_stub.RegisterPolicy.return_value = policy_pb2.RegisterPolicyResponse(ok=True)

        resp = mock_client.register_policy(desc)
        assert resp.ok is True

        req = mock_stub.RegisterPolicy.call_args[0][0]
        rules = json.loads(req.policy_descriptor.rules)
        assert rules["voting"]["algorithm"] == "supermajority"
        assert rules["voting"]["threshold"] == 0.67

    def test_register_error_response(self, mock_client, mock_stub):
        mock_stub.RegisterPolicy.return_value = policy_pb2.RegisterPolicyResponse(
            ok=False, error="INVALID_POLICY_DEFINITION"
        )
        resp = mock_client.register_policy(build_decision_policy("bad-pol", "bad"))
        assert resp.ok is False
        assert resp.error == "INVALID_POLICY_DEFINITION"


class TestUnregisterPolicy:
    def test_unregister_sends_policy_id(self, mock_client, mock_stub):
        mock_stub.UnregisterPolicy.return_value = policy_pb2.UnregisterPolicyResponse(ok=True)

        resp = mock_client.unregister_policy("pol-remove")

        assert resp.ok is True
        req = mock_stub.UnregisterPolicy.call_args[0][0]
        assert isinstance(req, policy_pb2.UnregisterPolicyRequest)
        assert req.policy_id == "pol-remove"

    def test_unregister_unknown_policy(self, mock_client, mock_stub):
        mock_stub.UnregisterPolicy.return_value = policy_pb2.UnregisterPolicyResponse(ok=False)

        resp = mock_client.unregister_policy("nonexistent")
        assert resp.ok is False


class TestGetPolicy:
    def test_get_returns_descriptor(self, mock_client, mock_stub):
        expected = policy_pb2.PolicyDescriptor(
            policy_id="pol-get",
            mode="macp.mode.decision.v1",
            description="retrieved policy",
            schema_version=1,
            rules=json.dumps({"voting": {"algorithm": "majority"}}).encode(),
        )
        mock_stub.GetPolicy.return_value = policy_pb2.GetPolicyResponse(policy_descriptor=expected)

        resp = mock_client.get_policy("pol-get")

        assert resp.policy_descriptor.policy_id == "pol-get"
        assert resp.policy_descriptor.description == "retrieved policy"
        rules = json.loads(resp.policy_descriptor.rules)
        assert rules["voting"]["algorithm"] == "majority"

        req = mock_stub.GetPolicy.call_args[0][0]
        assert req.policy_id == "pol-get"


class TestListPolicies:
    def test_list_all(self, mock_client, mock_stub):
        desc1 = policy_pb2.PolicyDescriptor(policy_id="pol-1", mode="macp.mode.decision.v1")
        desc2 = policy_pb2.PolicyDescriptor(policy_id="pol-2", mode="macp.mode.quorum.v1")
        mock_stub.ListPolicies.return_value = policy_pb2.ListPoliciesResponse(
            descriptors=[desc1, desc2]
        )

        resp = mock_client.list_policies()

        assert len(resp.descriptors) == 2
        assert resp.descriptors[0].policy_id == "pol-1"
        assert resp.descriptors[1].policy_id == "pol-2"

        req = mock_stub.ListPolicies.call_args[0][0]
        assert req.mode == ""

    def test_list_filtered_by_mode(self, mock_client, mock_stub):
        desc = policy_pb2.PolicyDescriptor(policy_id="pol-d", mode="macp.mode.decision.v1")
        mock_stub.ListPolicies.return_value = policy_pb2.ListPoliciesResponse(descriptors=[desc])

        resp = mock_client.list_policies(mode="macp.mode.decision.v1")

        assert len(resp.descriptors) == 1
        req = mock_stub.ListPolicies.call_args[0][0]
        assert req.mode == "macp.mode.decision.v1"

    def test_list_empty(self, mock_client, mock_stub):
        mock_stub.ListPolicies.return_value = policy_pb2.ListPoliciesResponse(descriptors=[])

        resp = mock_client.list_policies()
        assert len(resp.descriptors) == 0


class TestWatchPolicies:
    def test_watch_yields_events(self, mock_stub):
        """Verify the watch_policies pattern yields streaming responses."""
        event1 = policy_pb2.WatchPoliciesResponse(
            descriptors=[
                policy_pb2.PolicyDescriptor(policy_id="pol-new", mode="macp.mode.decision.v1")
            ],
            observed_at_unix_ms=1000,
        )
        event2 = policy_pb2.WatchPoliciesResponse(
            descriptors=[
                policy_pb2.PolicyDescriptor(policy_id="pol-updated", mode="macp.mode.task.v1")
            ],
            observed_at_unix_ms=2000,
        )
        mock_stub.WatchPolicies.return_value = iter([event1, event2])

        # Simulate the watch_policies generator pattern
        call = mock_stub.WatchPolicies(policy_pb2.WatchPoliciesRequest(), timeout=5.0)
        events = list(call)

        assert len(events) == 2
        assert events[0].descriptors[0].policy_id == "pol-new"
        assert events[0].observed_at_unix_ms == 1000
        assert events[1].descriptors[0].policy_id == "pol-updated"


class TestWatchPoliciesAuth:
    """WatchPolicies authenticates like ListPolicies from runtime 0.12.0 (#206)."""

    def test_requires_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        client.stub = MagicMock()
        with pytest.raises(MacpSdkError, match="requires auth"):
            list(client.watch_policies())
        client.stub.WatchPolicies.assert_not_called()

    def test_forwards_client_auth(self):
        client = MacpClient(
            target="localhost:0", allow_insecure=True, auth=AuthConfig.for_bearer("tok")
        )
        client.stub = MagicMock()
        client.stub.WatchPolicies.return_value = iter([policy_pb2.WatchPoliciesResponse()])
        assert len(list(client.watch_policies())) == 1
        metadata = list(client.stub.WatchPolicies.call_args.kwargs["metadata"])
        assert ("authorization", "Bearer tok") in metadata

    def test_per_call_auth_overrides_client_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        client.stub = MagicMock()
        client.stub.WatchPolicies.return_value = iter([])
        list(client.watch_policies(auth=AuthConfig.for_dev_agent("alice")))
        metadata = list(client.stub.WatchPolicies.call_args.kwargs["metadata"])
        assert ("authorization", "Bearer alice") in metadata

    def test_policy_watcher_without_auth_raises(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        client.stub = MagicMock()
        with pytest.raises(MacpSdkError):
            list(PolicyWatcher(client).changes())


class TestPolicyRoundTrip:
    """Test that policies built with helpers can be sent through the wire format."""

    def test_decision_policy_roundtrip(self, mock_client, mock_stub):
        """Build → register → get should preserve all rule fields."""
        original = build_decision_policy(
            "pol-rt",
            "roundtrip test",
            voting=VotingRules(
                algorithm="weighted",
                threshold=0.75,
                quorum_type="percentage",
                quorum_value=0.5,
                weights={"lead": 3.0, "reviewer": 1.0},
            ),
            commitment=CommitmentRules(
                authority="designated_role",
                designated_roles=["lead"],
                require_vote_quorum=True,
            ),
        )

        # Simulate register + get round-trip
        mock_stub.RegisterPolicy.return_value = policy_pb2.RegisterPolicyResponse(ok=True)
        mock_stub.GetPolicy.return_value = policy_pb2.GetPolicyResponse(policy_descriptor=original)

        mock_client.register_policy(original)
        resp = mock_client.get_policy("pol-rt")
        retrieved = resp.policy_descriptor

        assert retrieved.policy_id == original.policy_id
        assert retrieved.mode == original.mode
        assert retrieved.schema_version == original.schema_version

        original_rules = json.loads(original.rules)
        retrieved_rules = json.loads(retrieved.rules)
        assert original_rules == retrieved_rules
        assert retrieved_rules["voting"]["weights"]["lead"] == 3.0
        assert retrieved_rules["commitment"]["designated_roles"] == ["lead"]
