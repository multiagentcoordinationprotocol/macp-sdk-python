"""WatchPolicies authenticates like ListPolicies from runtime 0.12.0
(macp-runtime #274, SDK issue #206)."""

from __future__ import annotations

import uuid

import grpc
import pytest
from macp.v1 import policy_pb2

from macp_sdk import MacpClient, MacpSdkError, build_decision_policy

from .conftest import RUNTIME_TARGET, make_client

pytestmark = pytest.mark.integration


def _version(client: MacpClient) -> tuple[int, ...]:
    raw = client.initialize().runtime_info.version
    return tuple(int(part) for part in raw.split("-")[0].split(".")[:3])


def test_authenticated_watch_sees_a_registered_policy() -> None:
    # The runtime sends the full registry when the stream opens and again on
    # every change, so the policy shows up whether it is registered before or
    # after the subscription lands.
    client = make_client("coordinator")
    policy_id = f"policy.watch-auth-{uuid.uuid4().hex[:8]}"
    client.register_policy(build_decision_policy(policy_id, "watch auth"))
    stream = client.watch_policies(timeout=10)
    try:
        for response in stream:
            if any(d.policy_id == policy_id for d in response.descriptors):
                break
        else:
            pytest.fail(f"{policy_id} never appeared on the stream")
    finally:
        stream.close()
        client.unregister_policy(policy_id)
        client.close()


def test_runtime_rejects_an_unauthenticated_watch() -> None:
    client = make_client("coordinator")
    try:
        if _version(client) < (0, 12, 0):
            pytest.skip("WatchPolicies is open before runtime 0.12.0")
        # Bypass the SDK's own guard: a raw call with no credential at all.
        call = client.stub.WatchPolicies(policy_pb2.WatchPoliciesRequest(), timeout=5)
        with pytest.raises(grpc.RpcError) as raised:
            next(iter(call))
        assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        client.close()


def test_sdk_refuses_to_watch_without_auth() -> None:
    client = MacpClient(target=RUNTIME_TARGET, allow_insecure=True)
    try:
        with pytest.raises(MacpSdkError, match="requires auth"):
            next(client.watch_policies())
    finally:
        client.close()
