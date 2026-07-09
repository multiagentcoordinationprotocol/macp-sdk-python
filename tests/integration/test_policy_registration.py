"""Integration tests for the mutable policy-registry RPCs (RFC-MACP-0012).

Positive-path round-trip against a live runtime with a writable registry:
register → get → list → unregister. The read-only-registry negative path
lives in ``test_absorb_v050.py`` (env-gated on a second runtime).

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import uuid

import pytest

from macp_sdk import MacpClient, build_quorum_policy
from macp_sdk.policy import QuorumThreshold
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration


@pytest.fixture
def admin() -> MacpClient:
    client = make_client("admin")
    yield client
    client.close()


class TestPolicyRegistrationRoundTrip:
    def test_register_get_list_unregister(self, admin: MacpClient) -> None:
        init = admin.initialize()
        if not init.capabilities.policy_registry.register_policy:
            pytest.skip("runtime policy registry is read-only (MACP_POLICIES_DIR)")

        policy_id = f"policy.itest-{uuid.uuid4().hex[:8]}"
        descriptor = build_quorum_policy(
            policy_id,
            "integration round-trip policy",
            threshold=QuorumThreshold(type="percentage", value=60),
        )

        resp = admin.register_policy(descriptor)
        assert resp.ok, f"RegisterPolicy failed: {resp}"
        try:
            got = admin.get_policy(policy_id)
            assert got.policy_descriptor.policy_id == policy_id
            assert got.policy_descriptor.description == "integration round-trip policy"

            listed = admin.list_policies()
            assert policy_id in [d.policy_id for d in listed.descriptors]
        finally:
            resp = admin.unregister_policy(policy_id)
            assert resp.ok, f"UnregisterPolicy failed: {resp}"

        listed = admin.list_policies()
        assert policy_id not in [d.policy_id for d in listed.descriptors]

    def test_reregister_same_id_conflicts(self, admin: MacpClient) -> None:
        """A duplicate policy_id must not silently clobber the first
        registration — the runtime reports it via ok=false or a raised
        ALREADY_EXISTS, never a second ok."""
        init = admin.initialize()
        if not init.capabilities.policy_registry.register_policy:
            pytest.skip("runtime policy registry is read-only (MACP_POLICIES_DIR)")

        policy_id = f"policy.itest-{uuid.uuid4().hex[:8]}"
        descriptor = build_quorum_policy(policy_id, "dup check")
        assert admin.register_policy(descriptor).ok
        try:
            from macp_sdk.errors import MacpAckError

            try:
                second = admin.register_policy(descriptor)
                assert not second.ok, "duplicate register_policy must not succeed twice"
            except MacpAckError:
                pass  # ALREADY_EXISTS surfaced as an ack error — also correct
        finally:
            admin.unregister_policy(policy_id)
