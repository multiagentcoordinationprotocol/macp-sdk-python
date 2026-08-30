"""Replay the RFC-MACP-0013 canonical-commitment-hash spec vectors.

The vector pack lives in ``tests/vectors/cmt-hash/`` -- a manual snapshot of
the spec repo's ``schemas/conformance/cmt-hash/`` directory, kept outside
``tests/conformance/`` on purpose. See ``tests/vectors/cmt-hash/SOURCE.md``
for the full rationale: ``tests/conformance/``'s zero-drift gate
(``make verify-fixtures``) diffs a *flat* ``tests/conformance/*.json`` glob
against the spec repo's flat ``schemas/conformance/*.json`` -- the spec
repo's ``cmt-hash`` vectors live in a subdirectory there, so they can't be
placed flat in ``tests/conformance/`` (would be flagged EXTRA and fail the
gate) or in a ``tests/conformance/cmt-hash/`` subdirectory (invisible to the
gate's non-recursive glob, so drift would never be caught). This module is
the thing that actually exercises them, and it lives under ``tests/unit/``
so the CI command (``pytest tests/unit/ -v --cov``) collects it.

Unlike ``test_commitment_hash.py`` (Phase 1), which hardcodes payload
literals inline for isolation, this module builds every ``CommitmentPayload``
from the on-disk vector JSON -- the point is proving the vector pack itself
round-trips through the SDK, not proving a Python literal matches a string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from macp.v1 import core_pb2

from macp_sdk.commitment_hash import LABEL, canonical_projection, commitment_hash

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "cmt-hash"
SCHEMA_PATH = VECTORS_DIR / "vector-schema.json"

_EXCLUDED_NAMES = {"vector-schema.json", "SOURCE.md"}


def _discover_vector_files() -> list[Path]:
    return [
        p
        for p in sorted(VECTORS_DIR.glob("cmt_hash_*.json"))
        if p.name not in _EXCLUDED_NAMES
    ]


VECTOR_FILES = _discover_vector_files()


def _load_vector(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_payload(payload_data: dict[str, Any]) -> core_pb2.CommitmentPayload:
    """Build a ``CommitmentPayload`` from a vector's ``payload`` object.

    Critical: when ``supersedes`` is absent from the vector JSON, the kwarg
    is omitted entirely (never passed as ``None`` or ``{}``) so the field is
    left genuinely unset on the proto message -- this is what vector 004
    ("empty but present supersedes") is distinguishing itself from.
    """
    kwargs: dict[str, Any] = {
        "commitment_id": payload_data["commitment_id"],
        "action": payload_data["action"],
        "authority_scope": payload_data["authority_scope"],
        "reason": payload_data["reason"],
        "mode_version": payload_data["mode_version"],
        "policy_version": payload_data["policy_version"],
        "configuration_version": payload_data["configuration_version"],
        "outcome_positive": payload_data["outcome_positive"],
    }
    if "supersedes" in payload_data:
        supersedes = payload_data["supersedes"]
        kwargs["supersedes"] = core_pb2.CommitmentRef(
            session_id=supersedes["session_id"],
            commitment_hash=supersedes["commitment_hash"],
        )
    return core_pb2.CommitmentPayload(**kwargs)


@pytest.mark.parametrize(
    "vector_path",
    VECTOR_FILES,
    ids=[p.stem for p in VECTOR_FILES],
)
def test_commitment_hash_vector(vector_path: Path) -> None:
    vector = _load_vector(vector_path)
    payload = _build_payload(vector["payload"])
    jcs_bytes = canonical_projection(payload)
    preimage = LABEL.encode("ascii") + b":" + jcs_bytes

    assert jcs_bytes.hex() == vector["jcs_utf8_hex"]
    assert preimage.hex() == vector["preimage_utf8_hex"]
    assert commitment_hash(payload) == vector["hash"]


@pytest.mark.parametrize(
    "vector_path",
    VECTOR_FILES,
    ids=[p.stem for p in VECTOR_FILES],
)
def test_vector_matches_schema(vector_path: Path) -> None:
    """Each vector validates against the vendored ``vector-schema.json``,
    mirroring ``tests/conformance/test_conformance_projections.py``'s
    ``test_fixtures_validate_against_vendored_schema``."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=_load_vector(vector_path), schema=schema)


def test_must_differ_from_vectors_produce_distinct_hashes() -> None:
    """RFC-MACP-0013 Section 11's explicit sole vector-level check: any
    vector declaring ``must_differ_from`` MUST hash to something different
    from the referenced vector (e.g. an unset ``supersedes`` vs. one present
    with empty sub-fields must not collide).

    Written generically over whichever vectors declare ``must_differ_from``
    rather than hardcoding "003 vs 004" so this stays correct if more such
    vectors are added later.
    """
    vectors_by_name = {
        vector_path.stem: _load_vector(vector_path) for vector_path in VECTOR_FILES
    }

    pairs_checked = 0
    for name, vector in vectors_by_name.items():
        other_name = vector.get("must_differ_from")
        if other_name is None:
            continue
        assert other_name in vectors_by_name, (
            f"{name} declares must_differ_from={other_name!r}, "
            "but no such vector was discovered"
        )
        other_vector = vectors_by_name[other_name]

        hash_a = commitment_hash(_build_payload(vector["payload"]))
        hash_b = commitment_hash(_build_payload(other_vector["payload"]))
        assert hash_a != hash_b, (
            f"{name} and {other_name} must hash differently "
            "(must_differ_from), but both produced the same commitment hash"
        )
        pairs_checked += 1

    assert pairs_checked > 0, (
        "expected at least one vector declaring must_differ_from "
        f"in {VECTORS_DIR}"
    )
