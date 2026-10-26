"""OpenTelemetry: trace-context propagation and the ``send`` span (Wave 5 S2).

The ``test_otel_*`` names match macp-sdk-typescript's. Assertions on span
names and attributes are read from the vendored ``tests/otel/macp-semconv.json``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.metadata
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from macp.v1 import core_pb2, envelope_pb2
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
    TraceState,
)

from macp_sdk import AsyncMacpClient, AuthConfig, _otel
from macp_sdk import client as client_module
from macp_sdk.errors import MacpAckError, MacpTransportError
from macp_sdk.retry import RetryPolicy, retry_send
from tests.conftest import FakeRpcError, client_with_stub, make_ack

REPO = Path(__file__).parents[2]
AUTH = AuthConfig.for_bearer("tok", sender_hint="alice")

# One global provider for the whole session: OpenTelemetry ignores a second
# set_tracer_provider (with only a logging warning).
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)
# A provider set elsewhere first would silently win, and the span tests would
# read an empty exporter.
assert trace.get_tracer_provider() is _PROVIDER


@pytest.fixture(autouse=True)
def _fresh_otel() -> Iterator[InMemorySpanExporter]:
    _otel._reset()
    _EXPORTER.clear()
    yield _EXPORTER
    _otel._reset()


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


_BLOCK_OTEL = """
    import importlib.abc, sys

    class _BlockOtel(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError(f"blocked: {name}")
            return None

    sys.meta_path.insert(0, _BlockOtel())
"""


def _loaded_otel_modules() -> str:
    return "sorted(k for k, v in sys.modules.items() if k.startswith('opentelemetry') and v)"


# ------------------------------------------------------------ absent / lazy


def test_otel_absent_no_import() -> None:
    result = _run_isolated(
        _BLOCK_OTEL
        + f"""
    from macp_sdk import AuthConfig, MacpClient
    auth = AuthConfig.for_bearer("tok", sender_hint="alice")
    client = MacpClient(target="localhost:0", allow_insecure=True, auth=auth)
    assert list(client._metadata()) == list(auth.metadata()), client._metadata()
    loaded = {_loaded_otel_modules()}
    assert loaded == [], loaded
    print("ok")
    """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_import_is_lazy_when_installed() -> None:
    result = _run_isolated(
        f"""
    import sys
    import macp_sdk
    from macp_sdk import AuthConfig, MacpClient
    MacpClient(target="localhost:0", allow_insecure=True, auth=AuthConfig.for_bearer("t"))
    print({_loaded_otel_modules()})
    """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_absent_in_process_leaves_metadata_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    client, _ = client_with_stub(auth=AUTH)
    tracer = _PROVIDER.get_tracer("t")

    with tracer.start_as_current_span("outer"):
        assert list(client._metadata()) == list(AUTH.metadata())
    assert _otel._load() is None


def test_broken_install_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> str:
        raise RuntimeError("broken metadata")

    monkeypatch.setattr("importlib.metadata.version", boom)
    tracer = _PROVIDER.get_tracer("t")

    with tracer.start_as_current_span("outer"):
        assert _otel.inject([("a", "b")]) == [("a", "b")]
    assert _otel._load() is None


def test_unknown_package_version_still_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    assert _otel._load() is not None


def test_throwing_propagator_leaves_metadata_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = _otel._load()
    assert loaded is not None

    class Boom:
        def inject(self, carrier: Any) -> None:
            raise RuntimeError("exporter down")

    monkeypatch.setattr(
        _otel,
        "_load",
        lambda: _otel._Otel(
            trace=loaded.trace,
            context=loaded.context,
            propagator=Boom(),
            tracer=loaded.tracer,
        ),
    )
    with _PROVIDER.get_tracer("t").start_as_current_span("outer"):
        assert _otel.inject([("a", "b")]) == [("a", "b")]


# ------------------------------------------------------------ propagation


def _header(metadata: Any, key: str) -> list[str]:
    return [value for name, value in metadata if name == key]


def test_otel_traceparent_injected_in_metadata() -> None:
    client, _ = client_with_stub(auth=AUTH)
    tracer = _PROVIDER.get_tracer("t")

    with tracer.start_as_current_span("outer") as span:
        metadata = list(client._metadata())
    ctx = span.get_span_context()

    (traceparent,) = _header(metadata, "traceparent")
    version, trace_id, span_id, _flags = traceparent.split("-")
    assert (version, trace_id, span_id) == ("00", f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}")
    # Auth metadata comes first, untouched.
    assert metadata[: len(AUTH.metadata())] == list(AUTH.metadata())


def test_tracestate_passes_through() -> None:
    parent = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState([("vendor", "v1")]),
    )
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(parent)))
    try:
        metadata = _otel.inject([])
    finally:
        context.detach(token)

    assert _header(metadata, "traceparent") == [
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    ]
    assert _header(metadata, "tracestate") == ["vendor=v1"]


def test_baggage_is_never_injected() -> None:
    tracer = _PROVIDER.get_tracer("t")
    token = context.attach(baggage.set_baggage("user.secret", "do-not-send"))
    try:
        with tracer.start_as_current_span("outer"):
            metadata = _otel.inject([])
    finally:
        context.detach(token)

    assert {name for name, _ in metadata} == {"traceparent"}


def test_no_active_span_adds_nothing() -> None:
    client, _ = client_with_stub(auth=AUTH)
    assert list(client._metadata()) == list(AUTH.metadata())


def test_input_metadata_is_not_mutated() -> None:
    original = (("authorization", "Bearer tok"),)
    tracer = _PROVIDER.get_tracer("t")
    with tracer.start_as_current_span("outer"):
        result = _otel.inject(original)
    assert original == (("authorization", "Bearer tok"),)
    assert result[0] == original[0] and len(result) == 2


def test_unary_rpc_and_stream_open_carry_trace_context(monkeypatch: pytest.MonkeyPatch) -> None:
    client, stub = client_with_stub(auth=AUTH)
    opened: dict[str, Any] = {}
    monkeypatch.setattr(
        client_module, "MacpStream", lambda stub, *, metadata, timeout: opened.update(m=metadata)
    )
    tracer = _PROVIDER.get_tracer("t")

    with tracer.start_as_current_span("outer"):
        client.get_session("s1")
        client.open_stream()

    assert len(_header(stub.GetSession.call_args.kwargs["metadata"], "traceparent")) == 1
    assert len(_header(opened["m"], "traceparent")) == 1


def test_async_client_carries_trace_context() -> None:
    async def go() -> Any:
        client = AsyncMacpClient(target="localhost:0", allow_insecure=True, auth=AUTH)
        with _PROVIDER.get_tracer("t").start_as_current_span("outer") as span:
            await asyncio.sleep(0)
            return list(client._metadata()), span.get_span_context()

    metadata, ctx = asyncio.run(go())
    (traceparent,) = _header(metadata, "traceparent")
    assert f"{ctx.span_id:016x}" in traceparent


# ------------------------------------------------------------ send span

_SEMCONV = json.loads((REPO / "tests/otel/macp-semconv.json").read_text())
_SEND = next(g for g in _SEMCONV["groups"] if g["id"] == "span.macp.send")
_SEND_REFS = {a["ref"] for a in _SEND["attributes"]}
_SEND_REQUIRED = {a["ref"] for a in _SEND["attributes"] if a["requirement_level"] == "required"}

SENTINEL = "SENTINEL-do-not-record"


def _envelope(**overrides: Any) -> envelope_pb2.Envelope:
    fields: dict[str, Any] = {
        "macp_version": "1.0",
        "mode": "macp.mode.decision.v1",
        "message_type": "Proposal",
        "message_id": "m-1",
        "session_id": "s-1",
        "sender": "alice",
        "timestamp_unix_ms": 1,
        "payload": SENTINEL.encode(),
    }
    fields.update(overrides)
    return envelope_pb2.Envelope(**fields)


def _nack(code: str) -> envelope_pb2.Ack:
    ack = envelope_pb2.Ack(ok=False, message_id="m-1", session_id="s-1")
    ack.error.CopyFrom(envelope_pb2.MACPError(code=code, message=f"{SENTINEL} reason"))
    return ack


def _send_sync(response: Any, envelope: envelope_pb2.Envelope, **kwargs: Any) -> Any:
    client, stub = client_with_stub(auth=AUTH)
    if isinstance(response, BaseException):
        stub.Send.side_effect = response
    else:
        stub.Send.return_value = core_pb2.SendResponse(ack=response)
    return client.send(envelope, **kwargs), stub


def _send_async(response: Any, envelope: envelope_pb2.Envelope, **kwargs: Any) -> Any:
    async def go() -> Any:
        client = AsyncMacpClient(target="localhost:0", allow_insecure=True, auth=AUTH)
        stub = MagicMock()
        if isinstance(response, BaseException):
            stub.Send = AsyncMock(side_effect=response)
        else:
            stub.Send = AsyncMock(return_value=core_pb2.SendResponse(ack=response))
        client.stub = stub
        return await client.send(envelope, **kwargs), stub

    return asyncio.run(go())


@pytest.fixture(params=["sync", "async"])
def send(request: pytest.FixtureRequest) -> Callable[..., Any]:
    return _send_sync if request.param == "sync" else _send_async  # type: ignore[return-value]


def _only_span() -> ReadableSpan:
    (span,) = _EXPORTER.get_finished_spans()
    return span


def _assert_semconv_shape(span: ReadableSpan) -> None:
    keys = set(span.attributes or {})
    assert keys <= _SEND_REFS, keys - _SEND_REFS
    assert keys >= _SEND_REQUIRED
    assert span.kind is SpanKind.CLIENT
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "macp-sdk-python"
    assert span.instrumentation_scope.version == importlib.metadata.version("macp-sdk-python")


def test_otel_send_span_name_and_attributes(send: Callable[..., Any]) -> None:
    send(make_ack(True, session_id="s-1", message_id="m-1"), _envelope())

    span = _only_span()
    _assert_semconv_shape(span)
    assert span.name == _SEND["span_name"].replace("{macp.mode.id}", "macp.mode.decision.v1")
    assert dict(span.attributes or {}) == {
        "macp.operation.name": "send",
        "macp.message.id": "m-1",
        "macp.message.type": "Proposal",
        "macp.session.id": "s-1",
        "macp.mode.id": "macp.mode.decision.v1",
        "macp.sender.id": "alice",
        "macp.version": "1.0",
        "macp.envelope.outcome": "accepted",
    }
    assert span.status.status_code is StatusCode.UNSET
    assert span.events == ()


def test_otel_send_span_empty_mode_bare_name(send: Callable[..., Any]) -> None:
    send(make_ack(True), _envelope(mode="", session_id="", sender="", macp_version=""))

    span = _only_span()
    _assert_semconv_shape(span)
    assert span.name == "send"
    attributes = dict(span.attributes or {})
    for absent in ("macp.mode.id", "macp.session.id", "macp.sender.id", "macp.version"):
        assert absent not in attributes


@pytest.mark.parametrize("raise_on_nack", [True, False])
def test_otel_send_span_nack_rejected_status_unset(
    send: Callable[..., Any], raise_on_nack: bool
) -> None:
    if raise_on_nack:
        with pytest.raises(MacpAckError):
            send(_nack("SESSION_NOT_OPEN"), _envelope())
    else:
        ack, _ = send(_nack("SESSION_NOT_OPEN"), _envelope(), raise_on_nack=False)
        assert not ack.ok

    span = _only_span()
    assert span.attributes is not None
    assert span.attributes["macp.envelope.outcome"] == "rejected"
    assert span.attributes["error.type"] == "SESSION_NOT_OPEN"
    assert span.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize("raise_on_nack", [True, False])
def test_nack_without_code_is_other(send: Callable[..., Any], raise_on_nack: bool) -> None:
    ack = envelope_pb2.Ack(ok=False)
    if raise_on_nack:
        with pytest.raises(MacpAckError):
            send(ack, _envelope())
    else:
        send(ack, _envelope(), raise_on_nack=False)

    assert (_only_span().attributes or {})["error.type"] == "_OTHER"


@pytest.mark.parametrize(
    ("rpc_error", "code"),
    [
        (
            FakeRpcError(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"{SENTINEL} denied",
                trailing_metadata=[("macp-error-code", "POLICY_DENIED")],
            ),
            "POLICY_DENIED",
        ),
        # Pre-0.9 runtime: no macp-error-code, so the SDK infers the code.
        (FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, SENTINEL), "POLICY_DENIED"),
        (FakeRpcError(grpc.StatusCode.ALREADY_EXISTS, SENTINEL), "SESSION_ALREADY_EXISTS"),
    ],
)
def test_otel_send_span_coded_status_rejected(
    send: Callable[..., Any], rpc_error: FakeRpcError, code: str
) -> None:
    with pytest.raises(MacpAckError):
        send(rpc_error, _envelope())

    span = _only_span()
    assert span.attributes is not None
    assert span.attributes["macp.envelope.outcome"] == "rejected"
    assert span.attributes["error.type"] == code
    assert span.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.INVALID_ARGUMENT,
    ],
)
def test_otel_send_span_transport_failure_error_status(
    send: Callable[..., Any], status: grpc.StatusCode
) -> None:
    with pytest.raises(MacpTransportError):
        send(FakeRpcError(status, SENTINEL), _envelope())

    span = _only_span()
    assert span.attributes is not None
    assert "macp.envelope.outcome" not in span.attributes
    assert span.attributes["error.type"] == status.name
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == status.name
    assert span.events == ()


def test_otel_send_span_duplicate_outcome(send: Callable[..., Any]) -> None:
    # A duplicate is classified first, even when the runtime leaves ok=false.
    send(envelope_pb2.Ack(ok=False, duplicate=True), _envelope())

    span = _only_span()
    assert (span.attributes or {})["macp.envelope.outcome"] == "duplicate"
    assert "error.type" not in (span.attributes or {})
    assert span.status.status_code is StatusCode.UNSET


def test_send_carries_its_own_span_as_traceparent(send: Callable[..., Any]) -> None:
    with _PROVIDER.get_tracer("t").start_as_current_span("caller"):
        _, stub = send(make_ack(True), _envelope())

    send_span, caller = _EXPORTER.get_finished_spans()
    (traceparent,) = _header(stub.Send.call_args.kwargs["metadata"], "traceparent")
    assert traceparent.split("-")[1:3] == [
        f"{send_span.context.trace_id:032x}",
        f"{send_span.context.span_id:016x}",
    ]
    assert send_span.parent is not None
    assert send_span.parent.span_id == caller.context.span_id


def test_otel_no_payload_or_reason_recorded(send: Callable[..., Any]) -> None:
    with pytest.raises(MacpAckError):
        send(_nack("POLICY_DENIED"), _envelope())
    with pytest.raises(MacpTransportError):
        send(FakeRpcError(grpc.StatusCode.UNAVAILABLE, SENTINEL), _envelope())
    denied = {
        "reasons": [f"{SENTINEL} reason"],
        "denials": [{"code": "veto", "message": f"{SENTINEL} denial"}],
    }
    with pytest.raises(MacpAckError) as raised:
        send(
            FakeRpcError(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"{SENTINEL} details",
                trailing_metadata=[
                    ("macp-error-code", "POLICY_DENIED"),
                    ("macp-error-details-bin", json.dumps(denied).encode()),
                ],
            ),
            _envelope(),
        )
    assert raised.value.denials  # the details were parsed, so they were available to leak
    with pytest.raises(RuntimeError):
        send(RuntimeError(SENTINEL), _envelope())

    spans = _EXPORTER.get_finished_spans()
    assert len(spans) == 4
    for span in spans:
        recorded = [span.name, span.status.description or ""]
        recorded += [str(v) for v in (span.attributes or {}).values()]
        recorded += [repr(event) for event in span.events]
        assert not any(SENTINEL in text for text in recorded), recorded


def test_non_sdk_exception_is_other_and_reraised(send: Callable[..., Any]) -> None:
    boom = RuntimeError(SENTINEL)
    with pytest.raises(RuntimeError) as raised:
        send(boom, _envelope())

    assert raised.value is boom
    span = _only_span()
    assert (span.attributes or {})["error.type"] == "_OTHER"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "_OTHER"


def _assert_other_error(span: ReadableSpan) -> None:
    assert (span.attributes or {})["error.type"] == "_OTHER"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "_OTHER"


def test_async_cancellation_from_stub_is_other_and_propagates() -> None:
    cancelled = asyncio.CancelledError()

    async def go() -> None:
        client = AsyncMacpClient(target="localhost:0", allow_insecure=True, auth=AUTH)
        client.stub = MagicMock()
        client.stub.Send = AsyncMock(side_effect=cancelled)
        await client.send(_envelope())

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(go())

    assert raised.value is cancelled
    _assert_other_error(_only_span())


def test_async_task_cancel_mid_send_is_other_and_propagates() -> None:
    async def go() -> None:
        started = asyncio.Event()

        async def hang(*args: Any, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

        client = AsyncMacpClient(target="localhost:0", allow_insecure=True, auth=AUTH)
        client.stub = MagicMock()
        client.stub.Send = hang
        task = asyncio.create_task(client.send(_envelope()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The send span was detached in the task's own context.
        assert not trace.get_current_span().get_span_context().is_valid

    asyncio.run(go())
    _assert_other_error(_only_span())


def test_retry_send_gets_one_span_per_attempt() -> None:
    client, stub = client_with_stub(auth=AUTH)
    stub.Send.side_effect = [
        core_pb2.SendResponse(ack=_nack("RATE_LIMITED")),
        core_pb2.SendResponse(ack=make_ack(True)),
    ]
    retry_send(client, _envelope(), policy=RetryPolicy(max_retries=1, backoff_base=0))

    first, second = _EXPORTER.get_finished_spans()
    assert (first.attributes or {})["macp.envelope.outcome"] == "rejected"
    assert (second.attributes or {})["macp.envelope.outcome"] == "accepted"


def test_send_without_otel_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    ack, stub = _send_sync(make_ack(True), _envelope())

    assert ack.ok
    assert list(stub.Send.call_args.kwargs["metadata"]) == list(AUTH.metadata())
    assert _EXPORTER.get_finished_spans() == ()


class _ThrowingSpan:
    def __getattr__(self, name: str) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("telemetry down")

        return boom


class _AttachFailsContext:
    def attach(self, ctx: Any) -> Any:
        raise RuntimeError("attach down")

    def detach(self, token: Any) -> None:  # pragma: no cover - never reached
        raise AssertionError("detach without attach")


class _ThrowingTracer:
    def __init__(self, span: Any = None) -> None:
        self._span = span

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        if self._span is None:
            raise RuntimeError("tracer down")
        return self._span


@pytest.mark.parametrize("tracer", [_ThrowingTracer(), _ThrowingTracer(_ThrowingSpan())])
@pytest.mark.parametrize(
    "response",
    [
        make_ack(True),
        envelope_pb2.Ack(ok=False, duplicate=True),
        _nack("SESSION_NOT_OPEN"),
        FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down"),
    ],
)
def test_throwing_telemetry_never_changes_send(
    monkeypatch: pytest.MonkeyPatch, tracer: Any, response: Any
) -> None:
    def outcome() -> Any:
        try:
            ack, _ = _send_sync(response, _envelope())
            return ("ok", ack)
        except Exception as exc:
            return (type(exc), getattr(exc, "code", None), str(exc))

    expected = outcome()
    loaded = _otel._load()
    assert loaded is not None
    monkeypatch.setattr(_otel, "_load", lambda: dataclasses.replace(loaded, tracer=tracer))

    assert outcome() == expected


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (make_ack(True), "accepted"),
        (_nack("SESSION_NOT_OPEN"), "rejected"),
    ],
)
def test_attach_failure_still_records_and_ends_span(
    monkeypatch: pytest.MonkeyPatch, send: Callable[..., Any], response: Any, outcome: str
) -> None:
    loaded = _otel._load()
    assert loaded is not None
    monkeypatch.setattr(
        _otel, "_load", lambda: dataclasses.replace(loaded, context=_AttachFailsContext())
    )

    if outcome == "accepted":
        ack, _ = send(response, _envelope())
        assert ack.ok
    else:
        with pytest.raises(MacpAckError):
            send(response, _envelope())

    assert (_only_span().attributes or {})["macp.envelope.outcome"] == outcome
