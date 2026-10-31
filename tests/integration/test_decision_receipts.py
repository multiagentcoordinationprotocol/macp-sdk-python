"""Runtime-issued decision receipts (RFC-MACP-0013 §13) against a live runtime.

The test names and assertions match macp-sdk-typescript's. Each test skips
unless its runtime advertises ``decision_receipt.get_decision_receipt``
(runtime ≥ 0.11.0) with the matching ``signed`` flag, so the suite stays
green against an older pinned image.

- The unsigned runtime is the shared ``MACP_RUNTIME_TARGET``.
- The signed runtime is ``MACP_TEST_RECEIPT_SIGNED_TARGET``: a runtime started
  with ``MACP_RECEIPT_SIGNING_KEY_PATH`` (or ``MACP_RECEIPT_SIGNING_KEY``),
  ``MACP_RECEIPT_SIGNING_KID`` and ``MACP_RUNTIME_ID``.
- ``MACP_EXPECT_RECEIPTS`` (``signed``, ``unsigned`` or ``signed,unsigned``)
  turns a missing capability or a ``signed`` mismatch for those legs into a
  failure instead of a skip, so CI can't silently stop exercising them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from macp_sdk import (
    RECEIPT_UNAVAILABLE,
    AuthConfig,
    DecisionSession,
    MacpClient,
    MacpTransportError,
    trusted_keys_from_jwks,
    verify_receipt,
)

from .conftest import RUNTIME_TARGET

pytestmark = pytest.mark.integration

EXPECTED = {
    leg.strip() for leg in os.environ.get("MACP_EXPECT_RECEIPTS", "").split(",") if leg.strip()
}


def _unavailable(leg: str, reason: str) -> None:
    if leg in EXPECTED:
        pytest.fail(f"MACP_EXPECT_RECEIPTS={leg}: {reason}")
    pytest.skip(reason)


def _receipt_client(target: str | None, leg: str) -> Iterator[MacpClient]:
    if not target:
        _unavailable(leg, "set MACP_TEST_RECEIPT_SIGNED_TARGET to a receipt-signing runtime")
        return
    client = MacpClient(
        target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("coordinator")
    )
    try:
        caps = client.initialize().capabilities
        if not caps.HasField("decision_receipt") or not caps.decision_receipt.get_decision_receipt:
            _unavailable(leg, f"runtime at {target} does not issue receipts (needs ≥ 0.11.0)")
        if caps.decision_receipt.signed != (leg == "signed"):
            _unavailable(leg, f"runtime at {target} is not a {leg} receipt runtime")
        yield client
    finally:
        client.close()


@pytest.fixture
def unsigned_client() -> Iterator[MacpClient]:
    yield from _receipt_client(RUNTIME_TARGET, "unsigned")


@pytest.fixture
def signed_client() -> Iterator[MacpClient]:
    yield from _receipt_client(os.environ.get("MACP_TEST_RECEIPT_SIGNED_TARGET"), "signed")


@pytest.fixture
def any_receipt_client() -> Iterator[MacpClient]:
    """The unsigned runtime if it issues receipts, else the signed one."""
    for target in (RUNTIME_TARGET, os.environ.get("MACP_TEST_RECEIPT_SIGNED_TARGET")):
        if not target:
            continue
        client = MacpClient(
            target=target, allow_insecure=True, auth=AuthConfig.for_dev_agent("coordinator")
        )
        try:
            caps = client.initialize().capabilities
            issues = (
                caps.HasField("decision_receipt") and caps.decision_receipt.get_decision_receipt
            )
            if issues:
                yield client
                return
        finally:
            client.close()
    if EXPECTED:
        pytest.fail(
            f"MACP_EXPECT_RECEIPTS={','.join(sorted(EXPECTED))}: no runtime issues receipts"
        )
    pytest.skip("no runtime issues decision receipts (needs ≥ 0.11.0)")


def _resolved_session(client: MacpClient) -> DecisionSession:
    session = DecisionSession(client, auth=AuthConfig.for_dev_agent("coordinator"))
    assert session.start(
        intent="receipt integration", participants=["coordinator", "alice"], ttl_ms=30_000
    ).ok
    assert session.propose("p1", "ship").ok
    assert session.vote("p1", "APPROVE", sender="alice", auth=AuthConfig.for_dev_agent("alice")).ok
    assert session.commit(action="decision.approved", authority_scope="test", reason="approved").ok
    return session


def _assert_body(receipt: dict[str, object], session: DecisionSession) -> None:
    assert receipt["session_id"] == session.session_id
    assert receipt["mode"] == "macp.mode.decision.v1"
    commitment = receipt["commitment"]
    assert isinstance(commitment, dict)
    assert commitment["action"] == "decision.approved"


def test_receipt_signed_runtime_attested(signed_client: MacpClient) -> None:
    session = _resolved_session(signed_client)

    first = signed_client.get_decision_receipt(session.session_id)
    # Issued once, served verbatim thereafter (§13 item 6).
    second = signed_client.get_decision_receipt(session.session_id)
    assert second.raw == first.raw
    _assert_body(first.receipt, session)
    _assert_body(second.receipt, session)

    assert first.receipt["attestations"], first.receipt
    keys = signed_client.get_receipt_keys()
    assert keys["keys"], keys
    result = verify_receipt(first.receipt, trusted_keys_from_jwks(keys))
    assert result["integrity"] == "match", result
    assert "valid" in result["attestations"], result
    assert result["verdict"] == "attested", result

    # Without trusting the runtime's key the receipt still proves integrity only.
    assert verify_receipt(first.receipt, {})["verdict"] == "integrity_only"


def test_receipt_unsigned_runtime_integrity_only(unsigned_client: MacpClient) -> None:
    session = _resolved_session(unsigned_client)

    first = unsigned_client.get_decision_receipt(session.session_id)
    second = unsigned_client.get_decision_receipt(session.session_id)
    assert second.raw == first.raw
    _assert_body(first.receipt, session)
    _assert_body(second.receipt, session)
    assert first.receipt["attestations"] == []

    # An unsigned runtime still publishes retired keys (spec #235); this leg
    # sets no MACP_RECEIPT_PUBLIC_KEYS_PATH, so the set is empty.
    keys = unsigned_client.get_receipt_keys()
    assert keys == {"keys": []}
    assert verify_receipt(first.receipt, trusted_keys_from_jwks(keys)) == {
        "integrity": "match",
        "attestations": [],
        "verdict": "integrity_only",
    }


def test_receipt_open_session_unavailable(any_receipt_client: MacpClient) -> None:
    session = DecisionSession(any_receipt_client, auth=AuthConfig.for_dev_agent("coordinator"))
    assert session.start(
        intent="receipt open", participants=["coordinator", "alice"], ttl_ms=30_000
    ).ok

    with pytest.raises(MacpTransportError) as raised:
        any_receipt_client.get_decision_receipt(session.session_id)
    assert raised.value.macp_code == RECEIPT_UNAVAILABLE
