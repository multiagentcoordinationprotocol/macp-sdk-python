"""Unit tests for the asyncio session helpers (``Async*Session``).

Three layers, mirroring ``test_sessions.py`` / ``test_base_session.py``:

* **Surface parity** -- each ``Async<Mode>Session`` exposes the same public
  methods with the same parameters as ``<Mode>Session`` (minus the sync-only
  deprecated Task aliases), every runtime-facing method is a coroutine
  function, and the ``*_projection`` narrowing property is present.
* **Envelope parity** -- a full flow per mode is replayed through a sync and
  an async session; each step must send the same envelope (modulo
  ``message_id`` / ``timestamp_unix_ms``) and leave the same projection
  state, since both build through the shared ``_<Mode>Actions`` builders.
* **Lifecycle** -- ``metadata`` / ``cancel`` / ``suspend`` / ``resume`` /
  ``open_stream`` delegate to the async client with the session's auth, and a
  NACK leaves the projection untouched.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from macp.v1 import envelope_pb2

from macp_sdk import (
    AsyncBaseSession,
    AsyncDecisionSession,
    AsyncHandoffSession,
    AsyncProposalSession,
    AsyncQuorumSession,
    AsyncTaskSession,
    AuthConfig,
    BaseSession,
    DecisionSession,
    HandoffSession,
    MacpIdentityMismatchError,
    MacpSessionError,
    ProposalSession,
    QuorumSession,
    TaskSession,
)
from tests.conftest import VALID_SESSION_ID, make_ack

PAIRS: list[tuple[type[BaseSession], type[AsyncBaseSession]]] = [
    (DecisionSession, AsyncDecisionSession),
    (ProposalSession, AsyncProposalSession),
    (TaskSession, AsyncTaskSession),
    (HandoffSession, AsyncHandoffSession),
    (QuorumSession, AsyncQuorumSession),
]

SYNC_ONLY_DEPRECATED = {"request", "update", "complete", "fail"}


def _auth(agent: str) -> AuthConfig:
    return AuthConfig.for_dev_agent(agent)


def _ok(envelope: envelope_pb2.Envelope, **_: Any) -> envelope_pb2.Ack:
    return make_ack(ok=True, session_id=envelope.session_id, message_id=envelope.message_id)


def _sync_client() -> MagicMock:
    client = MagicMock()
    client.auth = _auth("coordinator")
    client.send.side_effect = _ok
    return client


def _async_client() -> MagicMock:
    client = MagicMock()
    client.auth = _auth("coordinator")
    client.send = AsyncMock(side_effect=_ok)
    return client


def _public(cls: type) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for name, member in vars(klass).items():
            if not name.startswith("_"):
                members[name] = member
    return members


def _comparable(envelope: envelope_pb2.Envelope) -> envelope_pb2.Envelope:
    clone = envelope_pb2.Envelope()
    clone.CopyFrom(envelope)
    clone.message_id = ""
    clone.timestamp_unix_ms = 0
    return clone


# ── Surface parity ────────────────────────────────────────────────────


class TestSurfaceParity:
    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
    def test_same_public_methods_and_parameters(
        self, sync_cls: type[BaseSession], async_cls: type[AsyncBaseSession]
    ) -> None:
        sync = {n: m for n, m in _public(sync_cls).items() if n not in SYNC_ONLY_DEPRECATED}
        async_ = _public(async_cls)
        assert set(sync) == set(async_)
        for name, member in sync.items():
            if callable(member):
                assert (
                    inspect.signature(member).parameters
                    == inspect.signature(async_[name]).parameters
                ), name

    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
    def test_runtime_methods_are_coroutines(
        self, sync_cls: type[BaseSession], async_cls: type[AsyncBaseSession]
    ) -> None:
        for name, member in _public(async_cls).items():
            if isinstance(member, property) or name in {"MODE", "open_stream"}:
                continue
            assert inspect.iscoroutinefunction(member), f"{async_cls.__name__}.{name}"

    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
    def test_mode_and_projection_match(
        self, sync_cls: type[BaseSession], async_cls: type[AsyncBaseSession]
    ) -> None:
        assert sync_cls.MODE == async_cls.MODE
        a = async_cls(_async_client(), session_id=VALID_SESSION_ID)
        s = sync_cls(_sync_client(), session_id=VALID_SESSION_ID)
        assert type(a.projection) is type(s.projection)
        (prop,) = [n for n in vars(async_cls) if n.endswith("_projection")]
        assert getattr(a, prop) is a.projection


# ── Envelope parity over full flows ───────────────────────────────────

# Each step: (method name, args, kwargs). Run identically on both helpers.
Step = tuple[str, tuple[Any, ...], dict[str, Any]]

FLOWS: dict[type[BaseSession], list[Step]] = {
    DecisionSession: [
        ("start", (), {"intent": "pick", "participants": ["coordinator", "alice"], "ttl_ms": 1}),
        ("propose", ("p1", "opt"), {"rationale": "r", "supporting_data": b"d"}),
        (
            "evaluate",
            ("p1", "approve"),
            {"confidence": 0.5, "reason": "ok", "sender": "alice", "auth": _auth("alice")},
        ),
        (
            "raise_objection",
            ("p1",),
            {"reason": "hmm", "severity": "low", "sender": "alice", "auth": _auth("alice")},
        ),
        ("vote", ("p1", "approve"), {"reason": "y", "sender": "alice", "auth": _auth("alice")}),
        (
            "commit",
            (),
            {"action": "a", "authority_scope": "s", "reason": "r", "commitment_id": "c1"},
        ),
    ],
    ProposalSession: [
        ("start", (), {"intent": "neg", "participants": ["coordinator", "bob"], "ttl_ms": 1}),
        ("propose", ("p1", "title"), {"summary": "s", "details": b"x", "tags": ["t"]}),
        (
            "counter_propose",
            ("p2", "p1", "counter"),
            {"summary": "s", "sender": "bob", "auth": _auth("bob")},
        ),
        ("withdraw", ("p2",), {"reason": "w", "sender": "bob", "auth": _auth("bob")}),
        ("reject", ("p1",), {"reason": "no", "sender": "bob", "auth": _auth("bob")}),
        ("accept", ("p1",), {"reason": "fine", "sender": "bob", "auth": _auth("bob")}),
    ],
    TaskSession: [
        ("start", (), {"intent": "t", "participants": ["coordinator", "worker"], "ttl_ms": 1}),
        (
            "request_task",
            ("t1", "build"),
            {"instructions": "i", "requested_assignee": "worker", "deadline_unix_ms": 5},
        ),
        ("reject_task", ("t1",), {"reason": "busy", "sender": "worker", "auth": _auth("worker")}),
        ("accept_task", ("t1",), {"sender": "worker", "auth": _auth("worker")}),
        (
            "update_task",
            ("t1",),
            {"status": "running", "progress": 0.5, "sender": "worker", "auth": _auth("worker")},
        ),
        (
            "fail_task",
            ("t1",),
            {"error_code": "E", "retryable": True, "sender": "worker", "auth": _auth("worker")},
        ),
        (
            "complete_task",
            ("t1",),
            {"output": b"o", "summary": "s", "sender": "worker", "auth": _auth("worker")},
        ),
    ],
    HandoffSession: [
        ("start", (), {"intent": "h", "participants": ["coordinator", "dave"], "ttl_ms": 1}),
        ("offer", ("h1", "dave"), {"scope": "all", "reason": "shift"}),
        ("add_context", ("h1",), {"content_type": "text/plain", "context": b"notes"}),
        ("decline", ("h1",), {"reason": "no", "sender": "dave", "auth": _auth("dave")}),
        ("accept_handoff", ("h1",), {"reason": "ok", "sender": "dave", "auth": _auth("dave")}),
    ],
    QuorumSession: [
        (
            "start",
            (),
            {"intent": "q", "participants": ["coordinator", "alice", "bob"], "ttl_ms": 1},
        ),
        ("request_approval", ("r1", "deploy"), {"summary": "s", "required_approvals": 2}),
        ("approve", ("r1",), {"reason": "y", "sender": "alice", "auth": _auth("alice")}),
        ("reject", ("r1",), {"reason": "n", "sender": "bob", "auth": _auth("bob")}),
        ("abstain", ("r1",), {"reason": "-", "sender": "coordinator"}),
        (
            "commit",
            (),
            {
                "action": "a",
                "authority_scope": "s",
                "reason": "r",
                "commitment_id": "c2",
                "outcome_positive": False,
            },
        ),
    ],
}


class TestEnvelopeParity:
    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
    def test_flow_sends_identical_envelopes(
        self, sync_cls: type[BaseSession], async_cls: type[AsyncBaseSession]
    ) -> None:
        sync_client = _sync_client()
        sync_session = sync_cls(sync_client, session_id=VALID_SESSION_ID)

        async def go() -> AsyncBaseSession:
            async_client = _async_client()
            session = async_cls(async_client, session_id=VALID_SESSION_ID)
            for step, (name, args, kwargs) in enumerate(FLOWS[sync_cls]):
                sync_ack = getattr(sync_session, name)(*args, **kwargs)
                async_ack = await getattr(session, name)(*args, **kwargs)
                assert sync_ack.ok and async_ack.ok
                sent_sync = sync_client.send.call_args.args[0]
                sent_async = async_client.send.call_args.args[0]
                assert _comparable(sent_sync) == _comparable(sent_async), (step, name)
                assert (
                    sync_client.send.call_args.kwargs["auth"]
                    == async_client.send.call_args.kwargs["auth"]
                ), (step, name)
            assert async_client.send.await_count == len(FLOWS[sync_cls])
            return session

        async_session = asyncio.run(go())
        assert [_comparable(e) for e in async_session.projection.transcript] == [
            _comparable(e) for e in sync_session.projection.transcript
        ]
        assert async_session.projection.phase == sync_session.projection.phase


# ── Validation is shared ──────────────────────────────────────────────


INVALID_CALLS: list[tuple[type[AsyncBaseSession], Callable[[Any], Any]]] = [
    (AsyncDecisionSession, lambda s: s.propose("", "opt")),
    (AsyncDecisionSession, lambda s: s.vote("p1", "maybe")),
    (AsyncDecisionSession, lambda s: s.evaluate("p1", "approve", confidence=2.0)),
    (AsyncDecisionSession, lambda s: s.raise_objection("p1", reason="x", severity="extreme")),
    (AsyncProposalSession, lambda s: s.propose("p1", "")),
    (AsyncTaskSession, lambda s: s.request_task("", "title")),
    (AsyncHandoffSession, lambda s: s.offer("", "dave")),
    (AsyncQuorumSession, lambda s: s.approve("")),
    (AsyncDecisionSession, lambda s: s.start(intent="i", participants=["a", "a"], ttl_ms=1)),
]


class TestValidation:
    @pytest.mark.parametrize(("cls", "call"), INVALID_CALLS)
    def test_invalid_input_raises_before_send(
        self, cls: type[AsyncBaseSession], call: Callable[[Any], Any]
    ) -> None:
        async def go() -> None:
            client = _async_client()
            session = cls(client, session_id=VALID_SESSION_ID)
            with pytest.raises(MacpSessionError):
                await call(session)
            client.send.assert_not_awaited()

        asyncio.run(go())

    def test_sender_mismatch_raises(self) -> None:
        async def go() -> None:
            client = _async_client()
            session = AsyncDecisionSession(client, session_id=VALID_SESSION_ID)
            with pytest.raises(MacpIdentityMismatchError):
                await session.propose("p1", "opt", sender="mallory")

        asyncio.run(go())

    def test_invalid_session_id_rejected(self) -> None:
        with pytest.raises(MacpSessionError):
            AsyncDecisionSession(_async_client(), session_id="not-a-uuid")

    def test_session_id_generated_when_omitted(self) -> None:
        session = AsyncQuorumSession(_async_client())
        assert session.session_id


# ── Lifecycle delegation ──────────────────────────────────────────────


class TestLifecycle:
    def test_nack_does_not_update_projection(self) -> None:
        async def go() -> None:
            client = _async_client()
            client.send = AsyncMock(return_value=make_ack(ok=False))
            session = AsyncDecisionSession(client, session_id=VALID_SESSION_ID)
            ack = await session.propose("p1", "opt")
            assert not ack.ok
            assert session.projection.transcript == []

        asyncio.run(go())

    def test_session_auth_used_by_default(self) -> None:
        async def go() -> None:
            client = _async_client()
            auth = _auth("coordinator")
            session = AsyncDecisionSession(client, session_id=VALID_SESSION_ID, auth=auth)
            await session.propose("p1", "opt")
            assert client.send.call_args.kwargs["auth"] is auth

        asyncio.run(go())

    @pytest.mark.parametrize(
        ("method", "client_method", "kwargs"),
        [
            ("cancel", "cancel_session", {"reason": "stop"}),
            ("suspend", "suspend_session", {"reason": "pause"}),
            ("resume", "resume_session", {"reason": "go"}),
        ],
    )
    def test_lifecycle_rpcs_delegate(
        self, method: str, client_method: str, kwargs: dict[str, str]
    ) -> None:
        async def go() -> None:
            client = _async_client()
            setattr(client, client_method, AsyncMock(return_value=make_ack(ok=True)))
            auth = _auth("coordinator")
            session = AsyncTaskSession(client, session_id=VALID_SESSION_ID, auth=auth)
            ack = await getattr(session, method)(**kwargs)
            assert ack.ok
            getattr(client, client_method).assert_awaited_once_with(
                VALID_SESSION_ID, reason=kwargs["reason"], auth=auth
            )

        asyncio.run(go())

    def test_metadata_delegates(self) -> None:
        async def go() -> None:
            client = _async_client()
            client.get_session = AsyncMock(return_value="meta")
            session = AsyncHandoffSession(client, session_id=VALID_SESSION_ID)
            override = _auth("other")
            assert await session.metadata(auth=override) == "meta"
            client.get_session.assert_awaited_once_with(VALID_SESSION_ID, auth=override)

        asyncio.run(go())

    def test_open_stream_delegates(self) -> None:
        client = _async_client()
        client.open_stream.return_value = "stream"
        auth = _auth("coordinator")
        session = AsyncProposalSession(client, session_id=VALID_SESSION_ID, auth=auth)
        assert session.open_stream() == "stream"
        client.open_stream.assert_called_once_with(auth=auth)
