"""Integration tests for the client-visible runtime 0.14.0 behaviour.

Pins the rules the SDK suite had no coverage for. Two tests discriminate
against runtime 0.13.2 (a negative Handoff Commitment with no offers is now
allowed; after an accept it is now refused); the rest held on 0.13.2 too and
guard against regression.

- Handoff Commitment readiness (``semantics_rev`` 5, RFC-MACP-0010 §6.1): a
  positive Commitment needs an accepted offer; a negative one is allowed only
  with no offers or every offer declined. Otherwise ``INVALID_ENVELOPE``.
- Lifecycle annotations (``SessionCancel`` / ``SessionSuspend`` /
  ``SessionResume``) are runtime-emitted; submitting one through ``Send`` is
  ``FORBIDDEN`` (RFC-MACP-0001 §7.6 item 9). The dedicated RPCs still work.

Requires runtime >= 0.14.0 started with ``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk import (
    AuthConfig,
    DecisionSession,
    HandoffSession,
    MacpAckError,
    build_envelope,
    serialize_message,
)
from tests.integration.conftest import make_client as _client

pytestmark = pytest.mark.integration


class _Identity(TypedDict):
    sender: str
    auth: AuthConfig


def _as(agent: str) -> _Identity:
    return {"sender": agent, "auth": AuthConfig.for_dev_agent(agent)}


def _start_handoff(session: HandoffSession) -> None:
    ack = session.start(
        intent="rev5 readiness", participants=["owner", "target", "target2"], ttl_ms=30_000
    )
    assert ack.ok


def _commit(session: HandoffSession, *, positive: bool) -> envelope_pb2.Ack:
    return session.commit(
        action="handoff.accepted" if positive else "handoff.declined",
        authority_scope="support",
        reason="rev5 readiness",
        outcome_positive=positive,
    )


def _assert_refused(session: HandoffSession, *, positive: bool) -> None:
    with pytest.raises(MacpAckError) as excinfo:
        _commit(session, positive=positive)
    assert excinfo.value.failure.code == "INVALID_ENVELOPE"


# ── Handoff Commitment readiness (semantics_rev 5) ──


class TestHandoffCommitmentReadiness:
    def test_positive_commit_needs_an_accepted_offer(self) -> None:
        client = _client("owner")
        try:
            session = HandoffSession(client)
            _start_handoff(session)
            # No offer at all: nothing to commit positively to.
            _assert_refused(session, positive=True)

            session.offer("h1", "target", scope="support")
            # Offer still pending: not settled yet.
            _assert_refused(session, positive=True)

            session.accept_handoff("h1", accepted_by="target", **_as("target"))
            assert _commit(session, positive=True).ok
            assert session.handoff_projection.commitment is not None
        finally:
            client.close()

    def test_negative_commit_refused_while_an_offer_is_pending(self) -> None:
        client = _client("owner")
        try:
            session = HandoffSession(client)
            _start_handoff(session)
            session.offer("h1", "target", scope="support")
            _assert_refused(session, positive=False)

            session.decline("h1", declined_by="target", reason="at capacity", **_as("target"))
            # Every offer declined: a negative Commitment is now allowed.
            assert _commit(session, positive=False).ok
        finally:
            client.close()

    def test_negative_commit_allowed_with_no_offers(self) -> None:
        client = _client("owner")
        try:
            session = HandoffSession(client)
            _start_handoff(session)
            assert _commit(session, positive=False).ok
        finally:
            client.close()

    def test_negative_commit_refused_after_an_accept(self) -> None:
        client = _client("owner")
        try:
            session = HandoffSession(client)
            _start_handoff(session)
            session.offer("h1", "target", scope="support")
            session.accept_handoff("h1", accepted_by="target", **_as("target"))
            _assert_refused(session, positive=False)
            session.cancel(reason="test cleanup")
        finally:
            client.close()


# ── Lifecycle annotations via Send are FORBIDDEN (RFC-MACP-0001 §7.6 item 9) ──


_ANNOTATIONS = [
    ("SessionCancel", core_pb2.SessionCancelPayload(reason="via Send")),
    ("SessionSuspend", core_pb2.SessionSuspendPayload(reason="via Send")),
    ("SessionResume", core_pb2.SessionResumePayload(reason="via Send")),
]


class TestAnnotationsViaSendForbidden:
    @pytest.mark.parametrize(("message_type", "payload"), _ANNOTATIONS, ids=lambda v: str(v)[:14])
    def test_annotation_via_send_is_forbidden(self, message_type: str, payload: object) -> None:
        client = _client("coordinator")
        try:
            session = DecisionSession(client)
            assert session.start(
                intent="annotation via Send",
                participants=["coordinator", "alice"],
                ttl_ms=30_000,
            ).ok
            env = build_envelope(
                mode="macp.mode.decision.v1",
                message_type=message_type,
                session_id=session.session_id,
                payload=serialize_message(payload),  # type: ignore[arg-type]
                sender="coordinator",
            )
            with pytest.raises(MacpAckError) as excinfo:
                client.send(env)
            assert excinfo.value.failure.code == "FORBIDDEN"

            # The refused annotation changed nothing: the session is still OPEN
            # and the dedicated RPC still cancels it.
            assert session.metadata().metadata.state == envelope_pb2.SESSION_STATE_OPEN
            assert client.cancel_session(session.session_id, reason="test cleanup").ok
        finally:
            client.close()
