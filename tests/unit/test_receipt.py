"""Decision receipts (RFC-MACP-0013 Appendix A) against the spec's vector pack.

The vectors live in ``tests/vectors/receipt/``, a byte-identical copy of the
spec repo's ``schemas/conformance/receipt/`` kept under ``make
verify-fixtures``. ``receipt_001_attested`` is the Appendix A.5 example
(``examples/discovery/decision_receipt.json``), so rebuilding it with the A.5
key is the round-trip check.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from macp.v1 import core_pb2

from macp_sdk import (
    RECEIPT_FORMAT,
    MacpSdkError,
    ReceiptSigner,
    build_receipt,
    commitment_from_projection,
    verify_receipt,
)
from macp_sdk.receipt import _jcs

VECTORS_DIR = Path(__file__).resolve().parents[1] / "vectors" / "receipt"
VECTORS = sorted(p for p in VECTORS_DIR.glob("*.json") if p.name != "vector-schema.json")
SEED = hashlib.sha256(b"macp-receipt example key").digest()
EXAMPLE_X = "TDVzkxaSf6_Oxu7mmn5TbCkF80s8Ulh33qKQsi0gnZM"
TRUSTED = {"example-1": EXAMPLE_X}


def _vector(name: str) -> dict[str, Any]:
    return json.loads((VECTORS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _rebuild(receipt: dict[str, Any], signer: ReceiptSigner | None) -> dict[str, Any]:
    wire = receipt.get("commitment_wire_b64")
    return dict(
        build_receipt(
            commitment_from_projection(receipt["commitment"]),
            session_id=receipt["session_id"],
            mode=receipt["mode"],
            issuer=receipt["issuer"],
            issued_at=receipt["issued_at"],
            signer=signer,
            semantics_rev=receipt.get("semantics_rev"),
            transcript=receipt.get("transcript"),
            commitment_wire=base64.b64decode(wire) if wire else None,
        )
    )


def _signed(**changes: Any) -> dict[str, Any]:
    """The A.5 example with ``changes`` applied, re-signed with the A.5 key."""
    receipt = copy.deepcopy(_vector("receipt_001_attested")["receipt"])
    receipt.update(changes)
    receipt["attestations"] = []
    from macp_sdk.receipt import _sign

    receipt["attestations"] = [_sign(receipt, ReceiptSigner("example-1", private_key=SEED))]
    return receipt


def test_vector_pack_is_complete() -> None:
    assert len(VECTORS) == 12


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector(path: Path) -> None:
    v = json.loads(path.read_text(encoding="utf-8"))
    assert verify_receipt(v["receipt"], v["trusted_keys"]) == v["expected"]


def test_example_key_matches_a5() -> None:
    public = ed25519.Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes_raw()
    assert base64.urlsafe_b64encode(public).rstrip(b"=").decode() == EXAMPLE_X


@pytest.mark.parametrize(
    "name", ["receipt_001_attested", "receipt_010_wire_match", "receipt_002_integrity_only"]
)
def test_round_trip_reproduces_vector(name: str) -> None:
    expected = _vector(name)["receipt"]
    signer = ReceiptSigner("example-1", private_key=SEED) if expected["attestations"] else None
    built = _rebuild(expected, signer)
    assert built == expected
    assert json.dumps(built) == json.dumps(expected)  # key order too
    assert verify_receipt(built, TRUSTED)["verdict"] == ("attested" if signer else "integrity_only")


def test_callback_signer_matches_seed_signer() -> None:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(SEED)
    expected = _vector("receipt_001_attested")["receipt"]
    assert _rebuild(expected, ReceiptSigner("example-1", sign=key.sign)) == expected


def test_build_from_payload_without_supersedes() -> None:
    payload = core_pb2.CommitmentPayload(commitment_id="c1", action="a", outcome_positive=False)
    receipt = build_receipt(payload, session_id="s", mode="m", issuer="i", issued_at="t")
    assert receipt["receipt_format"] == RECEIPT_FORMAT
    assert "supersedes" not in receipt["commitment"]
    assert receipt["commitment"]["policy_version"] == ""
    assert receipt["attestations"] == []
    assert verify_receipt(receipt, {}) == {
        "integrity": "match",
        "attestations": [],
        "verdict": "integrity_only",
    }


# --- ReceiptSigner / build_receipt errors -----------------------------------


@pytest.mark.parametrize("kwargs", [{}, {"private_key": SEED, "sign": lambda b: b}])
def test_signer_needs_exactly_one_key_source(kwargs: dict[str, Any]) -> None:
    with pytest.raises(MacpSdkError, match="exactly one"):
        ReceiptSigner("k", **kwargs)


def _payload() -> core_pb2.CommitmentPayload:
    return commitment_from_projection(_vector("receipt_001_attested")["receipt"]["commitment"])


BASE = {"session_id": "s", "mode": "m", "issuer": "i", "issued_at": "t"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"session_id": ""}, "session_id"),
        ({"issuer": 3}, "issuer"),
        ({"semantics_rev": {"implementation": "x", "rev": -1}}, "semantics_rev.rev"),
        ({"semantics_rev": {"implementation": "x", "rev": True}}, "semantics_rev.rev"),
        ({"semantics_rev": {"rev": 1}}, "semantics_rev.implementation"),
        ({"transcript": {"digest_alg": "sha-256"}}, "transcript.digest"),
        (
            {"transcript": {"digest_alg": "a", "digest": "d", "message_count": 2**53}},
            "transcript.message_count",
        ),
        ({"commitment_wire": b"\x0a\x01x"}, "commitment_wire"),
        ({"commitment_wire": "not bytes"}, "commitment_wire"),
        ({"signer": ReceiptSigner("k", private_key=b"short")}, "32-byte"),
        ({"signer": ReceiptSigner("k", sign=lambda b: b"x")}, "64-byte"),
        ({"signer": ReceiptSigner("", private_key=SEED)}, "signer.kid"),
    ],
)
def test_build_rejects(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(MacpSdkError, match=message):
        build_receipt(_payload(), **{**BASE, **changes})


def test_build_records_wire_and_transcript() -> None:
    payload = _payload()
    receipt = build_receipt(
        payload,
        **BASE,
        commitment_wire=payload.SerializeToString(),
        transcript={"digest_alg": "a", "digest": "d"},
    )
    assert base64.b64decode(receipt["commitment_wire_b64"]) == payload.SerializeToString()
    assert receipt["transcript"] == {"digest_alg": "a", "digest": "d"}
    assert verify_receipt(receipt, {})["integrity"] == "match"


# --- commitment_from_projection ---------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(extra="x"),
        lambda c: c.pop("reason"),
        lambda c: c.update(reason=1),
        lambda c: c.update(outcome_positive=1),
        lambda c: c.update(supersedes="x"),
        lambda c: c.update(supersedes={"session_id": "s"}),
        lambda c: c.update(supersedes={"session_id": "s", "commitment_hash": 1}),
        lambda c: c.update(supersedes={"session_id": "s", "commitment_hash": "h", "x": "y"}),
    ],
)
def test_malformed_commitment(mutate: Any) -> None:
    receipt = copy.deepcopy(_vector("receipt_001_attested")["receipt"])
    mutate(receipt["commitment"])
    with pytest.raises(MacpSdkError, match="§3 projection"):
        commitment_from_projection(receipt["commitment"])
    assert verify_receipt(receipt, TRUSTED) == {
        "integrity": "cannot_verify",
        "attestations": [],
        "verdict": "unverified",
    }


def test_commitment_from_projection_rejects_non_mapping() -> None:
    with pytest.raises(MacpSdkError):
        commitment_from_projection([])  # type: ignore[arg-type]


# --- verify_receipt edges ----------------------------------------------------


@pytest.mark.parametrize("receipt", [None, [], "x", {"receipt_format": "macp-receipt/2"}])
def test_refused(receipt: Any) -> None:
    assert verify_receipt(receipt, TRUSTED) == {
        "integrity": None,
        "attestations": [],
        "verdict": "refused",
    }


@pytest.mark.parametrize(
    ("changes", "integrity"),
    [
        ({"commitment": "x"}, "cannot_verify"),
        ({"commitment_hash": 7}, "mismatch"),
        ({"commitment_wire_b64": 7}, "cannot_verify"),
        ({"commitment_wire_b64": "Zm9"}, "cannot_verify"),
        ({"commitment_wire_b64": "CgJjMg=="}, "mismatch"),  # parses, different hash
        ({"commitment_wire_b64": "AA=="}, "cannot_verify"),  # field number 0
    ],
)
def test_integrity_edges(changes: dict[str, Any], integrity: str) -> None:
    receipt = copy.deepcopy(_vector("receipt_001_attested")["receipt"])
    receipt.update(changes)
    assert verify_receipt(receipt, TRUSTED) == {
        "integrity": integrity,
        "attestations": [],
        "verdict": "unverified",
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


EXAMPLE_JWS = _vector("receipt_001_attested")["receipt"]["attestations"][0]["jws"]
HDR, _, SIG = EXAMPLE_JWS.split(".")
NO_KID = _b64url(b'{"alg":"EdDSA"}')
INT_KID = _b64url(b'{"alg":"EdDSA","kid":7}')


@pytest.mark.parametrize(
    ("attestation", "outcome"),
    [
        ("not a dict", "unsupported"),
        ({"type": "other"}, "unsupported"),
        ({"type": "jws"}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}.{SIG}"}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}.x.{SIG}"}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}..{SIG}="}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}.."}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}..{SIG[:-1]}"}, "invalid"),  # length % 4 == 1
        ({"type": "jws", "jws": f"{_b64url(b'not json')}..{SIG}"}, "invalid"),
        ({"type": "jws", "jws": f"{_b64url(bytes([0xFF]))}..{SIG}"}, "invalid"),
        ({"type": "jws", "jws": f"{_b64url(b'[]')}..{SIG}"}, "unsupported"),
        ({"type": "jws", "jws": f"{NO_KID}..{SIG}"}, "unknown_key"),
        (
            {"type": "jws", "jws": f"{INT_KID}..{SIG}"},
            "unknown_key",
        ),
        ({"type": "jws", "jws": f"{HDR}..{_b64url(b'x' * 63)}"}, "invalid"),
        ({"type": "jws", "jws": f"{HDR}..{_b64url(b'x' * 64)}"}, "invalid"),
    ],
)
def test_attestation_edges(attestation: Any, outcome: str) -> None:
    receipt = copy.deepcopy(_vector("receipt_001_attested")["receipt"])
    receipt["attestations"] = [attestation]
    assert verify_receipt(receipt, TRUSTED) == {
        "integrity": "match",
        "attestations": [outcome],
        "verdict": "integrity_only",
    }


@pytest.mark.parametrize("key", [7, "", "AAAA", EXAMPLE_X + "="])
def test_bad_trusted_key_is_invalid(key: Any) -> None:
    receipt = _vector("receipt_001_attested")["receipt"]
    assert verify_receipt(receipt, {"example-1": key})["attestations"] == ["invalid"]


def test_non_mapping_trusted_keys_trust_nothing() -> None:
    receipt = _vector("receipt_001_attested")["receipt"]
    assert verify_receipt(receipt, None)["attestations"] == ["unknown_key"]  # type: ignore[arg-type]


def test_non_list_attestations() -> None:
    receipt = {**_vector("receipt_001_attested")["receipt"], "attestations": "x"}
    assert verify_receipt(receipt, TRUSTED)["verdict"] == "integrity_only"


@pytest.mark.parametrize("extra", [1.5, 2**53, {1: "x"}, object()])
def test_body_without_jcs_form_cannot_be_attested(extra: Any) -> None:
    receipt = {**_vector("receipt_008_vendor_binds_other")["receipt"], "x_extra": extra}
    receipt["attestations"] = [*receipt["attestations"], {"type": "jws", "jws": EXAMPLE_JWS}]
    assert verify_receipt(receipt, TRUSTED)["attestations"] == ["unsupported", "invalid"]


def test_extra_body_members_are_signed() -> None:
    receipt = _signed(x_note=[None, True, "é", -3, {"\U0001f600": 1, "ﬁ": 2}])
    assert verify_receipt(receipt, TRUSTED)["verdict"] == "attested"
    receipt["x_note"][3] = -4
    assert verify_receipt(receipt, TRUSTED)["attestations"] == ["invalid"]


def test_jcs_sorts_keys_by_utf16_code_units() -> None:
    # U+1F600 is D83D DE00 in UTF-16, so it sorts before U+FB01.
    assert _jcs({"ﬁ": 1, "\U0001f600": 2, "a": None}) == '{"a":null,"\U0001f600":2,"ﬁ":1}'
