"""Unit coverage for the runtime v0.5.0 / macp-proto 0.1.6 absorption.

Grouped by the new wire surface and behavior:
- ``max_suspend_ms`` on SessionStart (B-2)
- ``ListSessions`` pagination (B-4)
- Quorum threshold integer + range validation (B-5)
- Handoff ``implicit`` accept surfacing + client-accept regression (B-3)
- Read-only policy registry + ext-mode Commitment guard (B-6)
- ``WatchSignals`` auth + watch/stream error-code preservation (C-2/C-3)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import grpc
import pytest
from macp.modes.handoff.v1 import handoff_pb2
from macp.v1 import core_pb2, envelope_pb2

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient, MacpStream
from macp_sdk.constants import MODE_HANDOFF
from macp_sdk.envelope import build_session_start_payload
from macp_sdk.errors import (
    MacpAckError,
    MacpSdkError,
    MacpSessionError,
    MacpTransportError,
)
from macp_sdk.handoff import HandoffProjection, HandoffSession
from macp_sdk.policy import QuorumThreshold, build_quorum_policy
from tests.conftest import (
    FakeRpcError as _FakeRpcError,
)
from tests.conftest import (
    client_with_stub as _client_with_stub,
)
from tests.conftest import (
    make_ack,
    make_envelope,
)

# ── B-2: max_suspend_ms ───────────────────────────────────────────────


class TestMaxSuspendMs:
    def test_set_when_nonzero_round_trips(self):
        payload = build_session_start_payload(
            intent="x", participants=["a"], ttl_ms=1000, max_suspend_ms=5000
        )
        assert payload.max_suspend_ms == 5000
        rt = core_pb2.SessionStartPayload()
        rt.ParseFromString(payload.SerializeToString())
        assert rt.max_suspend_ms == 5000

    def test_default_not_serialized(self):
        payload = build_session_start_payload(intent="x", participants=["a"], ttl_ms=1000)
        # proto3 does not serialize a scalar 0 — keeps byte-compat with pre-0.1.5.
        set_fields = {f.name for f, _ in payload.ListFields()}
        assert "max_suspend_ms" not in set_fields
        assert payload.max_suspend_ms == 0

    def test_negative_rejected(self):
        with pytest.raises(MacpSessionError):
            build_session_start_payload(
                intent="x", participants=["a"], ttl_ms=1000, max_suspend_ms=-1
            )


# ── B-4: ListSessions pagination ──────────────────────────────────────


class TestListSessionsPagination:
    def test_auto_paginates_across_pages(self):
        client, stub = _client_with_stub()
        page1 = core_pb2.ListSessionsResponse(
            sessions=[core_pb2.SessionMetadata(session_id="a")], next_page_token="t1"
        )
        page2 = core_pb2.ListSessionsResponse(
            sessions=[core_pb2.SessionMetadata(session_id="b")], next_page_token=""
        )
        stub.ListSessions.side_effect = [page1, page2]

        out = client.list_sessions(page_size=1)

        assert [s.session_id for s in out] == ["a", "b"]
        assert stub.ListSessions.call_count == 2
        second_req = stub.ListSessions.call_args_list[1].args[0]
        assert second_req.page_token == "t1"
        assert second_req.page_size == 1

    def test_single_page_short_circuits(self):
        client, stub = _client_with_stub()
        stub.ListSessions.return_value = core_pb2.ListSessionsResponse(
            sessions=[core_pb2.SessionMetadata(session_id="a")], next_page_token=""
        )
        out = client.list_sessions()
        assert [s.session_id for s in out] == ["a"]
        stub.ListSessions.assert_called_once()

    def test_list_sessions_page_returns_token(self):
        client, stub = _client_with_stub()
        stub.ListSessions.return_value = core_pb2.ListSessionsResponse(
            sessions=[core_pb2.SessionMetadata(session_id="a")], next_page_token="next"
        )
        sessions, token = client.list_sessions_page(page_size=1)
        assert [s.session_id for s in sessions] == ["a"]
        assert token == "next"


# ── B-5: Quorum threshold integer ─────────────────────────────────────


class TestQuorumThreshold:
    def test_percentage_valid_emits_integer(self):
        desc = build_quorum_policy("q", "d", threshold=QuorumThreshold(type="percentage", value=75))
        rules = json.loads(desc.rules)
        assert rules["threshold"] == {"type": "percentage", "value": 75}
        assert isinstance(rules["threshold"]["value"], int)

    def test_n_of_m_count(self):
        desc = build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=3))
        rules = json.loads(desc.rules)
        assert rules["threshold"] == {"type": "n_of_m", "value": 3}

    def test_percentage_over_100_rejected(self):
        with pytest.raises(MacpSessionError):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="percentage", value=150))

    def test_negative_rejected(self):
        with pytest.raises(MacpSessionError):
            build_quorum_policy("q", "d", threshold=QuorumThreshold(type="n_of_m", value=-1))


# ── B-3: Handoff implicit accept ──────────────────────────────────────


def _offer(proj: HandoffProjection, handoff_id: str = "h1", target: str = "bob") -> None:
    proj.apply_envelope(
        make_envelope(
            MODE_HANDOFF,
            "HandoffOffer",
            handoff_pb2.HandoffOfferPayload(handoff_id=handoff_id, target_participant=target),
            sender="alice",
        )
    )


def _synthetic_accept(handoff_id: str = "h1", target: str = "bob") -> envelope_pb2.Envelope:
    """A runtime-synthesized implicit accept (RFC-MACP-0010 §5.1).

    Runtime v0.5.0 does not yet emit these; this exercises the SDK's forward
    compatibility for histories that carry them.
    """
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=MODE_HANDOFF,
        message_type="HandoffAccept",
        message_id=f"implicit-accept:{handoff_id}",
        session_id="test-session",
        sender=target,
        timestamp_unix_ms=1,
        payload=handoff_pb2.HandoffAcceptPayload(
            handoff_id=handoff_id, accepted_by=target, implicit=True
        ).SerializeToString(),
    )


class TestHandoffImplicit:
    def test_synthetic_accept_sets_implicit_and_phase(self):
        proj = HandoffProjection()
        _offer(proj)
        proj.apply_envelope(_synthetic_accept())
        record = proj.get_handoff("h1")
        assert record is not None
        assert record.status == "accepted"
        assert record.implicit is True
        assert proj.is_implicitly_accepted("h1") is True
        assert proj.phase == "Accepted"

    def test_explicit_client_accept_is_not_implicit(self):
        # Regression: accept_handoff must never set implicit=true on the wire
        # (RFC-MACP-0010 §5.1 — the runtime rejects a client-submitted True).
        client = MagicMock()
        client.auth = None
        client.send.return_value = make_ack(ok=True)
        session = HandoffSession(
            client, session_id="handoff-session-000000001", auth=AuthConfig.for_dev_agent("bob")
        )
        session.accept_handoff("h1")
        envelope = client.send.call_args.args[0]
        payload = handoff_pb2.HandoffAcceptPayload()
        payload.ParseFromString(envelope.payload)
        assert payload.implicit is False

    def test_participant_on_terminal_fires_on_synthetic_accept(self):
        from macp_sdk.agent.participant import Participant

        fired: list = []
        participant = Participant(
            participant_id="obs", session_id="test-session", mode=MODE_HANDOFF, client=MagicMock()
        )
        participant.on_terminal(lambda result: fired.append(result))

        proj = participant.projection
        assert isinstance(proj, HandoffProjection)
        participant.process_event(
            make_envelope(
                MODE_HANDOFF,
                "HandoffOffer",
                handoff_pb2.HandoffOfferPayload(handoff_id="h1", target_participant="bob"),
                sender="alice",
            )
        )
        participant.process_event(_synthetic_accept())

        assert participant.is_stopped
        assert len(fired) == 1
        assert fired[0].state == "Accepted"


# ── B-6: Read-only registry + ext-mode guard ──────────────────────────


class TestReadOnlyPolicyRegistry:
    def test_register_policy_failed_precondition_maps_to_ackerror(self):
        client, stub = _client_with_stub()
        stub.RegisterPolicy.side_effect = _FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION, "registry read-only"
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.register_policy(build_quorum_policy("q", "d"))
        assert excinfo.value.failure.code == "FAILED_PRECONDITION"
        assert "read-only" in excinfo.value.failure.message

    def test_register_policy_other_error_maps_to_transport(self):
        client, stub = _client_with_stub()
        stub.RegisterPolicy.side_effect = _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")
        with pytest.raises(MacpTransportError) as excinfo:
            client.register_policy(build_quorum_policy("q", "d"))
        assert excinfo.value.code == "UNAVAILABLE"

    def test_unregister_policy_failed_precondition_maps_to_ackerror(self):
        client, stub = _client_with_stub()
        stub.UnregisterPolicy.side_effect = _FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION, "registry read-only"
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.unregister_policy("q")
        assert excinfo.value.failure.code == "FAILED_PRECONDITION"


class TestExtModeCommitmentGuard:
    def test_missing_commitment_terminal_rejected(self):
        client, stub = _client_with_stub()
        desc = core_pb2.ModeDescriptor(mode="ext.x.v1", terminal_message_types=["Done"])
        with pytest.raises(MacpSessionError):
            client.register_ext_mode(desc)
        stub.RegisterExtMode.assert_not_called()

    def test_commitment_terminal_passes(self):
        client, stub = _client_with_stub()
        desc = core_pb2.ModeDescriptor(mode="ext.x.v1", terminal_message_types=["Commitment"])
        stub.RegisterExtMode.return_value = core_pb2.RegisterExtModeResponse()
        client.register_ext_mode(desc)
        stub.RegisterExtMode.assert_called_once()


# ── C-2 / C-3: WatchSignals auth + error codes ────────────────────────


class TestWatchSignalsAuth:
    def test_requires_auth(self):
        client = MacpClient(target="localhost:0", allow_insecure=True)
        client.stub = MagicMock()
        with pytest.raises(MacpSdkError):
            list(client.watch_signals())

    def test_forwards_bearer_metadata(self):
        client, stub = _client_with_stub()
        stub.WatchSignals.return_value = iter([])
        list(client.watch_signals())
        _, kwargs = stub.WatchSignals.call_args
        assert ("authorization", "Bearer tok") in list(kwargs["metadata"])


class TestWatchStreamErrorCodes:
    def test_watch_signals_resource_exhausted_preserves_code(self):
        client, stub = _client_with_stub()

        def _raise():
            raise _FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "lagging")
            yield  # pragma: no cover

        stub.WatchSignals.return_value = _raise()
        with pytest.raises(MacpTransportError) as excinfo:
            list(client.watch_signals())
        assert excinfo.value.code == "RESOURCE_EXHAUSTED"

    def test_signal_watcher_surfaces_code(self):
        from macp_sdk.watchers import SignalWatcher

        client, stub = _client_with_stub()

        def _raise():
            raise _FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "lag")
            yield  # pragma: no cover

        stub.WatchSignals.return_value = _raise()
        watcher = SignalWatcher(client, auth=client.auth)
        with pytest.raises(MacpTransportError) as excinfo:
            list(watcher.signals())
        assert excinfo.value.code == "RESOURCE_EXHAUSTED"

    def test_stream_read_preserves_code(self):
        stub = MagicMock()

        def _raise_iter():
            raise _FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "lag")
            yield  # pragma: no cover

        stub.StreamSession.return_value = _raise_iter()
        stream = MacpStream(stub, metadata=[])
        with pytest.raises(MacpTransportError) as excinfo:
            list(stream.responses(timeout=2))
        assert excinfo.value.code == "RESOURCE_EXHAUSTED"
        stream.close()
