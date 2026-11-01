# A2A binding

`macp_sdk.a2a` maps MACP Envelopes, Acks and session states to and from
[A2A](https://a2a-protocol.org) 1.0 objects, as RFC-MACP-0014 defines. It has
the same functions, outcomes and error codes as macp-sdk-typescript's
`./a2a` module.

- Protocol spec: [RFC-MACP-0014](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/rfcs/RFC-MACP-0014-a2a-binding.md)
- Example transcript: [`examples/a2a-binding-session.json`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/examples/a2a-binding-session.json)

The module is a codec only. It has no transport, no extra, and no dependency
on an A2A SDK. It does not seal or attest anything. A2A objects are plain
dicts in A2A's JSON form (lowerCamelCase keys, `ROLE_USER`, `mediaType`), so
you can post them as JSON-RPC or HTTP+JSON bodies yourself, or hand them to an
official A2A SDK (see [below](#using-an-official-a2a-sdk)). Envelopes and Acks
are the protobuf `Envelope` and `Ack`; their JSON inside a Part is the
canonical form of `macp_sdk.canonical_json` (RFC-MACP-0001 §10).

## Sending an Envelope

```python
from macp_sdk import envelope_to_a2a_message, ack_from_a2a_message

msg = envelope_to_a2a_message(envelope, context_id="ctx-txn-48000")
# {"messageId": envelope.message_id, "role": "ROLE_USER",
#  "parts": [{"mediaType": "application/macp-envelope+json", "data": {...}}],
#  "extensions": ["https://multiagentcoordinationprotocol.io/a2a/extensions/macp/v1"],
#  "taskId": envelope.session_id, "contextId": "ctx-txn-48000"}

body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}}
# POST it with the A2A-Version: 1.0 and A2A-Extensions: <the URI> headers, then:
ack = ack_from_a2a_message(response["result"]["message"])
```

- `taskId` is the session id, except on a SessionStart or a Signal, which carry
  none (§6.1, §6.2).
- `messageId` defaults to `envelope.message_id`. A retry under a new A2A
  `messageId` passes `message_id=`; the server deduplicates on the Envelope's
  own `message_id` (§12).
- `encoding="proto"` sends a `raw` Part with the Envelope's Protobuf bytes
  (`application/macp-envelope+proto`) instead of a `data` Part. Use it, or a
  `payload_b64` Envelope, when the exact payload bytes matter (§5.3).

`ack_to_a2a_message(ack, message_id=..., task_id=..., context_id=...)` is the
server side. It refuses an Ack whose `error` is set when `ok` is true, or
missing when `ok` is false, as the decoder does (§5.2).

## Receiving an Envelope

```python
from macp_sdk import MacpA2aError, envelope_from_a2a_message

try:
    envelope = envelope_from_a2a_message(msg, max_payload_bytes=1_048_576)
except MacpA2aError as exc:
    exc.layer, exc.code  # e.g. ("macp", "INVALID_ENVELOPE")
```

The decoder takes the one Envelope Part and ignores Parts with any other
`mediaType` (§5.1). `envelope_from_a2a_part` decodes a bare Part.

`mediaType` is compared the way RFC 9110 compares media types: type and subtype
ASCII case-insensitively, with surrounding spaces and tabs ignored, so
`Application/MACP-Envelope+JSON` is an Envelope Part. A non-ASCII lookalike is
not. An Envelope Part whose `mediaType` carries parameters (`; charset=utf-8`)
is refused before it is decoded, as `ContentTypeNotSupportedError`, and it still
counts toward the one-Envelope-per-Message rule. The codec does not check
extension activation (§4): a server checks it first and answers
`ExtensionSupportRequiredError` before this one. The encoders always write the
exact lowercase type with no parameters.

`max_payload_bytes` (default 1 MiB, the runtime's `MACP_MAX_PAYLOAD_BYTES`
default) bounds the **decoded payload**: the base64-decoded `payload_b64`,
the Protobuf bytes a JSON `payload` converts to, or the payload of a `raw`
Envelope (§5.4). A `payload_b64` is measured before it is decoded, so an
oversized one costs no decoding.

Every decode failure raises `MacpA2aError`. Its `layer` says who answers it (§11):

| `layer` | `code` | When |
|---|---|---|
| `a2a` | `INVALID_REQUEST` | Two or more Envelope Parts in one Message (JSON-RPC -32602), or an Envelope Part whose `mediaType` carries parameters (`ContentTypeNotSupportedError`) |
| `macp` | `INVALID_ENVELOPE` | The Part does not decode, its `data`/`raw` arm does not match its `mediaType`, or `taskId` names another session: an in-band Ack error |
| `macp` | `PAYLOAD_TOO_LARGE` | The decoded payload is over `max_payload_bytes`: an in-band Ack error |
| `codec` | `NOT_A_MACP_MESSAGE` | No Envelope Part, or no Ack Part |
| `codec` | `INVALID_ACK` | A malformed Ack Message |
| `codec` | `INVALID_SEQUENCE` | `EXT/sequence` missing, not an integer, or below 1 |
| `codec` | `INVALID_SESSION_STATE` | `EXT/sessionState` missing or not a bare state name |

The `codec` codes are local; RFC-MACP-0014 gives these cases no wire code.
`MacpA2aError` is a `MacpSdkError` and a `ValueError`.

The encoders raise it too, so the codec never writes what it would refuse:
`macp`/`INVALID_ENVELOPE` for an Envelope whose payload does not parse as its
standard schema or whose timestamp is outside the years 0001 to 9999,
`codec`/`INVALID_ACK` for an inconsistent Ack, and `codec`/`INVALID_SEQUENCE`
for a bad sequence. A bad argument (an unknown `encoding`, a negative
`max_payload_bytes`, a bare string for `modes`) is a plain `ValueError`.

## Observing a session

A server publishes each accepted Envelope as an Artifact (§9.1) and each state
change as Task metadata (§8):

```python
from macp_sdk import (
    envelope_from_a2a_artifact,
    envelope_to_a2a_artifact,
    session_state_from_a2a,
    session_state_to_a2a,
)

artifact = envelope_to_a2a_artifact(envelope, sequence=3)
task_state, metadata = session_state_to_a2a("SESSION_STATE_RESOLVED")
# ("TASK_STATE_COMPLETED", {".../macp/v1/sessionState": "RESOLVED"})

envelope, sequence = envelope_from_a2a_artifact(event["artifactUpdate"]["artifact"])
state = session_state_from_a2a(task)  # a Task, or an unwrapped TaskStatusUpdateEvent
```

`session_state_from_a2a` reads only `EXT/sessionState` and never falls back to
the TaskState, which cannot tell OPEN from SUSPENDED. `EXT/sequence` may arrive
as an integral float (`2.0`), as it does from any Protobuf-based A2A SDK.

**Deduplication is yours.** A2A subscriptions have no resume position, so a
client that reconnects sees Artifacts again. Discard any whose sequence you have
already applied, never count one twice, and recover a gap in the sequence from
`GetTask` or from the runtime (§9.2). The codec returns the sequence for that and
keeps no state.

## Declaring and reading errors

```python
from macp_sdk import error_info_from_a2a, macp_agent_extension

card["capabilities"]["extensions"] = [
    macp_agent_extension(
        macp_versions=["1.0"],
        modes=["macp.mode.decision.v1"],
        required=True,
        manifest_url="https://runtime.example.com/.well-known/macp.json",
    )
]

info = error_info_from_a2a(response["error"])  # or a bare details array
if info is not None and info["duplicate"]:
    ...  # the envelope was accepted earlier: treat as success (§12)
```

`error_info_from_a2a` picks the `google.rpc.ErrorInfo` whose `domain` is
`multiagentcoordinationprotocol.io` from a JSON-RPC error's `data` or from a bare
details array (HTTP+JSON, gRPC). It returns `None` when there is none.
`duplicate` is true only for the metadata string `"true"`.

## Using an official A2A SDK

The dicts are A2A's JSON form, so they convert to an official SDK's types
without the codec depending on one. With [`a2a-sdk`](https://pypi.org/project/a2a-sdk/)
(Python, 1.x), whose types are Protobuf messages:

```python
from a2a.types import Message
from google.protobuf import json_format

message = json_format.ParseDict(envelope_to_a2a_message(envelope), Message())
# ... send it with the a2a-sdk client, then decode what comes back:
envelope = envelope_from_a2a_message(json_format.MessageToDict(received))
```

`tests/unit/test_a2a_interop.py` parses every Message, Artifact, Task and
AgentExtension this codec writes as the `a2a-sdk` type with unknown fields
refused, and decodes what that type writes back. `a2a-sdk` is a dev dependency
of this repo only.

## Conformance

`tests/unit/test_a2a.py` runs the spec's 106 A2A vectors
(`schemas/conformance/a2a/`, vendored under `tests/vectors/a2a/`) and replays the
example transcript end to end. macp-sdk-typescript runs the same vectors.
