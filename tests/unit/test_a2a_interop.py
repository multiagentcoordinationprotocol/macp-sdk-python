"""The A2A codec's objects against the official A2A 1.0 types (``a2a-sdk``).

``a2a-sdk`` is a dev-only dependency: the SDK itself never imports it. Every
Message, Artifact, AgentExtension and Task this codec writes must parse as the
official Protobuf type with unknown fields refused, and what the official type
writes back (where every JSON number in ``data`` becomes a double) must decode
to the same value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format  # type: ignore[import-untyped]

from macp_sdk import a2a
from macp_sdk.canonical_json import ack_from_canonical_json, envelope_from_canonical_json

a2a_types = pytest.importorskip("a2a.types")

VECTOR_DIR = Path(__file__).parents[1] / "vectors" / "a2a"
VECTORS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(VECTOR_DIR.glob("a2a_*.json"))]


def _official(obj: Any, type_name: str) -> dict[str, Any]:
    """Parse *obj* as the official type (strict) and return what it writes."""
    message = json_format.ParseDict(obj, getattr(a2a_types, type_name)())
    out: dict[str, Any] = json_format.MessageToDict(message)
    return out


def _vectors(op: str) -> list[Any]:
    return [pytest.param(v, id=v["id"]) for v in VECTORS if v["op"] == op and "expected" in v]


@pytest.mark.parametrize("vector", _vectors("envelope_to_a2a_part"))
def test_envelope_part(vector: dict[str, Any]) -> None:
    env = envelope_from_canonical_json(vector["input"]["envelope"])
    part = a2a.envelope_to_a2a_part(env, **vector.get("params", {}))
    assert a2a.envelope_from_a2a_part(_official(part, "Part")) == env


@pytest.mark.parametrize("vector", _vectors("envelope_to_a2a_message"))
def test_envelope_message(vector: dict[str, Any]) -> None:
    env = envelope_from_canonical_json(vector["input"]["envelope"])
    msg = a2a.envelope_to_a2a_message(env, **vector.get("params", {}))
    back = _official(msg, "Message")
    assert back["role"] == "ROLE_USER"
    assert a2a.envelope_from_a2a_message(back) == env


@pytest.mark.parametrize("vector", _vectors("ack_to_a2a_message"))
def test_ack_message(vector: dict[str, Any]) -> None:
    ack = ack_from_canonical_json(vector["input"]["ack"])
    msg = a2a.ack_to_a2a_message(ack, **vector.get("params", {}))
    back = _official(msg, "Message")
    assert back["role"] == "ROLE_AGENT"
    assert a2a.ack_from_a2a_message(back) == ack


@pytest.mark.parametrize("vector", _vectors("envelope_to_a2a_artifact"))
def test_artifact(vector: dict[str, Any]) -> None:
    env = envelope_from_canonical_json(vector["input"]["envelope"])
    artifact = a2a.envelope_to_a2a_artifact(env, vector["input"]["sequence"])
    back = _official(artifact, "Artifact")
    # Struct numbers come back as doubles: EXT/sequence is now 2.0, not 2.
    assert isinstance(back["metadata"][a2a.MACP_A2A_EXTENSION_URI + "/sequence"], float)
    assert a2a.envelope_from_a2a_artifact(back) == (env, vector["input"]["sequence"])


@pytest.mark.parametrize("vector", _vectors("session_state_to_a2a"))
def test_task_and_status_event(vector: dict[str, Any]) -> None:
    task_state, metadata = a2a.session_state_to_a2a(vector["input"]["session_state"])
    task = {"id": "t", "contextId": "c", "status": {"state": task_state}, "metadata": metadata}
    event = {"taskId": "t", "contextId": "c", "status": {"state": task_state}, "metadata": metadata}
    for obj, type_name in ((task, "Task"), (event, "TaskStatusUpdateEvent")):
        back = _official(obj, type_name)
        assert back["status"]["state"] == task_state
        assert a2a.session_state_from_a2a(back) == vector["input"]["session_state"]


@pytest.mark.parametrize("vector", _vectors("macp_agent_extension"))
def test_agent_extension(vector: dict[str, Any]) -> None:
    ext = a2a.macp_agent_extension(**vector["params"])
    back = _official(ext, "AgentExtension")
    assert back["uri"] == a2a.MACP_A2A_EXTENSION_URI
    assert back["params"] == ext["params"]


def test_unknown_field_is_refused() -> None:
    # Guards the strictness the tests above rely on.
    with pytest.raises(json_format.ParseError):
        _official({"messageId": "m", "role": "ROLE_USER", "parts": [], "bogus": 1}, "Message")
