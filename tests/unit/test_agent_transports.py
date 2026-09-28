"""Unit tests for agent transport adapters."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from macp.v1 import envelope_pb2

from macp_sdk.agent.transports import (
    GrpcTransportAdapter,
    HttpTransportAdapter,
    _envelope_to_message,
)
from macp_sdk.client import UNBOUNDED
from macp_sdk.constants import MODE_DECISION, MODE_MULTI_ROUND
from macp_sdk.envelope import new_message_id, now_unix_ms
from macp_sdk.errors import MacpSdkError, MacpTransportError
from macp_sdk.retry import RetryPolicy


def _make_envelope(
    message_type: str = "Proposal",
    session_id: str = "test-session",
    sender: str = "agent-a",
    payload: bytes | None = None,
) -> envelope_pb2.Envelope:
    if payload is None:
        import json

        payload = json.dumps({"proposal_id": "p1", "option": "opt-a"}).encode()
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=MODE_DECISION,
        message_type=message_type,
        message_id=new_message_id(),
        session_id=session_id,
        sender=sender,
        timestamp_unix_ms=now_unix_ms(),
        payload=payload,
    )


class TestEnvelopeToMessage:
    def test_basic_conversion(self):
        env = _make_envelope()
        msg = _envelope_to_message(env)
        assert msg.message_type == "Proposal"
        assert msg.sender == "agent-a"
        assert msg.raw is env
        assert "proposal_id" in msg.payload
        assert msg.proposal_id == "p1"

    def test_binary_payload(self):
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            mode=MODE_DECISION,
            message_type="Custom",
            message_id=new_message_id(),
            session_id="s1",
            sender="agent-b",
            timestamp_unix_ms=now_unix_ms(),
            payload=b"\x00\x01\x02",
        )
        msg = _envelope_to_message(env)
        assert msg.message_type == "Custom"
        # Binary payloads for unknown message types fall through to the
        # ProtoRegistry UTF-8 decode path which produces a text+base64 dict.
        assert msg.payload.get("encoding") in ("text",) or "_raw_bytes" in msg.payload

    def test_empty_payload(self):
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            mode=MODE_DECISION,
            message_type="Ping",
            message_id=new_message_id(),
            session_id="s1",
            sender="agent-c",
            timestamp_unix_ms=now_unix_ms(),
            payload=b"",
        )
        msg = _envelope_to_message(env)
        assert msg.payload == {}
        assert msg.proposal_id is None

    @pytest.mark.parametrize(
        "non_dict_json_payload",
        [b"null", b"0", b'"x"', b"[]", b"true"],
        ids=["null", "zero", "string", "empty-array", "true"],
    )
    def test_contribute_non_dict_json_payload_does_not_raise(self, non_dict_json_payload: bytes):
        # issue #69: none of these bytes are a canonical proto encoding of
        # ContributePayload, so ProtoRegistry.decode_known_payload returns
        # the legacy JSON wrapper for each (see
        # tests/unit/test_proto_registry.py::TestMultiRoundContribute::
        # test_decode_non_dict_json_still_returns_safe_wrapper). This pins
        # the caller-side contract that matters: _envelope_to_message must
        # come back with a dict `payload` -- never raise -- so that
        # `payload_dict.get("proposal_id")` a few lines below never sees a
        # non-dict. A decoder that instead raised DecodeError on these bytes
        # would surface an uncaught AttributeError here, because
        # `except Exception:`'s recovery path (transports.py) re-parses the
        # same bytes as JSON and succeeds with a non-dict result.
        env = envelope_pb2.Envelope(
            macp_version="1.0",
            mode=MODE_MULTI_ROUND,
            message_type="Contribute",
            message_id=new_message_id(),
            session_id="s1",
            sender="agent-d",
            timestamp_unix_ms=now_unix_ms(),
            payload=non_dict_json_payload,
        )
        msg = _envelope_to_message(env)
        assert isinstance(msg.payload, dict)
        assert msg.proposal_id is None


class TestGrpcTransportAdapter:
    def test_yields_messages_for_target_session(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()

        env1 = _make_envelope(session_id="target-session")
        env2 = _make_envelope(session_id="other-session")
        env3 = _make_envelope(session_id="target-session", message_type="Vote")
        mock_stream.responses.return_value = iter([env1, env2, env3])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")
        messages = list(adapter.start())

        assert len(messages) == 2
        assert messages[0].message_type == "Proposal"
        assert messages[1].message_type == "Vote"
        mock_stream.close.assert_called_once()

    def test_unbounded_timeout_passed_through_to_open_stream(self):
        """#97 follow-up to issue #93 item 6: a long-lived subscribe stream
        is the natural caller of client.UNBOUNDED -- the adapter's
        ``timeout`` param must be TimeoutValue, not a plain float | None,
        so this is expressible (and type-correct under mypy strict).
        """
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.responses.return_value = iter([])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session", timeout=UNBOUNDED)
        list(adapter.start())

        mock_client.open_stream.assert_called_once_with(auth=None, timeout=UNBOUNDED)

    def test_subscribe_sent_on_start(self):
        """RFC-MACP-0006-A1: the adapter must subscribe to the target
        session before iterating responses so non-initiator agents get
        SessionStart + Proposal replayed regardless of connection order.
        """
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.responses.return_value = iter([])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")
        list(adapter.start())

        mock_stream.send_subscribe.assert_called_once_with("target-session", after_sequence=0)

    def test_subscribe_precedes_response_iteration(self):
        """send_subscribe must be invoked before ``responses`` is consumed,
        otherwise the runtime won't replay history onto this stream."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        call_order: list[str] = []

        mock_stream.send_subscribe.side_effect = lambda *a, **kw: call_order.append("subscribe")

        def _responses_factory(*_a, **_kw):
            call_order.append("responses")
            return iter([])

        mock_stream.responses.side_effect = _responses_factory
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "s-order")
        list(adapter.start())

        assert call_order == ["subscribe", "responses"]

    def test_replayed_envelopes_yielded_after_subscribe(self):
        """End-to-end adapter contract: after ``send_subscribe`` the stream
        emits replayed envelopes (SessionStart + Proposal) and the adapter
        passes them through — this is the reason non-initiator agents see
        history they would otherwise miss."""
        mock_client = MagicMock()
        mock_stream = MagicMock()

        replayed = [
            _make_envelope(session_id="late", message_type="SessionStart"),
            _make_envelope(session_id="late", message_type="Proposal"),
        ]
        mock_stream.responses.return_value = iter(replayed)
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "late")
        messages = list(adapter.start())

        mock_stream.send_subscribe.assert_called_once_with("late", after_sequence=0)
        assert [m.message_type for m in messages] == ["SessionStart", "Proposal"]

    def test_stop_closes_stream(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.responses.return_value = iter([])
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "s1")
        list(adapter.start())
        adapter.stop()

        assert adapter._stopped is True

    def test_retries_transient_not_found_from_subscribe(self):
        """#75: a NOT_FOUND from the initial subscribe (the target session's
        SessionStart hasn't reached the runtime yet) is a normal startup
        race, retried with backoff instead of raised immediately."""
        mock_client = MagicMock()
        first_stream = MagicMock()
        second_stream = MagicMock()

        def _not_found_once():
            raise MacpTransportError("Session 'target-session' not found", code="NOT_FOUND")
            yield  # pragma: no cover - makes this a generator function

        first_stream.responses.side_effect = _not_found_once
        second_stream.responses.return_value = iter([_make_envelope(session_id="target-session")])
        mock_client.open_stream.side_effect = [first_stream, second_stream]

        adapter = GrpcTransportAdapter(
            mock_client,
            "target-session",
            subscribe_retry=RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0),
        )
        messages = list(adapter.start())

        assert len(messages) == 1
        first_stream.send_subscribe.assert_called_once_with("target-session", after_sequence=0)
        second_stream.send_subscribe.assert_called_once_with("target-session", after_sequence=0)
        first_stream.close.assert_called_once()
        second_stream.close.assert_called_once()

    def test_non_not_found_transport_error_raised_immediately(self):
        """Only NOT_FOUND is treated as a transient startup race; any other
        transport failure must still fail fast, with no retry."""
        mock_client = MagicMock()
        mock_stream = MagicMock()

        def _unavailable():
            raise MacpTransportError("boom", code="UNAVAILABLE")
            yield  # pragma: no cover

        mock_stream.responses.side_effect = _unavailable
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")

        with pytest.raises(MacpTransportError, match="boom"):
            list(adapter.start())

        mock_client.open_stream.assert_called_once()

    def test_raises_after_subscribe_retries_exhausted(self):
        """A session that is permanently missing must still fail once the
        bounded retry window is exhausted, not retry forever."""
        mock_client = MagicMock()
        streams: list[MagicMock] = []

        def _not_found():
            raise MacpTransportError("still missing", code="NOT_FOUND")
            yield  # pragma: no cover

        def _new_stream(*_args, **_kwargs):
            stream = MagicMock()
            stream.responses.side_effect = _not_found
            streams.append(stream)
            return stream

        mock_client.open_stream.side_effect = _new_stream

        adapter = GrpcTransportAdapter(
            mock_client,
            "target-session",
            subscribe_retry=RetryPolicy(max_retries=2, backoff_base=0.0, backoff_max=0.0),
        )

        with pytest.raises(MacpTransportError, match="still missing"):
            list(adapter.start())

        assert mock_client.open_stream.call_count == 3  # initial attempt + 2 retries
        for stream in streams:
            stream.close.assert_called_once()

    def test_not_found_after_envelope_seen_is_not_retried(self):
        """NOT_FOUND is only transient at startup — once an envelope has
        been delivered the session demonstrably exists, so a later
        NOT_FOUND (e.g. it expired mid-stream) is raised as-is."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        env = _make_envelope(session_id="target-session")

        def _envelope_then_not_found():
            yield env
            raise MacpTransportError("session expired mid-stream", code="NOT_FOUND")

        mock_stream.responses.side_effect = _envelope_then_not_found
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")

        with pytest.raises(MacpTransportError, match="session expired mid-stream"):
            list(adapter.start())

        mock_client.open_stream.assert_called_once()


class TestGrpcTransportAdapterResumeCursor:
    """Phase 3 item 7: a client-side ``delivered`` counter (incremented
    once per distinct envelope actually yielded) is the resume cursor
    passed as ``after_sequence`` on reconnect, and ``IncomingMessage.seq``
    increments on its own separate counter on the gRPC path.
    """

    def test_delivered_and_seq_increment_per_yielded_envelope(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()
        envs = [
            _make_envelope(session_id="target-session", message_type="SessionStart"),
            _make_envelope(session_id="other-session", message_type="Ignored"),
            _make_envelope(session_id="target-session", message_type="Proposal"),
        ]
        mock_stream.responses.return_value = iter(envs)
        mock_client.open_stream.return_value = mock_stream

        adapter = GrpcTransportAdapter(mock_client, "target-session")
        messages = list(adapter.start())

        # Only the two target-session envelopes are yielded (the other
        # session's envelope is skipped, not counted).
        assert [m.seq for m in messages] == [1, 2]
        assert adapter.last_sequence == 2

    def test_reconnect_resumes_after_sequence_from_delivered(self):
        """A second start() call on the SAME adapter instance (e.g. a
        supervisor-driven restart that reuses it) must resume from
        after_sequence=N, not 0, once N envelopes have already been
        delivered."""
        mock_client = MagicMock()
        first_stream = MagicMock()
        first_stream.responses.return_value = iter(
            [_make_envelope(session_id="s1"), _make_envelope(session_id="s1")]
        )
        second_stream = MagicMock()
        second_stream.responses.return_value = iter([])
        mock_client.open_stream.side_effect = [first_stream, second_stream]

        adapter = GrpcTransportAdapter(mock_client, "s1")
        list(adapter.start())
        assert adapter.last_sequence == 2

        list(adapter.start())
        first_stream.send_subscribe.assert_called_once_with("s1", after_sequence=0)
        second_stream.send_subscribe.assert_called_once_with("s1", after_sequence=2)

    def test_fresh_adapter_instance_starts_at_zero(self):
        """last_sequence is per-adapter-instance, not per-session — a fresh
        instance never carries over a prior instance's delivered count."""
        adapter = GrpcTransportAdapter(MagicMock(), "s1")
        assert adapter.last_sequence == 0


