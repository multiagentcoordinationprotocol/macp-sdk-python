"""OpenTelemetry against a live runtime: the runtime accepts the W3C trace
context the SDK injects, and each ``send()`` span records the runtime's real
outcome (accepted, duplicate, rejected).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from macp.modes.decision.v1 import decision_pb2
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from macp_sdk import AuthConfig, DecisionSession, MacpClient, _otel
from macp_sdk.envelope import build_envelope, new_session_id
from macp_sdk.errors import MacpAckError

from .conftest import make_client

pytestmark = pytest.mark.integration

MODE = "macp.mode.decision.v1"


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    # A local provider, swapped into the SDK's tracer, so this module never
    # touches the process-global TracerProvider.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _otel._reset()
    loaded = _otel._load()
    assert loaded is not None
    patched = dataclasses.replace(loaded, tracer=provider.get_tracer(_otel.SCOPE_NAME))
    monkeypatch.setattr(_otel, "_load", lambda: patched)
    yield exporter
    _otel._reset()


@pytest.fixture
def client() -> Iterator[MacpClient]:
    c = make_client("coordinator")
    yield c
    c.close()


def _outcomes(exporter: InMemorySpanExporter) -> list[tuple[str, str | None, str | None]]:
    return [
        (
            span.name,
            (span.attributes or {}).get("macp.envelope.outcome"),  # type: ignore[misc]
            (span.attributes or {}).get("error.type"),  # type: ignore[misc]
        )
        for span in exporter.get_finished_spans()
    ]


def test_send_spans_record_runtime_outcomes(
    client: MacpClient, exporter: InMemorySpanExporter
) -> None:
    session = DecisionSession(client, auth=AuthConfig.for_dev_agent("coordinator"))
    assert session.start(
        intent="otel integration", participants=["coordinator", "alice"], ttl_ms=30_000
    ).ok

    proposal = build_envelope(
        mode=MODE,
        message_type="Proposal",
        session_id=session.session_id,
        sender="coordinator",
        payload=decision_pb2.ProposalPayload(proposal_id="p1", option="ship").SerializeToString(),
    )
    assert client.send(proposal).ok
    # The runtime deduplicates on message_id.
    assert client.send(proposal).duplicate

    with pytest.raises(MacpAckError) as rejected:
        client.send(
            build_envelope(
                mode=MODE,
                message_type="Proposal",
                session_id=new_session_id(),
                sender="coordinator",
                payload=decision_pb2.ProposalPayload(
                    proposal_id="p1", option="ship"
                ).SerializeToString(),
            )
        )

    assert _outcomes(exporter) == [
        (f"send {MODE}", "accepted", None),
        (f"send {MODE}", "accepted", None),
        (f"send {MODE}", "duplicate", None),
        (f"send {MODE}", "rejected", rejected.value.failure.code),
    ]
    for span in exporter.get_finished_spans():
        assert span.status.status_code is StatusCode.UNSET


def test_trace_context_is_accepted_on_every_rpc_kind(
    client: MacpClient, exporter: InMemorySpanExporter
) -> None:
    from opentelemetry.sdk.trace import TracerProvider as _Provider

    tracer = _Provider().get_tracer("caller")
    with tracer.start_as_current_span("caller"):
        assert client.initialize().runtime_info.name
        assert client.list_modes().modes
        session = DecisionSession(client, auth=AuthConfig.for_dev_agent("coordinator"))
        assert session.start(
            intent="otel ctx", participants=["coordinator", "alice"], ttl_ms=30_000
        ).ok
        assert client.get_session(session.session_id).metadata.session_id == session.session_id
