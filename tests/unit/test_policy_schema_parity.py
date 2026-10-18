"""Policy-builder value domains vs the canonical rule schemas.

The SDK analogue of the runtime's ``enum_lists_match_the_canonical_schemas``:
every enum and bound the builders enforce must equal the one in spec
``schemas/json/policy/*-rules.schema.json``. Needs a spec checkout, so it
runs only when ``MACP_POLICY_SCHEMAS_DIR`` points at that directory (the
conformance-fixtures workflow sets it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from macp_sdk import policy

_DIR = os.environ.get("MACP_POLICY_SCHEMAS_DIR")

pytestmark = pytest.mark.skipif(
    not _DIR, reason="set MACP_POLICY_SCHEMAS_DIR to the spec's schemas/json/policy directory"
)

_MODES = ("decision", "quorum", "proposal", "task", "handoff")


def _schema(mode: str) -> dict[str, Any]:
    assert _DIR is not None
    return json.loads((Path(_DIR) / f"{mode}-rules.schema.json").read_text())


def _prop(schema: dict[str, Any], *path: str) -> dict[str, Any]:
    node = schema
    for name in path:
        node = node["properties"][name]
    return node


@pytest.mark.parametrize("mode", _MODES)
def test_commitment_authorities(mode: str):
    node = _prop(_schema(mode), "commitment", "authority")
    assert set(node["enum"]) == policy.COMMITMENT_AUTHORITIES


@pytest.mark.parametrize("mode", _MODES)
def test_extension_and_annotation_key_patterns(mode: str):
    patterns = set(_schema(mode)["patternProperties"])
    assert policy._EXTENSION_KEY_RE.pattern in patterns
    assert policy._ANNOTATION_KEY_RE.pattern in patterns


def test_decision_enums_and_bounds():
    s = _schema("decision")
    assert set(_prop(s, "voting", "algorithm")["enum"]) == policy._DECISION_ALGORITHMS
    assert set(_prop(s, "voting", "quorum", "type")["enum"]) == policy._VOTING_QUORUM_TYPES
    assert _prop(s, "voting", "quorum", "value")["minimum"] == 0
    oh = _prop(s, "objection_handling")
    assert set(oh["properties"]["critical_objection_action"]["enum"]) == (
        policy.CRITICAL_OBJECTION_ACTIONS
    )
    assert oh["properties"]["veto_threshold"] == {
        **oh["properties"]["veto_threshold"],
        "type": "integer",
        "minimum": 1,
    }
    mc = _prop(s, "evaluation", "minimum_confidence")
    assert (mc["minimum"], mc["maximum"]) == (0, 1)


def test_quorum_enums_and_bounds():
    s = _schema("quorum")
    assert set(_prop(s, "threshold", "type")["enum"]) == policy._QUORUM_THRESHOLD_TYPES
    value = _prop(s, "threshold", "value")
    assert value["type"] == "integer" and value["exclusiveMinimum"] == 0
    assert set(_prop(s, "abstention", "interpretation")["enum"]) == (
        policy.ABSTENTION_INTERPRETATIONS
    )


def test_proposal_enums_and_bounds():
    s = _schema("proposal")
    assert set(_prop(s, "acceptance", "criterion")["enum"]) == policy.PROPOSAL_ACCEPTANCE_CRITERIA
    rounds = _prop(s, "counter_proposal", "max_rounds")
    assert rounds["type"] == "integer" and rounds["minimum"] == 0


def test_handoff_bounds():
    timeout = _prop(_schema("handoff"), "acceptance", "implicit_accept_timeout_ms")
    assert timeout["type"] == "integer" and timeout["minimum"] == 0


@pytest.mark.parametrize("mode", _MODES)
def test_every_section_the_builder_emits_is_declared(mode: str):
    """A builder must never emit a key the closed schema would refuse."""
    build = getattr(policy, f"build_{mode}_policy")
    emitted = json.loads(build("p", "d").rules)
    declared = _schema(mode)["properties"]
    for section, body in emitted.items():
        assert section in declared, f"{mode}: undeclared section {section!r}"
        if isinstance(body, dict):
            inner = set(declared[section].get("properties", {}))
            assert set(body) <= inner, f"{mode}.{section}: {set(body) - inner}"
