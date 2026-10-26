"""OpenTelemetry: trace-context propagation and the ``send`` span (Wave 5 S2).

The ``test_otel_*`` names match macp-sdk-typescript's. Assertions on span
names and attributes are read from the vendored ``tests/otel/macp-semconv.json``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from macp_sdk import AsyncMacpClient, AuthConfig, _otel
from macp_sdk import client as client_module
from tests.conftest import client_with_stub

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
