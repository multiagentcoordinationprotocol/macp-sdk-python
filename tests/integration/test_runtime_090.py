"""Integration tests for the client-visible runtime 0.9.0 behaviour.

Each test discriminates: it fails against runtime <= 0.8.8, where the rule it
pins did not exist (S4-S7, closed policy rules, the 128-byte session id cap).
Requires runtime >= 0.9.0 started with ``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from macp.modes.decision.v1 import decision_pb2
from macp.v1 import policy_pb2

from macp_sdk import (
    AuthConfig,
    DecisionSession,
    HandoffSession,
    MacpAckError,
    QuorumSession,
    QuorumThreshold,
    TaskSession,
    build_decision_policy,
    build_envelope,
    build_quorum_policy,
    build_session_start_payload,
    effective_quorum_threshold,
    new_session_id,
    serialize_message,
)
from tests.integration.conftest import make_client as _client

pytestmark = pytest.mark.integration


class _Identity(TypedDict):
    sender: str
    auth: AuthConfig


def _as(agent: str) -> _Identity:
    return {"sender": agent, "auth": AuthConfig.for_dev_agent(agent)}


def _unique(prefix: str) -> str:
    # The suite shares one runtime; policy ids must not collide across runs.
    return f"{prefix}.{new_session_id()[:8]}"


# ── S6: a pre-assignment TaskReject is a personal decline (RFC-MACP-0009 §5 rule 2a) ──


class TestTaskPersonalDecline:
    def test_decliner_is_barred_but_another_participant_may_accept(self) -> None:
        client = _client("planner")
        try:
            session = TaskSession(client)
            session.start(intent="s6", participants=["w1", "w2"], ttl_ms=30_000)
            session.request_task("t1", "Build")
            session.reject_task("t1", assignee="w1", reason="busy", **_as("w1"))
            proj = session.task_projection

            assert proj.has_declined("w1") and not proj.can_accept("w1")
            with pytest.raises(MacpAckError) as excinfo:
                session.accept_task("t1", assignee="w1", **_as("w1"))
            assert excinfo.value.failure.code == "INVALID_ENVELOPE"

            # Runtime <= 0.8.8 refused every accept after any reject.
            assert proj.can_accept("w2")
            assert session.accept_task("t1", assignee="w2", **_as("w2")).ok
            session.cancel(reason="test cleanup")
        finally:
            client.close()

    def test_named_assignee_decline_makes_a_negative_commit_eligible(self) -> None:
        client = _client("planner")
        try:
            session = TaskSession(client)
            session.start(intent="s6", participants=["w1", "w2"], ttl_ms=30_000)
            session.request_task("t1", "Build", requested_assignee="w1")
            session.reject_task("t1", assignee="w1", reason="busy", **_as("w1"))

            assert session.task_projection.negative_commit_eligible("t1")
            ack = session.commit(
                action="task.declined",
                authority_scope="test",
                reason="named assignee declined",
                outcome_positive=False,
            )
            assert ack.ok
        finally:
            client.close()


# ── S5: a declined target is barred from re-offers (RFC-MACP-0010 §5 rule 3a) ──


class TestHandoffDeclinedTargetBar:
    def test_reoffer_to_decliner_refused_new_target_accepted(self) -> None:
        client = _client("owner")
        try:
            session = HandoffSession(client)
            session.start(intent="s5", participants=["owner", "target", "target2"], ttl_ms=30_000)
            session.offer("h1", "target", scope="support")
            session.decline("h1", declined_by="target", reason="at capacity", **_as("target"))
            proj = session.handoff_projection

            assert not proj.can_offer_to("target")
            with pytest.raises(MacpAckError) as excinfo:
                session.offer("h2", "target", scope="support")
            assert excinfo.value.failure.code == "INVALID_ENVELOPE"

            assert proj.can_offer_to("target2")
            assert session.offer("h3", "target2", scope="support").ok
            session.cancel(reason="test cleanup")
        finally:
            client.close()


# ── S4: a policy threshold override applies as given (RFC-MACP-0011 §5 rule 6) ──


class TestQuorumThresholdOverride:
    def test_over_count_override_allows_an_immediate_negative_commit(self) -> None:
        client = _client("coordinator")
        try:
            policy = build_quorum_policy(
                _unique("policy.sdk-it.q5"),
                "n_of_m 5 over 3 participants: unreachable",
                threshold=QuorumThreshold(type="n_of_m", value=5),
            )
            assert client.register_policy(policy).ok
            participants = ["alice", "bob", "carol"]
            session = QuorumSession(client, policy_version=policy.policy_id)
            session.start(intent="s4", participants=participants, ttl_ms=30_000)
            # Runtime <= 0.8.8 refused an override above the participant count here.
            session.request_approval("r1", "deploy", required_approvals=2)

            proj = session.quorum_projection
            t = effective_quorum_threshold(policy, len(participants), 2)
            assert t == 5
            assert not proj.is_threshold_unreachable("r1", len(participants))
            assert proj.is_threshold_unreachable("r1", len(participants), effective_threshold=t)

            ack = session.commit(
                action="quorum.rejected",
                authority_scope="test",
                reason="threshold unreachable",
                outcome_positive=False,
            )
            assert ack.ok
        finally:
            client.close()


# ── S7: Decision casing is enforced on the wire (semantics_rev 4) ──


class TestDecisionCasing:
    def _session(self, client: object) -> DecisionSession:
        session = DecisionSession(client)  # type: ignore[arg-type]
        session.start(intent="s7", participants=["coordinator", "alice"], ttl_ms=30_000)
        session.propose("p1", "option-a")
        return session

    def _raw(self, session: DecisionSession, message_type: str, payload: object) -> None:
        env = build_envelope(
            mode="macp.mode.decision.v1",
            message_type=message_type,
            session_id=session.session_id,
            payload=serialize_message(payload),  # type: ignore[arg-type]
            sender="alice",
        )
        session.client.send(env, auth=AuthConfig.for_dev_agent("alice"))

    def test_lowercase_raw_vote_refused(self) -> None:
        client = _client("coordinator")
        try:
            session = self._session(client)
            with pytest.raises(MacpAckError) as excinfo:
                self._raw(
                    session, "Vote", decision_pb2.VotePayload(proposal_id="p1", vote="approve")
                )
            assert excinfo.value.failure.code == "INVALID_ENVELOPE"
            session.cancel(reason="test cleanup")
        finally:
            client.close()

    def test_empty_raw_objection_severity_refused(self) -> None:
        client = _client("coordinator")
        try:
            session = self._session(client)
            payload = decision_pb2.ObjectionPayload(proposal_id="p1", reason="no", severity="")
            with pytest.raises(MacpAckError) as excinfo:
                self._raw(session, "Objection", payload)
            assert excinfo.value.failure.code == "INVALID_ENVELOPE"
            session.cancel(reason="test cleanup")
        finally:
            client.close()

    def test_sdk_vote_is_normalised_and_accepted(self) -> None:
        client = _client("coordinator")
        try:
            session = self._session(client)
            assert session.vote("p1", "approve", **_as("alice")).ok
            session.cancel(reason="test cleanup")
        finally:
            client.close()


# ── Closed policy rule objects (RFC-MACP-0012 §4) ──


def _raw_descriptor(name: str, rules: dict[str, object], schema_version: int = 1) -> object:
    return policy_pb2.PolicyDescriptor(
        policy_id=_unique(f"policy.sdk-it.{name}"),
        mode="macp.mode.quorum.v1",
        description="admission matrix",
        rules=json.dumps(rules),
        schema_version=schema_version,
    )


class TestPolicyAdmission:
    """RegisterPolicy reports a refused descriptor in the response (``ok:
    false``, ``error`` prefixed with the registry code), not as an RPC error."""

    def test_builder_descriptors_register(self) -> None:
        client = _client("admin")
        try:
            assert client.register_policy(build_quorum_policy(_unique("policy.sdk-it.q"), "d")).ok
            assert client.register_policy(build_decision_policy(_unique("policy.sdk-it.d"), "d")).ok
        finally:
            client.close()

    @pytest.mark.parametrize(
        ("name", "rules", "schema_version"),
        [
            ("unknown-key", {"threshold": {"type": "n_of_m", "value": 1}, "bogus": {}}, 1),
            ("schema-v4", {"threshold": {"type": "n_of_m", "value": 1}}, 4),
            ("audit-block", {"audit": {"level": "info"}}, 1),
        ],
    )
    def test_raw_descriptor_refused(
        self, name: str, rules: dict[str, object], schema_version: int
    ) -> None:
        client = _client("admin")
        try:
            resp = client.register_policy(_raw_descriptor(name, rules, schema_version))  # type: ignore[arg-type]
            assert not resp.ok
            assert resp.error.startswith("INVALID_POLICY_DEFINITION"), resp.error
        finally:
            client.close()

    def test_unregistered_extension_key_refused_by_stock_image(self) -> None:
        client = _client("admin")
        try:
            policy = build_quorum_policy(
                _unique("policy.sdk-it.x"), "d", extensions={"x-com.acme.audit": {"k": 1}}
            )
            resp = client.register_policy(policy)
            assert not resp.ok
            assert resp.error.startswith("INVALID_POLICY_DEFINITION"), resp.error
            assert "implemented: []" in resp.error
        finally:
            client.close()

    def test_initialize_advertises_no_rule_extensions(self) -> None:
        if "rule_extensions" not in policy_pb2.PolicyRegistryCapability.DESCRIPTOR.fields_by_name:
            pytest.skip("installed macp-proto predates PolicyRegistryCapability.rule_extensions")
        client = _client("admin")
        try:
            caps = client.initialize().capabilities.policy_registry
            assert list(caps.rule_extensions) == []
        finally:
            client.close()


# ── Session id cap: 128 bytes (RFC-MACP-0004 §11.2) ──


class TestSessionIdCap:
    def test_129_char_session_id_refused(self) -> None:
        client = _client("coordinator")
        try:
            payload = build_session_start_payload(
                intent="cap", participants=["coordinator", "peer"], ttl_ms=30_000
            )
            env = build_envelope(
                mode="macp.mode.decision.v1",
                message_type="SessionStart",
                session_id="a" * 129,
                payload=serialize_message(payload),
                sender="coordinator",
            )
            with pytest.raises(MacpAckError) as excinfo:
                client.send(env)
            assert excinfo.value.failure.code == "INVALID_SESSION_ID"
        finally:
            client.close()
