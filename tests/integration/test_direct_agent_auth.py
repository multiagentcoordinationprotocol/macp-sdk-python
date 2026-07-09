"""Integration tests for the direct-agent-auth flow (PY-5).

Covers the end-to-end shape that agents will use once the control-plane
stops forging envelopes (see ``ui-console/plans/direct-agent-auth.md``):

    AuthConfig.for_bearer(token, expected_sender=...)  # bearer, guardrail
    session = DecisionSession(client, session_id=preallocated, auth=auth)
    session.start(...)                                 # unary Send(SessionStart)
    stream = session.open_stream()                     # bidi read path
    session.propose(...)                               # first mode-specific Send

The control-plane (or any other orchestrator) pre-allocates the
``session_id`` and distributes it to the initiator agent via bootstrap.

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.

For the Bearer-token variant, the runtime must also expose an
``MACP_AUTH_TOKENS_JSON`` identity that maps ``tok-initiator-integration``
to sender ``initiator-integration``. The Bearer tests are skipped
automatically when the token isn't configured.
"""

from __future__ import annotations

import os

import pytest

from macp_sdk import (
    AuthConfig,
    DecisionSession,
    MacpClient,
    MacpIdentityMismatchError,
    new_session_id,
)
from tests.integration.conftest import RUNTIME_TARGET

BEARER_TOKEN = os.environ.get("MACP_INTEGRATION_BEARER_TOKEN")
BEARER_SENDER = os.environ.get("MACP_INTEGRATION_BEARER_SENDER", "initiator-integration")

pytestmark = pytest.mark.integration


# ── Dev-header initiator flow ─────────────────────────────────────────


class TestDirectAgentAuthDevHeader:
    """Happy path with dev-header auth (covers PY-3 + PY-5 guardrails)."""

    def test_initiator_preallocated_session_id_start_stream_propose(self) -> None:
        session_id = new_session_id()
        auth = AuthConfig.for_dev_agent("coordinator")
        client = MacpClient(target=RUNTIME_TARGET, allow_insecure=True, auth=auth)
        try:
            client.initialize()

            session = DecisionSession(client, session_id=session_id, auth=auth)
            assert session.session_id == session_id

            ack = session.start(
                intent="direct-agent-auth smoke",
                participants=["coordinator", "alice"],
                ttl_ms=30_000,
            )
            assert ack.ok
            assert ack.session_id == session_id

            # Observer/event-loop stream (non-initiators would call this without .start)
            stream = session.open_stream()
            try:
                ack = session.propose(
                    "p1",
                    "deploy-v1",
                    rationale="ship it",
                )
                assert ack.ok

                ack = session.commit(
                    action="deployment.approved",
                    authority_scope="release",
                    reason="coordinator authority",
                )
                assert ack.ok
            finally:
                stream.close()
        finally:
            client.close()

    def test_expected_sender_guardrail_fires_before_wire(self) -> None:
        """Mismatched explicit sender must fail client-side, never hit runtime."""
        auth = AuthConfig.for_dev_agent("coordinator")
        client = MacpClient(target=RUNTIME_TARGET, allow_insecure=True, auth=auth)
        try:
            session = DecisionSession(client, auth=auth)
            with pytest.raises(MacpIdentityMismatchError):
                session.start(
                    intent="should never be sent",
                    participants=["coordinator"],
                    ttl_ms=10_000,
                    sender="mallory",
                )
        finally:
            client.close()


# ── Bearer-token initiator flow ───────────────────────────────────────

bearer_required = pytest.mark.skipif(
    not BEARER_TOKEN,
    reason="Set MACP_INTEGRATION_BEARER_TOKEN + matching MACP_AUTH_TOKENS_JSON entry",
)


@bearer_required
class TestDirectAgentAuthBearer:
    """Bearer-token initiator flow matching the direct-agent-auth target topology.

    Verifies that:

    1. ``MacpClient`` sends ``authorization: Bearer <token>`` metadata (and the
       runtime accepts it without any dev-header shenanigans).
    2. The envelope ``sender`` the runtime records matches the expected_sender
       bound to that token.
    3. ``expected_sender`` blocks mismatched ``sender=`` client-side.
    """

    def _client(self) -> MacpClient:
        assert BEARER_TOKEN is not None
        auth = AuthConfig.for_bearer(BEARER_TOKEN, expected_sender=BEARER_SENDER)
        md = dict(auth.metadata())
        assert md["authorization"] == f"Bearer {BEARER_TOKEN}"
        return MacpClient(target=RUNTIME_TARGET, allow_insecure=True, auth=auth)

    def test_initiator_full_loop(self) -> None:
        session_id = new_session_id()
        client = self._client()
        try:
            client.initialize()

            session = DecisionSession(client, session_id=session_id, auth=client.auth)
            ack = session.start(
                intent="bearer-direct-auth smoke",
                participants=[BEARER_SENDER, "alice"],
                ttl_ms=30_000,
            )
            assert ack.ok
            assert ack.session_id == session_id

            stream = session.open_stream()
            try:
                ack = session.propose("p1", "option-a", rationale="bearer path")
                assert ack.ok

                # Confirm the runtime recorded the Bearer identity as sender.
                meta = session.metadata().metadata
                assert meta.initiator == BEARER_SENDER

                ack = session.commit(
                    action="approved",
                    authority_scope="release",
                    reason="bearer commit",
                )
                assert ack.ok
            finally:
                stream.close()
        finally:
            client.close()

    def test_bearer_guardrail_rejects_spoofed_sender(self) -> None:
        client = self._client()
        try:
            session = DecisionSession(client, auth=client.auth)
            with pytest.raises(MacpIdentityMismatchError):
                session.start(
                    intent="should never be sent",
                    participants=[BEARER_SENDER, "alice"],
                    ttl_ms=10_000,
                    sender="mallory",
                )
        finally:
            client.close()
