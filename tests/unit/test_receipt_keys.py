"""`trusted_keys_from_jwks`: a ``GetReceiptKeys`` JWK Set to `verify_receipt` keys.

Test names match macp-sdk-typescript's (RFC-MACP-0013 §13, Wave 5 S1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from macp_sdk import RECEIPT_UNAVAILABLE, trusted_keys_from_jwks, verify_receipt

#: The spec's examples/discovery/receipt_keys.json, vendored byte-identical
#: (``make verify-fixtures`` checks it).
KEY_SET_PATH = Path(__file__).parents[1] / "fixtures" / "receipt_keys.json"
#: The Appendix A.5 example key, as pinned by the receipt vectors.
EXAMPLE_KID = "example-1"
EXAMPLE_X = "TDVzkxaSf6_Oxu7mmn5TbCkF80s8Ulh33qKQsi0gnZM"
RECEIPT_VECTORS = Path(__file__).parents[1] / "vectors" / "receipt"
#: The key-set schema in a sibling spec checkout, if there is one.
SPEC_KEY_SCHEMA = (
    Path(__file__).parents[3]
    / "multiagentcoordinationprotocol"
    / "schemas"
    / "json"
    / "macp-receipt-keys.schema.json"
)


def _key(**changes: Any) -> dict[str, Any]:
    key: dict[str, Any] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": "k1",
        "alg": "EdDSA",
        "use": "sig",
        "x": EXAMPLE_X,
    }
    for name, value in changes.items():
        if value is None:
            key.pop(name, None)
        else:
            key[name] = value
    return key


def test_trusted_keys_from_jwks_example_key_set() -> None:
    jwks = json.loads(KEY_SET_PATH.read_text())
    trusted = trusted_keys_from_jwks(jwks)
    assert trusted == {EXAMPLE_KID: EXAMPLE_X}

    # The runtime-shaped vector (§13) is signed with that key: the map is
    # exactly what verify_receipt takes.
    vector = json.loads((RECEIPT_VECTORS / "receipt_010_wire_match.json").read_text())
    assert vector["trusted_keys"] == trusted
    assert verify_receipt(vector["receipt"], trusted)["verdict"] == "attested"


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(_key(kty="EC", crv="P-256"), id="not-okp"),
        pytest.param(_key(kty=None), id="no-kty"),
        pytest.param(_key(crv="X25519"), id="okp-not-ed25519"),
        pytest.param(_key(crv=None), id="no-crv"),
        pytest.param(_key(kid=None), id="no-kid"),
        pytest.param(_key(kid=""), id="empty-kid"),
        pytest.param(_key(kid=7), id="kid-not-string"),
        pytest.param(_key(x=None), id="no-x"),
        pytest.param(_key(x=""), id="empty-x"),
        pytest.param(_key(x=["x"]), id="x-not-string"),
        pytest.param(_key(alg="ES256"), id="alg-not-eddsa"),
        pytest.param(_key(use="enc"), id="use-not-sig"),
        pytest.param({**_key(), "alg": None}, id="alg-null"),
        pytest.param({**_key(), "use": None}, id="use-null"),
        pytest.param({**_key(), "d": None}, id="private-member-null"),
        *(
            pytest.param(_key(**{member: "AAAA"}), id=f"private-{member}")
            for member in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")
        ),
        pytest.param("k1", id="not-an-object"),
        pytest.param(None, id="null"),
    ],
)
def test_trusted_keys_from_jwks_skips_unusable_keys(key: object) -> None:
    good = _key(kid="good")
    assert trusted_keys_from_jwks({"keys": [key, good]}) == {"good": EXAMPLE_X}


def test_trusted_keys_from_jwks_keeps_keys_without_alg_or_use() -> None:
    assert trusted_keys_from_jwks({"keys": [_key(alg=None, use=None)]}) == {"k1": EXAMPLE_X}


def test_trusted_keys_from_jwks_drops_duplicate_kid() -> None:
    other_x = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    jwks = {
        "keys": [
            _key(kid="dup"),
            _key(kid="solo"),
            _key(kid="dup", x=other_x),
            _key(kid="dup"),
        ]
    }
    assert trusted_keys_from_jwks(jwks) == {"solo": EXAMPLE_X}


def test_trusted_keys_from_jwks_duplicate_of_an_unusable_key_still_drops_the_kid() -> None:
    # A second key under the same kid makes the kid ambiguous even when that
    # second key is not one we would trust.
    for second in (_key(kid="dup", kty="EC", crv="P-256"), _key(kid="dup", d="AAAA")):
        assert trusted_keys_from_jwks({"keys": [_key(kid="dup"), second]}) == {}
    # A non-string kid claims nothing, so it makes no kid ambiguous.
    jwks = {"keys": [_key(kid="dup"), {**_key(), "kid": 7}, {**_key(), "kid": ["dup"]}]}
    assert trusted_keys_from_jwks(jwks) == {"dup": EXAMPLE_X}


def test_trusted_keys_from_jwks_proto_named_kid_is_an_ordinary_key() -> None:
    # TS stores it as an own property; in Python it is just a dict key.
    assert trusted_keys_from_jwks({"keys": [_key(kid="__proto__")]}) == {"__proto__": EXAMPLE_X}


@pytest.mark.parametrize(
    "jwks",
    [None, [], "x", 3, {}, {"keys": 3}, {"keys": None}, {"keys": {"k1": _key()}}],
    ids=["none", "list", "str", "int", "no-keys", "keys-int", "keys-null", "keys-object"],
)
def test_trusted_keys_from_jwks_malformed_input_is_empty(jwks: object) -> None:
    assert trusted_keys_from_jwks(jwks) == {}


def test_trusted_keys_from_jwks_empty_set_is_empty() -> None:
    # What a runtime with no signing key publishes (§13 item 8).
    assert trusted_keys_from_jwks({"keys": []}) == {}


def test_example_key_set_validates_against_the_spec_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    if not SPEC_KEY_SCHEMA.is_file():
        pytest.skip("spec repo checkout not found next to this repo")
    schema = json.loads(SPEC_KEY_SCHEMA.read_text())
    jsonschema.Draft202012Validator(schema).validate(json.loads(KEY_SET_PATH.read_text()))


def test_receipt_unavailable_constant() -> None:
    assert RECEIPT_UNAVAILABLE == "RECEIPT_UNAVAILABLE"
