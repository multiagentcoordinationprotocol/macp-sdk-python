"""Centralized protobuf encode/decode registry for MACP message types.

Uses compiled ``_pb2`` modules (via ``google.protobuf.symbol_database``) to
look up message classes by fully-qualified type name.
"""

from __future__ import annotations

import base64
import importlib
import json
from typing import Any

from google.protobuf import json_format, symbol_database  # type: ignore[import-untyped]

from .constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_MULTI_ROUND,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)

# ── Type-name mappings (mirrors TypeScript CORE_MAP / MODE_MAP) ──────

CORE_MAP: dict[str, str] = {
    "SessionStart": "macp.v1.SessionStartPayload",
    "Commitment": "macp.v1.CommitmentPayload",
    "Signal": "macp.v1.SignalPayload",
    "Progress": "macp.v1.ProgressPayload",
}

MODE_MAP: dict[str, dict[str, str]] = {
    MODE_DECISION: {
        "Proposal": "macp.modes.decision.v1.ProposalPayload",
        "Evaluation": "macp.modes.decision.v1.EvaluationPayload",
        "Objection": "macp.modes.decision.v1.ObjectionPayload",
        "Vote": "macp.modes.decision.v1.VotePayload",
    },
    MODE_PROPOSAL: {
        "Proposal": "macp.modes.proposal.v1.ProposalPayload",
        "CounterProposal": "macp.modes.proposal.v1.CounterProposalPayload",
        "Accept": "macp.modes.proposal.v1.AcceptPayload",
        "Reject": "macp.modes.proposal.v1.RejectPayload",
        "Withdraw": "macp.modes.proposal.v1.WithdrawPayload",
    },
    MODE_TASK: {
        "TaskRequest": "macp.modes.task.v1.TaskRequestPayload",
        "TaskAccept": "macp.modes.task.v1.TaskAcceptPayload",
        "TaskReject": "macp.modes.task.v1.TaskRejectPayload",
        "TaskUpdate": "macp.modes.task.v1.TaskUpdatePayload",
        "TaskComplete": "macp.modes.task.v1.TaskCompletePayload",
        "TaskFail": "macp.modes.task.v1.TaskFailPayload",
    },
    MODE_HANDOFF: {
        "HandoffOffer": "macp.modes.handoff.v1.HandoffOfferPayload",
        "HandoffContext": "macp.modes.handoff.v1.HandoffContextPayload",
        "HandoffAccept": "macp.modes.handoff.v1.HandoffAcceptPayload",
        "HandoffDecline": "macp.modes.handoff.v1.HandoffDeclinePayload",
    },
    MODE_QUORUM: {
        "ApprovalRequest": "macp.modes.quorum.v1.ApprovalRequestPayload",
        "Approve": "macp.modes.quorum.v1.ApprovePayload",
        "Reject": "macp.modes.quorum.v1.RejectPayload",
        "Abstain": "macp.modes.quorum.v1.AbstainPayload",
    },
    MODE_MULTI_ROUND: {
        # Runtime v0.5.0 / macp-proto >= 0.1.4: Contribute is the last
        # advertised mode moved onto the canonical protobuf wire format.
        # Legacy JSON (``{"value": "..."}``) is still decoded (tried first,
        # permanently) — see ``decode_known_payload``.
        "Contribute": "macp.modes.multi_round.v1.ContributePayload",
    },
}

# Ensure all _pb2 modules are imported so descriptors are registered.
_PB2_MODULES_LOADED = False


def _ensure_pb2_imports() -> None:
    global _PB2_MODULES_LOADED
    if _PB2_MODULES_LOADED:
        return
    # Import all proto modules to register their descriptors in the global pool.
    for _mod in (
        "macp.v1.core_pb2",
        "macp.v1.envelope_pb2",
        "macp.v1.policy_pb2",
        "macp.modes.decision.v1.decision_pb2",
        "macp.modes.proposal.v1.proposal_pb2",
        "macp.modes.task.v1.task_pb2",
        "macp.modes.handoff.v1.handoff_pb2",
        "macp.modes.quorum.v1.quorum_pb2",
        "macp.modes.multi_round.v1.multi_round_pb2",
    ):
        importlib.import_module(_mod)
    _PB2_MODULES_LOADED = True


class ProtoRegistry:
    """Registry for protobuf type-name-based encode/decode of MACP payloads."""

    def __init__(self) -> None:
        _ensure_pb2_imports()
        self._db = symbol_database.Default()

    def get_known_type_name(self, mode: str, message_type: str) -> str | None:
        """Return the fully-qualified protobuf type name, or None if unknown."""
        return MODE_MAP.get(mode, {}).get(message_type) or CORE_MAP.get(message_type)

    def encode_message(self, type_name: str, value: dict[str, Any]) -> bytes:
        """Encode *value* as a protobuf message identified by *type_name*."""
        cls = self._db.GetSymbol(type_name)
        msg = json_format.ParseDict(value, cls())
        return msg.SerializeToString()

    def decode_message(self, type_name: str, payload: bytes) -> dict[str, Any]:
        """Decode *payload* into a dict using the message class for *type_name*."""
        cls = self._db.GetSymbol(type_name)
        msg = cls()
        msg.ParseFromString(payload)
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)

    def encode_known_payload(self, mode: str, message_type: str, value: dict[str, Any]) -> bytes:
        """Encode *value* using the known type mapping for *mode*/*message_type*."""
        type_name = self.get_known_type_name(mode, message_type)
        if type_name is None:
            raise ValueError(f"unknown payload mapping for {mode}/{message_type}")
        if type_name == "__json__":
            return json.dumps(value).encode("utf-8")
        return self.encode_message(type_name, value)

    def decode_known_payload(
        self, mode: str, message_type: str, payload: bytes
    ) -> dict[str, Any] | None:
        """Decode *payload* using the known type mapping, or try UTF-8 fallback."""
        type_name = self.get_known_type_name(mode, message_type)
        if type_name is None or type_name == "__json__":
            return self._try_decode_utf8(payload)
        if mode == MODE_MULTI_ROUND and message_type == "Contribute":
            # RFC-MACP contract: multi_round Contribute accepts legacy JSON
            # (``{"value": "..."}``) *permanently* and tries it first, so
            # pre-proto histories/replays decode byte-identically. Proto
            # (``ContributePayload``) is the new canonical encoding; its
            # serialized bytes are not valid JSON, so the JSON attempt fails
            # loudly and we fall through to proto.
            return self._decode_json_first_then_proto(type_name, payload)
        return self.decode_message(type_name, payload)

    def _decode_json_first_then_proto(
        self, type_name: str, payload: bytes
    ) -> dict[str, Any] | None:
        if not payload:
            return None
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self.decode_message(type_name, payload)
        # Legacy JSON payload — keep the ``{"encoding": "json", "json": ...}``
        # shape that existing consumers of decoded dicts already handle.
        return {"encoding": "json", "json": parsed}

    @staticmethod
    def _try_decode_utf8(payload: bytes) -> dict[str, Any] | None:
        if not payload:
            return None
        text = payload.decode("utf-8")
        try:
            return {"encoding": "json", "json": json.loads(text)}
        except (json.JSONDecodeError, ValueError):
            return {
                "encoding": "text",
                "text": text,
                "payload_base64": base64.b64encode(payload).decode(),
            }
