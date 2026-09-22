"""Integration test for the runtime v0.8.0 handoff implicit-accept path.

RFC-MACP-0010 §5.1(2)/(3): when an outstanding ``HandoffOffer``'s
``implicit_accept_timeout_ms`` elapses unactioned, ``macp-runtime`` >= 0.8.0
synthesizes a real ``HandoffAccept`` envelope (``sender`` = the offer's
target, ``message_id = "implicit-accept:<handoff_id>"``, payload
``implicit=True``) and appends it to accepted history. Every runtime
version before 0.8.0 left the offer observably stuck at ``"offered"``
forever — no client-visible envelope was ever produced.

``tests/unit/test_absorb_runtime_v050.py`` (B-3) already proves the SDK
*decodes* a hand-built version of this envelope correctly. This test proves
a *live* runtime actually emits it, on schedule, over the real gRPC
boundary.

The runtime only synthesizes a due accept in two places: an eager sweep on
``MACP_CLEANUP_INTERVAL_SECS`` (default 60s — too slow for a test) and a
lazy check that runs before *any* subsequent message on the session is
processed (``Runtime::handle_send``, ahead of ``mode.on_message_at``). This
test forces the lazy path deterministically by sending a follow-up
``HandoffContext`` for the same handoff once the timeout has elapsed — late
context after accept/decline is permitted by RFC-MACP-0010 §2.1
(``tests/unit/test_handoff_context_after_accept.py`` covers the SDK side of
that), so the touch message itself succeeds whether or not the implicit
accept has landed yet, and the synthesis it triggers happens strictly
before that message is evaluated.

Requires a running MACP runtime (>= 0.8.0) on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``. Against an older runtime this test times out —
the assertion message names the version requirement.
"""

from __future__ import annotations

import queue
import time
import uuid

import pytest
from macp.v1 import envelope_pb2

from macp_sdk import AuthConfig, HandoffSession, MacpStream, new_session_id
from macp_sdk.policy import HandoffAcceptanceRules, build_handoff_policy
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration

IMPLICIT_ACCEPT_TIMEOUT_MS = 500
POLL_CEILING_S = 5.0


def _auth(agent_id: str) -> AuthConfig:
    return AuthConfig.for_dev_agent(agent_id)


def _wait_for_message_type(
    stream: MacpStream, session_id: str, message_type: str, timeout_s: float
) -> envelope_pb2.Envelope | None:
    """Poll *stream* for the first envelope of *message_type* in *session_id*."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            env = stream.read(timeout=1.0)
        except queue.Empty:
            continue
        if env is None:
            return None
        if env.session_id != session_id or env.message_type != message_type:
            continue
        return env
    return None


class TestHandoffImplicitAccept:
    def test_timeout_elapses_into_synthetic_accept(self) -> None:
        coordinator = make_client("coordinator")
        try:
            init = coordinator.initialize()
            if not init.capabilities.policy_registry.register_policy:
                pytest.skip("runtime policy registry is read-only (MACP_POLICIES_DIR)")

            policy_id = f"policy.itest-handoff-implicit-{uuid.uuid4().hex[:8]}"
            descriptor = build_handoff_policy(
                policy_id,
                "handoff implicit-accept integration test",
                acceptance=HandoffAcceptanceRules(
                    implicit_accept_timeout_ms=IMPLICIT_ACCEPT_TIMEOUT_MS
                ),
            )
            assert coordinator.register_policy(descriptor).ok
            try:
                session_id = new_session_id()
                session = HandoffSession(
                    coordinator,
                    session_id=session_id,
                    policy_version=policy_id,
                    auth=_auth("coordinator"),
                )
                try:
                    ack = session.start(
                        intent="handoff implicit-accept integration test",
                        participants=["coordinator", "bob"],
                        ttl_ms=30_000,
                    )
                    assert ack.ok

                    ack = session.offer("h1", "bob", scope="support", reason="unattended handoff")
                    assert ack.ok

                    # Deliberately never send an explicit accept/decline for
                    # h1 — the point is to let implicit_accept_timeout_ms
                    # elapse unactioned so the runtime synthesizes the accept.
                    observer = make_client("bob")
                    try:
                        stream = observer.open_stream()
                        try:
                            stream.send_subscribe(session_id)

                            # Let the timeout elapse, then force the lazy
                            # synthesis path (see module docstring) instead of
                            # waiting on the 60s-default eager sweep.
                            time.sleep((IMPLICIT_ACCEPT_TIMEOUT_MS / 1000) + 0.2)
                            touch_ack = session.add_context(
                                "h1", content_type="text/plain", context=b"touch"
                            )
                            assert touch_ack.ok

                            synthetic = _wait_for_message_type(
                                stream, session_id, "HandoffAccept", POLL_CEILING_S
                            )
                            assert synthetic is not None, (
                                "no HandoffAccept observed within "
                                f"{POLL_CEILING_S}s of a {IMPLICIT_ACCEPT_TIMEOUT_MS}ms "
                                "implicit_accept_timeout_ms elapsing; this behavior "
                                "requires macp-runtime >= 0.8.0 (RFC-MACP-0010 "
                                "§5.1) — check the runtime version if this times out"
                            )
                            assert synthetic.message_id == "implicit-accept:h1"
                            assert synthetic.sender == "bob"
                            session.handoff_projection.apply_envelope(synthetic)
                        finally:
                            stream.close()
                    finally:
                        observer.close()

                    proj = session.handoff_projection
                    assert proj.is_accepted("h1")
                    assert proj.is_implicitly_accepted("h1") is True
                    record = proj.get_handoff("h1")
                    assert record is not None
                    assert record.accepted_by == "bob"

                    # Prove this is the runtime's own persisted state, not
                    # just what one client's live stream happened to
                    # deliver: a fresh connection (new client, new stream)
                    # replaying from the start of history must see the same
                    # synthetic accept. Reuses the "bob" identity since only
                    # declared participants/observers may subscribe.
                    replay_client = make_client("bob")
                    try:
                        replay_stream = replay_client.open_stream()
                        try:
                            replay_stream.send_subscribe(session_id, after_sequence=0)
                            replayed = _wait_for_message_type(
                                replay_stream, session_id, "HandoffAccept", 5.0
                            )
                            assert replayed is not None, (
                                "replay from sequence 0 did not include the synthetic accept"
                            )
                            assert replayed.message_id == "implicit-accept:h1"
                        finally:
                            replay_stream.close()
                    finally:
                        replay_client.close()
                finally:
                    session.cancel(reason="test cleanup")
            finally:
                coordinator.unregister_policy(policy_id)
        finally:
            coordinator.close()
