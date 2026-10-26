"""Optional OpenTelemetry support (``macp-semconv/0.1``, RFC-MACP-0006 §2.1).

Active only when ``opentelemetry-api`` is installed (the ``otel`` extra).
Nothing here imports ``opentelemetry`` until the first RPC, so without the
package the SDK's behaviour, metadata and import graph are unchanged.

Trace context travels in gRPC metadata only, as W3C ``traceparent`` and
``tracestate``. The W3C propagator is used directly rather than the global
one: the global default also injects ``baggage``, which can carry arbitrary
user data, and a configured B3 propagator would change what goes on the wire.

Telemetry never changes an RPC's outcome: every OpenTelemetry call is
guarded, and a broken installation counts as absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

#: The instrumentation scope name (shared convention with macp-sdk-typescript,
#: which uses its own package name).
SCOPE_NAME = "macp-sdk-python"


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
