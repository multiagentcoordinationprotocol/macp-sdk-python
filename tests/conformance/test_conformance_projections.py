"""Conformance tests: replay fixture messages through SDK projections.

These tests validate that the SDK's local projections correctly track state
when fed the same messages the runtime would accept. Reject-path messages
(expect=="reject") are skipped since rejection is enforced by the runtime,
not by the SDK projection layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from google.protobuf import symbol_database
from google.protobuf.descriptor import FieldDescriptor
from macp.v1 import envelope_pb2

from macp_sdk.envelope import new_message_id, now_unix_ms, serialize_message
from macp_sdk.handoff import HandoffProjection
from macp_sdk.projections import DecisionProjection
from macp_sdk.proposal import ProposalProjection
from macp_sdk.proto_registry import CORE_MAP, MODE_MAP, ProtoRegistry
from macp_sdk.quorum import QuorumProjection
from macp_sdk.task import TaskProjection

FIXTURES_DIR = Path(__file__).parent

# Canonical fixtures (spec PRs #49-#52) identify payloads by fully-qualified
# protobuf message name (e.g. ``macp.modes.decision.v1.ProposalPayload``,
# ``macp.v1.CommitmentPayload``). The pattern is defined by the canonical
# ``schema.json`` and mirrored by the format-guard test below.
PAYLOAD_TYPE_RE = re.compile(
    r"^(macp\.v1\.[A-Za-z]+|macp\.modes\.[a-z_]+\.v\d+\.[A-Za-z]+Payload)$"
)


def _build_payload_builders() -> dict[str, type]:
    """Derive fully-qualified-name → pb2 class from the SDK's own registry.

    Using ``proto_registry.CORE_MAP``/``MODE_MAP`` as the single source of
    truth means the harness cannot drift from the type names the SDK actually
    encodes/decodes. Instantiating :class:`ProtoRegistry` registers every
    ``_pb2`` module in the descriptor pool so ``GetSymbol`` resolves.
    """
    ProtoRegistry()  # side effect: _ensure_pb2_imports registers descriptors
    sym = symbol_database.Default()
    builders: dict[str, type] = {}
    type_names = list(CORE_MAP.values())
    for mode_map in MODE_MAP.values():
        type_names.extend(mode_map.values())
    for type_name in type_names:
        if type_name == "__json__":  # legacy JSON escape hatch (pre-proto modes)
            continue
        builders[type_name] = sym.GetSymbol(type_name)
    return builders


PAYLOAD_BUILDERS: dict[str, type] = _build_payload_builders()

MODE_PROJECTIONS: dict[str, type] = {
    "macp.mode.decision.v1": DecisionProjection,
    "macp.mode.proposal.v1": ProposalProjection,
    "macp.mode.task.v1": TaskProjection,
    "macp.mode.handoff.v1": HandoffProjection,
    "macp.mode.quorum.v1": QuorumProjection,
}


def _build_payload(payload_type: str, payload_data: dict) -> bytes:
    cls = PAYLOAD_BUILDERS.get(payload_type)
    if cls is None:
        raise ValueError(f"Unknown payload_type: {payload_type}")
    fields = cls.DESCRIPTOR.fields_by_name
    # Build kwargs from the fixture payload, coercing JSON scalars to the proto
    # field types. Fixtures are JSON, so `bytes` proto fields (e.g.
    # HandoffContext.context, *_data/output) arrive as plain strings and must be
    # UTF-8 encoded before construction. Repeated fields are skipped (the
    # projections under test don't assert on them).
    filtered = {}
    for k, v in payload_data.items():
        if isinstance(v, list):
            continue
        field = fields.get(k)
        if field is not None and field.type == FieldDescriptor.TYPE_BYTES and isinstance(v, str):
            v = v.encode("utf-8")
        filtered[k] = v
    msg = cls(**filtered)
    return serialize_message(msg)


def _build_envelope(mode: str, msg: dict, session_id: str) -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(
        macp_version="1.0",
        mode=mode,
        message_type=msg["message_type"],
        message_id=new_message_id(),
        session_id=session_id,
        sender=msg["sender"],
        timestamp_unix_ms=now_unix_ms(),
        payload=_build_payload(msg["payload_type"], msg["payload"]),
    )


# Non-fixture JSON files that live alongside the fixtures and must not be
# replayed. ``schema.json`` is the canonical JSON Schema vendored by
# ``make sync-fixtures`` (spec PR #52); it has no ``mode`` and would crash the
# replay loop if loaded as a fixture.
_NON_FIXTURE_JSON = frozenset({"schema.json"})


def _load_fixtures():
    """Yield (fixture_name, fixture_data) for every JSON fixture.

    Skips non-fixture JSON (``schema.json``) and any file lacking a ``mode``
    key, so vendoring the canonical schema alongside the fixtures is safe.
    """
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        if path.name in _NON_FIXTURE_JSON:
            continue
        with open(path) as f:
            data = json.load(f)
        if "mode" not in data:
            continue
        yield path.stem, data


FIXTURES = list(_load_fixtures())
FIXTURE_IDS = [name for name, _ in FIXTURES]


@pytest.mark.conformance
@pytest.mark.parametrize("name,fixture", FIXTURES, ids=FIXTURE_IDS)
def test_projection_replay(name: str, fixture: dict):
    """Replay accepted messages through the projection and verify commitment state."""
    mode = fixture["mode"]
    projection_cls = MODE_PROJECTIONS.get(mode)
    if projection_cls is None:
        pytest.skip(
            f"No projection for mode {mode} — "
            "multi_round is an extension mode; SDK projection not yet implemented"
        )

    projection = projection_cls()
    session_id = "conformance-session"

    # Replay only accepted messages (rejection is runtime-side). Reject-path
    # fixtures still replay their accepted prefix, in lockstep with the
    # TypeScript harness.
    accepted_count = 0
    for msg in fixture["messages"]:
        if msg.get("expect") != "accept":
            continue
        envelope = _build_envelope(mode, msg, session_id)
        projection.apply_envelope(envelope)
        accepted_count += 1

    # Verify transcript was tracked
    assert len(projection.transcript) == accepted_count

    # Commitment presence is driven by the terminal state: Resolved ⇒ committed.
    expected_final = fixture.get("expected_final_state", "Open")
    if expected_final == "Resolved":
        assert projection.is_committed, f"Expected committed state for {name}"
        assert projection.commitment is not None
    else:
        assert not projection.is_committed, f"Expected non-committed state for {name}"

    # Verify every scalar field of expected_resolution (incl. outcome_positive).
    expected_res = fixture.get("expected_resolution") or {}
    if expected_res:
        assert projection.commitment is not None
        for key, val in expected_res.items():
            assert getattr(projection.commitment, key) == val, f"{name}: commitment.{key}"

    # Verify mode-state phase and recorded votes when the fixture specifies them.
    expected_mode_state = fixture.get("expected_mode_state") or {}
    if "phase" in expected_mode_state:
        assert projection.phase == expected_mode_state["phase"], f"{name}: phase"
    if "votes" in expected_mode_state:
        for proposal_id, by_sender in expected_mode_state["votes"].items():
            for sender, record in by_sender.items():
                assert projection.votes[proposal_id][sender].vote == record["vote"], (
                    f"{name}: vote {proposal_id}/{sender}"
                )


@pytest.mark.conformance
@pytest.mark.parametrize("name,fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_payload_types_are_fully_qualified(name: str, fixture: dict):
    """Format-guard: every ``payload_type`` uses the canonical fully-qualified
    protobuf name (mirrors the runtime harness + canonical ``schema.json``).

    This prevents drift back to the old shorthand (``decision.Proposal``)
    that the pre-0.5.0 harness used.
    """
    for msg in fixture["messages"]:
        payload_type = msg["payload_type"]
        assert PAYLOAD_TYPE_RE.match(payload_type), (
            f"{name}: payload_type {payload_type!r} is not fully qualified"
        )


@pytest.mark.conformance
def test_vendored_schema_matches_canonical_pattern():
    """The vendored ``schema.json`` (if present) must define the same
    fully-qualified payload_type pattern the harness enforces."""
    schema_path = FIXTURES_DIR / "schema.json"
    if not schema_path.exists():
        pytest.skip("schema.json not vendored (run `make sync-fixtures`)")
    with open(schema_path) as f:
        schema = json.load(f)
    pattern = schema["properties"]["messages"]["items"]["properties"]["payload_type"]["pattern"]
    assert pattern == PAYLOAD_TYPE_RE.pattern


def _strip_editorial_keys(fixture: dict) -> dict:
    """Drop ``_``-prefixed editorial annotation keys from a fixture copy.

    Some canonical fixtures carry editorial keys like ``_comment`` on
    individual messages, but the canonical ``schema.json`` declares
    ``additionalProperties: false`` on message objects — so a strict
    validation of the raw fixture fails on the annotation, not on any
    substantive drift. We keep byte-parity with the canonical pack (the
    vendored file is untouched) and validate the *meaningful* structure by
    ignoring these annotation-only keys. (Tracked upstream: the canonical
    schema should allow ``_``-prefixed annotations or the fixtures should drop
    them.)
    """
    clean = {k: v for k, v in fixture.items() if not k.startswith("_")}
    messages = clean.get("messages")
    if isinstance(messages, list):
        clean["messages"] = [
            {k: v for k, v in msg.items() if not k.startswith("_")}
            if isinstance(msg, dict)
            else msg
            for msg in messages
        ]
    return clean


@pytest.mark.conformance
@pytest.mark.parametrize("name,fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixtures_validate_against_vendored_schema(name: str, fixture: dict):
    """Each vendored fixture validates against the canonical ``schema.json``
    (ignoring ``_``-prefixed editorial annotation keys — see
    :func:`_strip_editorial_keys`).

    Uses ``jsonschema`` when available; skips cleanly when it is not.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = FIXTURES_DIR / "schema.json"
    if not schema_path.exists():
        pytest.skip("schema.json not vendored (run `make sync-fixtures`)")
    with open(schema_path) as f:
        schema = json.load(f)
    jsonschema.validate(instance=_strip_editorial_keys(fixture), schema=schema)
