from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from ..auth import AuthConfig
from ..client import MacpClient
from ..constants import DEFAULT_POLICY_VERSION
from .participant import InitiatorConfig, Participant

__all__ = ["InitiatorConfig", "from_bootstrap"]


def _decode_extensions(raw: Any) -> dict[str, bytes]:
    """Coerce a bootstrap ``session_start.extensions`` map into ``dict[str, bytes]``.

    The protobuf ``map<string, bytes>`` is JSON-encoded as base64 strings
    (proto-JSON canonical), so try base64 first and fall back to raw
    UTF-8 bytes for hand-authored bootstraps.
    """
    if not isinstance(raw, dict):
        return {}
    decoded: dict[str, bytes] = {}
    for key, value in raw.items():
        if isinstance(value, bytes):
            decoded[str(key)] = value
        elif isinstance(value, str):
            try:
                decoded[str(key)] = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                decoded[str(key)] = value.encode("utf-8")
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
    runtime_url = str(ctx.get("runtime_url") or ctx.get("runtime_address") or "localhost:50051")
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
            # default). The TypeScript bootstrap schema does not yet map this
            # key, so it is Python-only for now; a bootstrap that omits it
            # behaves identically in both SDKs.
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
