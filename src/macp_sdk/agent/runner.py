from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from .._logging import logger
from ..auth import AuthConfig
from ..client import MacpClient
from ..constants import DEFAULT_POLICY_VERSION
from .participant import InitiatorConfig, Participant

__all__ = ["InitiatorConfig", "from_bootstrap"]


def _decode_extensions(raw: Any) -> dict[str, bytes]:
    """Coerce a bootstrap ``session_start.extensions`` map into ``dict[str, bytes]``.

    The protobuf ``map<string, bytes>`` is JSON-encoded as base64 strings
    (proto-JSON canonical: ``schemas/json/macp-agent-bootstrap.schema.json``
    declares ``extensions`` values as ``{"type": "string", "contentEncoding":
    "base64"}``, mirroring ``macp-envelope.schema.json``'s ``$defs/Base64Bytes``
    and RFC-MACP-0001 §10.3), so base64 is tried first.

    The UTF-8 fallback below is deliberately laxer than that canonical schema
    -- ``contentEncoding`` is a draft-2020-12 *annotation*, not an assertion,
    so nothing in the schema itself rejects a non-base64 string; this SDK
    chooses to accept one anyway, decoded as raw UTF-8 bytes, so a
    hand-authored bootstrap that skipped base64-encoding its extension values
    still loads. A value that is neither ``str`` nor ``bytes`` has no
    reasonable interpretation under either path and is not silently
    dropped -- it raises, since the schema now says every value MUST be a
    string (issue #93 item 2). The same applies to the container itself:
    only an absent/``None`` ``extensions`` field (the common no-extensions
    bootstrap) defaults to ``{}``; a *present* value of the wrong type (a
    list, a string, ...) is equally malformed and raises rather than
    silently vanishing (#97 follow-up).

    **Known, accepted ambiguity (issue #121):** trying base64 first means a
    plain string that *happens* to also be syntactically valid base64 (e.g.
    ``"abcd"`` or ``"pack"`` -- any string whose length is a multiple of 4
    over the base64 alphabet) silently decodes as base64 bytes instead of
    being treated as the literal string a bootstrap author intended. There
    is no way to tell the two apart from the string alone, and this SDK is
    the canonical source ``macp-sdk-typescript`` mirrors for interop, so
    changing the heuristic would need a coordinated, versioned decision
    across both SDKs (and likely a wire-shape change), not a local fix.
    Decided: keep the heuristic as-is rather than add a disambiguation
    mechanism -- it is a diagnosability/correctness-on-the-margins wart, not
    a live bug, and the ``logger.debug`` call below at least makes a
    base64-decode *failure* observable. A value that round-trips through
    *both* interpretations without the caller noticing is the accepted
    cost; callers who need an unambiguous literal string should route it
    through a different field instead of ``extensions``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"bootstrap session_start.extensions must be an object, got {type(raw).__name__}"
        )
    decoded: dict[str, bytes] = {}
    for key, value in raw.items():
        if isinstance(value, bytes):
            decoded[str(key)] = value
        elif isinstance(value, str):
            try:
                decoded[str(key)] = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                logger.debug(
                    "bootstrap extensions[%r] is not valid base64; using its raw UTF-8 bytes",
                    key,
                )
                decoded[str(key)] = value.encode("utf-8")
        else:
            raise ValueError(
                f"bootstrap extensions[{key!r}] must be a base64-encoded string "
                f"(or bytes), got {type(value).__name__}"
            )
    return decoded


def from_bootstrap(bootstrap_path: str | None = None) -> Participant:
    """Create a Participant from a bootstrap context file.

    Reads the flat bootstrap format produced by the examples-service::

        {
            "participant_id": "...",
            "session_id": "...",
            "mode": "macp.mode.decision.v1",
            "runtime_url": "localhost:50051",
            "auth_token": "...",
            "participants": ["agent-a", "agent-b"],
            "secure": false,
            "allow_insecure": true,
            "initiator": { ... }
        }

    Also accepts ``auth.bearer_token`` for backwards compatibility.
    """
    path = bootstrap_path or os.environ.get("MACP_BOOTSTRAP_FILE")
    if not path:
        raise ValueError("No bootstrap path provided and MACP_BOOTSTRAP_FILE not set")

    with open(path) as f:
        ctx: dict[str, Any] = json.load(f)

    participant_id = str(ctx["participant_id"])
    session_id = str(ctx["session_id"])
    mode = str(ctx["mode"])
    runtime_url_raw = ctx.get("runtime_url") or ctx.get("runtime_address")
    if not runtime_url_raw:
        raise ValueError(
            "bootstrap JSON must set 'runtime_url' or 'runtime_address' "
            "(no implicit default) -- got neither key"
        )
    runtime_url = str(runtime_url_raw)
    secure = bool(ctx.get("secure", True))
    allow_insecure = bool(ctx.get("allow_insecure", False))

    auth: AuthConfig | None = None
    auth_token = ctx.get("auth_token")
    agent_id = ctx.get("agent_id")
    auth_data = ctx.get("auth")

    if auth_token:
        auth = AuthConfig.for_bearer(
            str(auth_token),
            sender_hint=participant_id,
            expected_sender=participant_id,
        )
    elif isinstance(auth_data, dict) and auth_data.get("bearer_token"):
        auth = AuthConfig.for_bearer(
            str(auth_data["bearer_token"]),
            sender_hint=participant_id,
            expected_sender=str(auth_data.get("expected_sender") or participant_id),
        )
    elif agent_id:
        auth = AuthConfig.for_dev_agent(str(agent_id), expected_sender=participant_id)
    elif isinstance(auth_data, dict) and auth_data.get("agent_id"):
        auth = AuthConfig.for_dev_agent(str(auth_data["agent_id"]), expected_sender=participant_id)

    client = MacpClient(
        target=runtime_url,
        secure=secure,
        allow_insecure=allow_insecure,
        auth=auth,
    )

    raw_participants = ctx.get("participants")
    participants: list[str] = (
        [str(p) for p in raw_participants] if isinstance(raw_participants, list) else []
    )

    mode_version = ctx.get("mode_version")
    configuration_version = ctx.get("configuration_version")
    policy_version = ctx.get("policy_version")

    initiator_config: InitiatorConfig | None = None
    initiator_data = ctx.get("initiator")
    if isinstance(initiator_data, dict):
        ss = initiator_data.get("session_start", {})
        kickoff = initiator_data.get("kickoff")

        def _str_or(key: str, fallback: object) -> str | None:
            """Pick value from session_start, then fallback, coercing to str."""
            val = ss.get(key)
            if val is not None:
                return str(val)
            return str(fallback) if fallback else None

        initiator_config = InitiatorConfig(
            intent=str(ss.get("intent", "")),
            participants=[str(p) for p in ss.get("participants", participants)],
            ttl_ms=int(ss.get("ttl_ms", 300000)),
            # Runtime v0.5.0 per-session suspension cap; absent → 0 (runtime
            # default). macp-sdk-typescript's BootstrapPayload also declares
            # and maps this key (src/agent/runner.ts:28,103); a bootstrap
            # that omits it behaves identically in both SDKs.
            max_suspend_ms=int(ss.get("max_suspend_ms", 0)),
            context_id=str(ss.get("context_id", "")),
            extensions=_decode_extensions(ss.get("extensions")),
            roots=ss.get("roots"),
            mode_version=_str_or("mode_version", mode_version),
            configuration_version=_str_or("configuration_version", configuration_version),
            policy_version=_str_or("policy_version", policy_version),
            kickoff_message_type=(
                str(kickoff["message_type"]) if kickoff and "message_type" in kickoff else None
            ),
            kickoff_payload=kickoff.get("payload", {}) if kickoff else {},
        )

    participant = Participant(
        participant_id=participant_id,
        session_id=session_id,
        mode=mode,
        client=client,
        auth=auth,
        participants=participants,
        mode_version=str(mode_version) if mode_version else None,
        configuration_version=str(configuration_version) if configuration_version else None,
        policy_version=str(policy_version) if policy_version else DEFAULT_POLICY_VERSION,
        initiator_config=initiator_config,
    )

    _bind_cancel_callback(participant, ctx.get("cancel_callback"))
    return participant


def _bind_cancel_callback(participant: Participant, raw: Any) -> None:
    """Start a cancel-callback HTTP server bound to ``participant.stop``.

    Reads the bootstrap ``cancel_callback`` field (``{host, port, path}``)
    and, if present, launches a daemon HTTP server that calls
    ``participant.stop()`` on POST. The server is attached to the
    participant so its event-loop shutdown (or an incoming POST) tears
    it down cleanly.

    The bootstrap field is optional; callers that never set it see no
    behavioural change. Reference: RFC-0001 §7.2 Option A, and the
    TypeScript SDK's equivalent wiring in
    ``examples-service/src/example-agents/runtime/risk-decider.worker.ts``.
    """
    if not isinstance(raw, dict):
        return
    host = str(raw.get("host") or "")
    port = raw.get("port")
    path = str(raw.get("path") or "")
    if not host or port is None or not path:
        return

    # Local import so the stdlib ``http.server`` is paid for only when
    # a bootstrap actually asks for a callback.
    from .cancel_callback import start_cancel_callback_server

    def _on_cancel(_run_id: str, _reason: str) -> None:
        participant.stop()

    server = start_cancel_callback_server(
        host=host,
        port=int(port),
        path=path,
        on_cancel=_on_cancel,
    )
    participant.attach_cancel_callback_server(server)
