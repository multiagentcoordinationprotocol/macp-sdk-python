"""Optional OpenTelemetry support (``macp-semconv/0.1``, RFC-MACP-0006 §2.1).

Active only when ``opentelemetry-api`` is installed (the ``otel`` extra).
Nothing here imports ``opentelemetry`` until the first RPC, so without the
package the SDK's behaviour, metadata and import graph are unchanged.

Trace context travels in gRPC metadata only, as W3C ``traceparent`` and
``tracestate``. The W3C propagator is used directly rather than the global
one: the global default also injects ``baggage``, which can carry arbitrary
user data, and a configured B3 propagator would change what goes on the wire.

Each ``send()`` gets one CLIENT span, ``send {macp.mode.id}`` (``span.macp.send``
in the semconv registry). It records identifiers only, never payload, reason,
rationale or exception messages, and classifies the outcome exactly as the
caller's result or exception does: a rejection leaves the status UNSET; only a
transport or implementation failure is ERROR. The rules match
macp-sdk-typescript outcome for outcome.

Telemetry never changes an RPC's outcome: every OpenTelemetry call is
guarded, and a broken installation counts as absent.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import MacpAckError, MacpTransportError

if TYPE_CHECKING:
    from macp.v1 import envelope_pb2
    from opentelemetry.trace import Span, Tracer
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

#: The instrumentation scope name (shared convention with macp-sdk-typescript,
#: which uses its own package name).
SCOPE_NAME = "macp-sdk-python"

#: OpenTelemetry's ``error.type`` value when no specific code is known.
ERROR_TYPE_OTHER = "_OTHER"


@dataclass(frozen=True)
class _Otel:
    trace: Any  # the opentelemetry.trace module
    context: Any  # the opentelemetry.context module
    propagator: TraceContextTextMapPropagator
    tracer: Tracer


_loaded = False
_cache: _Otel | None = None


def _load() -> _Otel | None:
    """Import OpenTelemetry once; ``None`` when it is absent or broken.

    The result is cached either way. Two threads racing here both import the
    same modules, which is harmless, so there is no lock.
    """
    global _cache, _loaded
    if not _loaded:
        _cache = _import()
        _loaded = True
    return _cache


def _import() -> _Otel | None:
    try:
        from importlib.metadata import PackageNotFoundError, version

        from opentelemetry import context, trace
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        try:
            sdk_version = version("macp-sdk-python")
        except PackageNotFoundError:
            sdk_version = ""
        return _Otel(
            trace=trace,
            context=context,
            propagator=TraceContextTextMapPropagator(),
            tracer=trace.get_tracer(SCOPE_NAME, sdk_version),
        )
    except Exception:
        return None


def _reset() -> None:
    """Forget the cached import (tests only)."""
    global _cache, _loaded
    _cache = None
    _loaded = False


def inject(metadata: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return ``metadata`` plus ``traceparent``/``tracestate`` for the active span.

    Returns a new list and never mutates ``metadata``. Adds nothing when
    OpenTelemetry is absent or the active span context is invalid.
    """
    result = list(metadata)
    otel = _load()
    if otel is None:
        return result
    try:
        if not otel.trace.get_current_span().get_span_context().is_valid:
            return result
        carrier: dict[str, str] = {}
        otel.propagator.inject(carrier)
    except Exception:
        return result
    result.extend((key, carrier[key]) for key in ("traceparent", "tracestate") if key in carrier)
    return result


class SendSpan:
    """Handle on one ``send`` span; a no-op when tracing is off."""

    __slots__ = ("_otel", "_span")

    def __init__(self, otel: _Otel | None, span: Span | None) -> None:
        self._otel = otel
        self._span = span

    def ack(self, ack: envelope_pb2.Ack) -> None:
        """Record the runtime's Ack (before any raise-on-nack check)."""
        span = self._span
        if span is None:
            return
        try:
            if ack.duplicate:
                span.set_attribute("macp.envelope.outcome", "duplicate")
            elif ack.ok:
                span.set_attribute("macp.envelope.outcome", "accepted")
            else:
                _rejected(span, ack.error.code)
        except Exception:
            pass

    def _error(self, exc: BaseException) -> None:
        span, otel = self._span, self._otel
        if span is None or otel is None:
            return
        try:
            # A rejection is a normal protocol outcome: the status stays UNSET.
            # The SDK raises MacpAckError for a NACK, a status carrying
            # ``macp-error-code`` and the pre-0.9 status fallback, so the span
            # classifies exactly as the caller's exception does.
            if isinstance(exc, MacpAckError):
                _rejected(span, exc.failure.code)
                return
            error_type = ERROR_TYPE_OTHER
            if isinstance(exc, MacpTransportError):
                error_type = exc.macp_code or exc.code or ERROR_TYPE_OTHER
            span.set_attribute("error.type", error_type)
            # The description is the code only; exception messages are never recorded.
            span.set_status(otel.trace.Status(otel.trace.StatusCode.ERROR, error_type))
        except Exception:
            pass


def _rejected(span: Span, code: str | None) -> None:
    span.set_attribute("macp.envelope.outcome", "rejected")
    span.set_attribute("error.type", code or ERROR_TYPE_OTHER)


def _send_attributes(envelope: envelope_pb2.Envelope) -> dict[str, str]:
    attributes = {
        "macp.operation.name": "send",
        "macp.message.id": envelope.message_id,
        "macp.message.type": envelope.message_type,
    }
    for key, value in (
        ("macp.session.id", envelope.session_id),
        ("macp.mode.id", envelope.mode),
        ("macp.sender.id", envelope.sender),
        ("macp.version", envelope.macp_version),
    ):
        if value:
            attributes[key] = value
    return attributes


@contextmanager
def send_span(envelope: envelope_pb2.Envelope) -> Iterator[SendSpan]:
    """Run one ``send`` inside its CLIENT span, made current for the block.

    Metadata built inside the block carries this span's ``traceparent``, so
    the runtime's server span is its child. An exception escaping the block is
    classified (never recorded) and re-raised unchanged.
    """
    otel = _load()
    span: Span | None = None
    token: object = None
    if otel is not None:
        try:
            span = otel.tracer.start_span(
                f"send {envelope.mode}" if envelope.mode else "send",
                kind=otel.trace.SpanKind.CLIENT,
                attributes=_send_attributes(envelope),
            )
            token = otel.context.attach(otel.trace.set_span_in_context(span))
        except Exception:
            pass
    handle = SendSpan(otel, span)
    try:
        yield handle
    except BaseException as exc:
        handle._error(exc)
        raise
    finally:
        if otel is not None and token is not None:
            with suppress(Exception):
                otel.context.detach(token)
        if span is not None:
            with suppress(Exception):
                span.end()
