# Observability (OpenTelemetry)

The SDK can emit OpenTelemetry traces and propagate W3C trace context to the
runtime. It follows the MACP semantic conventions (`macp-semconv/0.1`,
experimental) and RFC-MACP-0006 §2.1. It emits the same span, with the same
attributes and outcome rules, as macp-sdk-typescript.

- Protocol spec: [OpenTelemetry](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/opentelemetry.md)
- Attribute registry: [`schemas/otel/macp-semconv.json`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/schemas/otel/macp-semconv.json)

## Enabling it

Install the optional extra:

```bash
pip install "macp-sdk-python[otel]"
```

The extra installs only `opentelemetry-api`. To export spans, configure a
`TracerProvider` and an exporter with `opentelemetry-sdk`, as you would for any
instrumented library. Without a configured provider, OpenTelemetry's no-op
provider applies and nothing is recorded. That is also how you turn
instrumentation off: there is no SDK flag.

The SDK imports `opentelemetry` only on the first RPC, never at
`import macp_sdk`. Without the package installed, nothing changes: the same
metadata, the same behaviour and no extra imports. If the installation is
broken, the SDK treats it as absent. Telemetry never changes what an RPC
returns or raises, because every OpenTelemetry call is guarded.

## What is emitted

### One CLIENT span per `send()`

Each `MacpClient.send` / `AsyncMacpClient.send` call gets one span named
`send {mode}`, for example `send macp.mode.decision.v1`. When the Envelope has no
mode, the span is named just `send`. Session helpers, the agent `Participant`,
`send_signal` / `send_progress` and each `retry_send` attempt all go through
`send()`, so a retried send produces one span per attempt.

The span carries identifiers only:

| Attribute | Value |
|---|---|
| `macp.operation.name` | `send` |
| `macp.message.id`, `macp.message.type` | from the Envelope |
| `macp.session.id`, `macp.mode.id`, `macp.sender.id`, `macp.version` | from the Envelope, when non-empty |
| `macp.envelope.outcome` | `accepted`, `rejected` or `duplicate` |
| `error.type` | the MACP error code, or the gRPC status name |

The span classifies the result the same way the SDK reports it to you:

| Result of `send()` | `macp.envelope.outcome` | `error.type` | Span status |
|---|---|---|---|
| Ack accepted | `accepted` | — | UNSET |
| Ack with `duplicate` set | `duplicate` | — | UNSET |
| NACK, raised or returned with `raise_on_nack=False` | `rejected` | the Ack's code | UNSET |
| `MacpAckError` from a gRPC status (`macp-error-code`, or the pre-0.9 fallback) | `rejected` | the error's code | UNSET |
| `MacpTransportError` (unavailable, deadline, …) | — | the gRPC status name, e.g. `UNAVAILABLE` | ERROR, description = that name |
| Any other exception, including cancellation | — | `_OTHER` | ERROR, description `_OTHER` |

A rejection is a normal protocol outcome, so it leaves the status UNSET. Only
a transport or implementation failure is an error. An empty code is recorded
as `_OTHER`.

### Trace context on RPCs

When a span is active, every request carries `traceparent`, plus `tracestate` if
set, in its gRPC metadata. This covers `send`, the other unary RPCs, and the
stream-open request of `open_stream()` and the watchers. Inside `send()`, the
value is the `send` span's own context, so the runtime's server span becomes
its child.

The SDK uses the W3C Trace Context propagator directly, not the global
propagator. So the SDK itself never injects `baggage`, and a globally
configured B3 or other propagator does not change what the SDK sends. Trace
context is never written into the Envelope. gRPC auto-instrumentation (see
[Limitations](#limitations)) injects through the global propagator, which
defaults to `tracecontext,baggage`. If baggage must not leave the process,
set `OTEL_PROPAGATORS=tracecontext`.

## Privacy

Spans never record payloads, reasons, rationale, extension values or error
messages. That includes the runtime's error message and exception text. The
SDK never calls `record_exception`. `macp.sender.id` is the raw sender id, which
the semantic conventions allow. If sender ids are sensitive in your
deployment, drop or hash the attribute in your exporter or collector.

## Limitations

- **Streams:** `MacpStream.send` writes frames on an open bidirectional
  stream, so there is no per-Envelope request and no per-Envelope span. The
  stream-open request carries the trace context that was active when it
  opened.
- **gRPC auto-instrumentation:** `opentelemetry-instrumentation-grpc` may also
  write `traceparent`, from its own client span, which sits inside `send`. The
  runtime's server span then becomes a child of that gRPC span, which is the
  nesting the semantic conventions describe.
- **Stability:** `macp-semconv/0.1` is experimental. Attribute names follow the
  vendored `tests/otel/macp-semconv.json`, which CI checks for drift against the
  spec.
