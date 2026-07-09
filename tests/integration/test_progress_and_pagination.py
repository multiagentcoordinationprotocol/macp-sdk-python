"""Integration tests for ``send_progress`` and single-page session listing.

Complements the unit coverage (envelope construction, auto-drain pagination)
with the end-to-end pieces only a live runtime can prove: the runtime
*accepts* a Progress envelope inside a session, and a ``next_page_token``
returned by one ``ListSessions`` call is honoured by the next.

Requires a running MACP runtime on localhost:50051 started with
``MACP_ALLOW_INSECURE=1``.
"""

from __future__ import annotations

import contextlib

import pytest

from macp_sdk import MODE_TASK, DecisionSession, TaskSession, new_session_id
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration


class TestSendProgress:
    def test_progress_inside_task_session_accepted(self) -> None:
        coordinator = make_client("coordinator")
        try:
            coordinator.initialize()
            session = TaskSession(coordinator)
            ack = session.start(
                intent="progress smoke",
                participants=["coordinator", "worker"],
                ttl_ms=30_000,
            )
            assert ack.ok

            ack = coordinator.send_progress(
                session_id=session.session_id,
                mode=MODE_TASK,
                progress_token="t1",
                progress=1.0,
                total=4.0,
                message="quarter done",
            )
            assert ack.ok, f"Progress rejected: {ack}"
            session.cancel(reason="test cleanup")
        finally:
            coordinator.close()


class TestListSessionsPageTokenThreading:
    def test_next_page_token_yields_disjoint_pages(self) -> None:
        coordinator = make_client("coordinator")
        created: list[str] = []
        try:
            coordinator.initialize()
            # Ensure at least two open sessions exist so page_size=1 must
            # produce a continuation token.
            for _ in range(2):
                session = DecisionSession(coordinator, session_id=new_session_id())
                ack = session.start(
                    intent="pagination smoke",
                    participants=["coordinator"],
                    ttl_ms=30_000,
                )
                assert ack.ok
                created.append(session.session_id)

            page1, token1 = coordinator.list_sessions_page(page_size=1)
            if len(page1) > 1 and not token1:
                # macp-proto 0.1.6 defines page_size/page_token and the SDK
                # sends them, but runtime v0.5.0 does not implement pagination
                # server-side (no page handling in its ListSessions) — it
                # returns the full set with an empty token. This test becomes
                # live the moment a runtime honours page_size.
                pytest.skip(
                    "runtime ignores ListSessions page_size (pagination not "
                    "implemented server-side as of runtime v0.5.0)"
                )
            assert len(page1) == 1
            assert token1, "expected a next_page_token with more sessions open"

            page2, _token2 = coordinator.list_sessions_page(page_size=1, page_token=token1)
            assert len(page2) == 1
            assert page1[0].session_id != page2[0].session_id

            # The paged walk and the auto-drain agree on membership.
            drained = {s.session_id for s in coordinator.list_sessions()}
            assert {page1[0].session_id, page2[0].session_id} <= drained
        finally:
            for session_id in created:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    coordinator.cancel_session(session_id, reason="test cleanup")
            coordinator.close()