class TestGrpcTransportAdapterCancel:
    """Phase 2 item 2's production implementation: GrpcTransportAdapter.cancel()
    and start()'s stopped-swallow branch, exercised directly (Participant-level
    tests in test_agent_participant.py cover the wiring through stop(), but not
    this class's own cancel()/start() behavior in isolation)."""

    def test_cancel_calls_stream_cancel_and_sets_stopped(self):
        adapter = GrpcTransportAdapter(MagicMock(), "s1")
        mock_stream = MagicMock()
        adapter._stream = mock_stream

        adapter.cancel()

        mock_stream.cancel.assert_called_once()
        assert adapter._stopped is True

    def test_stop_calls_stream_close_when_stream_present(self):
        """#80's local-bind fix in stop() (read self._stream once into a
        local before checking it, closing the same double-read
        AttributeError window cancel() had) -- proven directly rather than
        only by the pre-existing test_stop_closes_stream, which runs
        start() to completion first so _stream is already None by the time
        stop() is called and never exercises this branch."""
        adapter = GrpcTransportAdapter(MagicMock(), "s1")
        mock_stream = MagicMock()
        adapter._stream = mock_stream

        adapter.stop()

        mock_stream.close.assert_called_once()
        assert adapter._stopped is True

    def test_cancel_before_start_is_safe_noop(self):
        adapter = GrpcTransportAdapter(MagicMock(), "s1")
        adapter.cancel()  # no stream yet -- must not raise
        assert adapter._stopped is True

    def test_cancel_is_idempotent(self):
        adapter = GrpcTransportAdapter(MagicMock(), "s1")
        mock_stream = MagicMock()
        adapter._stream = mock_stream

        adapter.cancel()
        adapter.cancel()

        assert adapter._stopped is True
        # Forwards each call; MacpStream.cancel() is itself idempotent.
        assert mock_stream.cancel.call_count == 2

    def test_start_swallows_transport_error_when_stopped(self):
        """The new stopped-swallow branch: a MacpTransportError surfacing
        mid-stream after cancel()/stop() (e.g. CANCELLED from the aborted
        call) must not propagate -- it's an expected clean shutdown, not a
        failure."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")

        def _cancelled_mid_stream():
            adapter._stopped = True  # simulate cancel() firing from another thread
            raise MacpTransportError("aborted", code="CANCELLED")
            yield  # pragma: no cover - makes this a generator function

        mock_stream.responses.side_effect = _cancelled_mid_stream
        mock_client.open_stream.return_value = mock_stream

        messages = list(adapter.start())  # must not raise

        assert messages == []

    def test_start_swallows_stopped_transport_error_of_any_code(self):
        """The stopped-swallow branch checks self._stopped, not the error
        code -- any transport error surfacing after an intentional stop is
        treated as a clean shutdown, not just CANCELLED specifically.
        self._stopped is set from inside the responses() side effect
        (not before calling start()), since start()'s own top-of-loop
        `if self._stopped: return` would otherwise short-circuit before
        ever reaching the except block this test targets."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")

        def _unavailable_after_stop():
            adapter._stopped = True
            raise MacpTransportError("boom", code="UNAVAILABLE")
            yield  # pragma: no cover

        mock_stream.responses.side_effect = _unavailable_after_stop
        mock_client.open_stream.return_value = mock_stream

        messages = list(adapter.start())  # must not raise

        assert messages == []

    def test_cancel_during_open_stream_window_is_not_lost(self):
        """#80: a cancel() landing after self._stream is assigned but
        before the recheck must be caught by that recheck, not lost.
        Deterministic single-thread reproduction: open_stream()'s own
        side_effect calls adapter.cancel() before returning the mock
        stream, so cancel() runs while self._stream is still None (its
        own no-op branch) and self._stopped is set to True before
        start()'s assignment+recheck ever run."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")

        def _cancel_before_returning_stream(**kwargs):
            adapter.cancel()
            return mock_stream

        mock_client.open_stream.side_effect = _cancel_before_returning_stream

        messages = list(adapter.start())

        assert messages == []
        mock_stream.send_subscribe.assert_not_called()
        # The discriminating assertion: MagicMock.__iter__ would let an
        # un-configured responses() silently iterate as empty, so without
        # this the test would pass on the pre-fix code too.
        mock_stream.responses.assert_not_called()
        mock_stream.cancel.assert_called_once()
        assert adapter._stream is None

    def test_cancel_during_open_stream_window_unblocks_background_thread(self):
        """#80's true two-thread regression. A single `opened` event is not
        enough to reproduce the race (the worker thread reaches the recheck
        and enters responses() before a main thread woken by opened.wait()
        can call cancel() -- verified empirically during plan review). This
        uses two events: open_stream()'s side_effect sets `opened` and then
        blocks on `may_return` before returning the mock stream, so
        open_stream() itself does not return until the main thread has both
        observed `opened` and called cancel(). At that instant self._stream
        is still its pre-call value, so cancel()'s own guard is a no-op
        beyond setting self._stopped; when open_stream() then unblocks and
        self._stream is assigned, the new recheck immediately observes
        self._stopped is True and tears the stream down without ever
        calling responses() -- pre-fix, with no recheck, the code proceeds
        straight into responses(), which blocks on a third, never-set event
        (simulating an idle live stream) and hangs."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")

        opened = threading.Event()
        may_return = threading.Event()
        stuck = threading.Event()

        def _open_stream(**kwargs):
            opened.set()
            may_return.wait(timeout=5)
            return mock_stream

        def _blocking_responses():
            stuck.wait(timeout=5)
            return
            yield  # pragma: no cover - makes this a generator function

        mock_client.open_stream.side_effect = _open_stream
        mock_stream.responses.side_effect = _blocking_responses

        thread = threading.Thread(target=lambda: list(adapter.start()), daemon=True)
        thread.start()
        assert opened.wait(timeout=2)
        adapter.cancel()
        may_return.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        mock_stream.responses.assert_not_called()
        mock_stream.cancel.assert_called_once()

    def test_cancel_between_recheck_and_send_subscribe_is_clean_shutdown(self):
        """#89: a cancel() landing after the #80 recheck passes but before
        send_subscribe() runs sets self._stopped (and, on the real
        MacpStream, self._closed) first -- so send_subscribe() itself then
        raises MacpSdkError("stream is already closed"), not
        MacpTransportError. Reproduced deterministically: send_subscribe's
        own side_effect calls adapter.cancel() before raising the same
        exception type/message the real MacpStream.send_subscribe would."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")
        mock_client.open_stream.return_value = mock_stream

        def _cancel_then_raise_already_closed(*args, **kwargs):
            adapter.cancel()
            raise MacpSdkError("stream is already closed")

        mock_stream.send_subscribe.side_effect = _cancel_then_raise_already_closed

        messages = list(adapter.start())  # must not raise

        assert messages == []
        mock_stream.responses.assert_not_called()
        assert adapter._stopped is True

    def test_macp_sdk_error_while_running_still_propagates(self):
        """The #89 fix is gated on self._stopped, not a blanket
        `except MacpSdkError: return` -- an unrelated MacpSdkError raised
        while the adapter is not stopped (e.g. a hypothetical caller bug)
        must still propagate, proving the gate is genuinely conditional."""
        mock_client = MagicMock()
        mock_stream = MagicMock()
        adapter = GrpcTransportAdapter(mock_client, "target-session")
        mock_client.open_stream.return_value = mock_stream
        mock_stream.send_subscribe.side_effect = MacpSdkError("unrelated failure")

        with pytest.raises(MacpSdkError, match="unrelated failure"):
            list(adapter.start())

        assert adapter._stopped is False


class TestHttpTransportAdapter:
    def test_stop_sets_flag(self):
        adapter = HttpTransportAdapter(
            base_url="http://localhost:8080",
            session_id="s1",
            participant_id="agent-a",
            poll_interval_ms=100,
        )
        assert adapter._stopped is False
        adapter.stop()
        assert adapter._stopped is True

    def test_config_values(self):
        adapter = HttpTransportAdapter(
            base_url="http://localhost:8080/",
            session_id="s1",
            participant_id="agent-a",
            poll_interval_ms=2000,
            auth_token="tok-123",
        )
        assert adapter._base_url == "http://localhost:8080"
        assert adapter._session_id == "s1"
        assert adapter._participant_id == "agent-a"
        assert adapter._poll_interval == 2.0
        assert adapter._auth_token == "tok-123"


class TestHttpTransportAdapterPayloadShapes:
    """Phase 3 item 8: accept both a bare JSON array and typescript-sdk's
    {"events": [...]} wrapper, and JSON-decode a string/bytes payload
    field the way tryParsePayload does."""

    @staticmethod
    def _adapter() -> HttpTransportAdapter:
        return HttpTransportAdapter(
            base_url="http://localhost:8080",
            session_id="s1",
            participant_id="agent-a",
            poll_interval_ms=0,
        )

    @staticmethod
    def _fake_response(body: bytes) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_accepts_events_wrapper_shape(self):
        import json
        from unittest.mock import patch

        body = json.dumps(
            {"events": [{"message_type": "Vote", "sender": "alice", "payload": {"x": 1}, "seq": 1}]}
        ).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.message_type == "Vote"
        assert msg.sender == "alice"
        assert msg.payload == {"x": 1}
        assert msg.seq == 1

    def test_bare_array_shape_still_works(self):
        import json
        from unittest.mock import patch

        body = json.dumps(
            [{"message_type": "Proposal", "sender": "bob", "payload": {"y": 2}, "seq": 5}]
        ).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.message_type == "Proposal"
        assert msg.payload == {"y": 2}

    def test_string_payload_is_json_decoded(self):
        import json
        from unittest.mock import patch

        body = json.dumps(
            [{"message_type": "Vote", "sender": "a", "payload": json.dumps({"foo": "bar"})}]
        ).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.payload == {"foo": "bar"}

    def test_non_dict_string_payload_falls_back_to_empty_dict(self):
        import json
        from unittest.mock import patch

        body = json.dumps(
            [{"message_type": "Vote", "sender": "a", "payload": json.dumps(["not", "a", "dict"])}]
        ).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.payload == {}

    def test_malformed_json_string_payload_falls_back_to_empty_dict(self):
        import json
        from unittest.mock import patch

        body = json.dumps(
            [{"message_type": "Vote", "sender": "a", "payload": "{not-valid-json"}]
        ).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.payload == {}

    def test_non_str_non_dict_payload_falls_back_to_empty_dict(self):
        import json
        from unittest.mock import patch

        body = json.dumps([{"message_type": "Vote", "sender": "a", "payload": 42}]).encode()
        with patch("urllib.request.urlopen", return_value=self._fake_response(body)):
            msg = next(self._adapter().start())
        assert msg.payload == {}
