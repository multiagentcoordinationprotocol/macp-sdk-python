"""GetDecisionReceipt / GetReceiptKeys (RFC-MACP-0013 §13): gate, parsing, errors.

The ``test_<shared name>`` tests match macp-sdk-typescript's names.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from macp.v1 import core_pb2

from macp_sdk import (
    RECEIPT_UNAVAILABLE,
    AsyncMacpClient,
    AuthConfig,
    DecisionReceiptResult,
    MacpSdkError,
    MacpTransportError,
    trusted_keys_from_jwks,
    verify_receipt,
)
from tests.conftest import FakeRpcError, client_with_stub

FIXTURES = Path(__file__).parents[1]
KEY_SET_JSON = (FIXTURES / "fixtures" / "receipt_keys.json").read_text()
VECTOR = json.loads((FIXTURES / "vectors" / "receipt" / "receipt_010_wire_match.json").read_text())
# Deliberately not json.dumps' default layout, with surrounding whitespace:
# ``raw`` must come back verbatim.
RECEIPT_JSON = "\n " + json.dumps(VECTOR["receipt"], indent=1, sort_keys=True) + " \n"


def _caps(*, receipts: bool, signed: bool = False) -> core_pb2.Capabilities:
    return core_pb2.Capabilities(
        decision_receipt=core_pb2.DecisionReceiptCapability(
            get_decision_receipt=receipts, signed=signed
        )
    )


def _ready_client(**kwargs: Any) -> tuple[Any, MagicMock]:
    client, stub = client_with_stub(**kwargs)
    stub.Initialize.return_value = core_pb2.InitializeResponse(
        capabilities=_caps(receipts=True, signed=True)
    )
    stub.GetDecisionReceipt.return_value = core_pb2.GetDecisionReceiptResponse(
        receipt_json=RECEIPT_JSON
    )
    stub.GetReceiptKeys.return_value = core_pb2.GetReceiptKeysResponse(jwks_json=KEY_SET_JSON)
    return client, stub


def _rpc(client: Any, name: str) -> Any:
    return client.get_decision_receipt("s1") if name == "receipt" else client.get_receipt_keys()


# ---------------------------------------------------------------- shared names


def test_get_decision_receipt_requires_capability() -> None:
    _assert_refused_without_capability("receipt")


def test_get_receipt_keys_requires_capability() -> None:
    _assert_refused_without_capability("keys")


def _assert_refused_without_capability(rpc: str) -> None:
    client, stub = client_with_stub()
    stub.Initialize.return_value = core_pb2.InitializeResponse(
        capabilities=_caps(receipts=False, signed=True)
    )

    with pytest.raises(MacpSdkError, match=r"RFC-MACP-0013 §13") as excinfo:
        _rpc(client, rpc)

    assert "decision_receipt.get_decision_receipt" in str(excinfo.value)
    assert not isinstance(excinfo.value, MacpTransportError)
    stub.GetDecisionReceipt.assert_not_called()
    stub.GetReceiptKeys.assert_not_called()


def test_get_decision_receipt_returns_parsed_and_raw() -> None:
    client, stub = _ready_client()

    result = client.get_decision_receipt("s1")

    assert isinstance(result, DecisionReceiptResult)
    assert result.raw == RECEIPT_JSON
    assert result.receipt == VECTOR["receipt"]
    request = stub.GetDecisionReceipt.call_args.args[0]
    assert request == core_pb2.GetDecisionReceiptRequest(session_id="s1")
    # End to end: the fetched receipt verifies against the fetched keys.
    trusted = trusted_keys_from_jwks(client.get_receipt_keys())
    assert verify_receipt(result.receipt, trusted)["verdict"] == "attested"


@pytest.mark.parametrize(
    "receipt_json",
    ["", "not json", "{", "[]", '"receipt"', "3", "null", '{"a": NaN}', '{"a": -Infinity}'],
    ids=["empty", "text", "truncated", "array", "string", "number", "null", "nan", "neg-inf"],
)
def test_get_decision_receipt_rejects_non_json(receipt_json: str) -> None:
    client, stub = _ready_client()
    stub.GetDecisionReceipt.return_value = core_pb2.GetDecisionReceiptResponse(
        receipt_json=receipt_json
    )

    with pytest.raises(MacpSdkError, match="GetDecisionReceipt receipt_json") as excinfo:
        client.get_decision_receipt("s1")
    assert not isinstance(excinfo.value, MacpTransportError)


def test_get_receipt_keys_returns_jwks() -> None:
    client, stub = _ready_client()

    keys = client.get_receipt_keys()

    assert keys == json.loads(KEY_SET_JSON)
    assert trusted_keys_from_jwks(keys) == VECTOR["trusted_keys"]
    assert stub.GetReceiptKeys.call_args.args[0] == core_pb2.GetReceiptKeysRequest()


@pytest.mark.parametrize("rpc", ["receipt", "keys"])
def test_receipt_unavailable_surfaces_macp_code(rpc: str) -> None:
    client, stub = _ready_client()
    error = FakeRpcError(
        grpc.StatusCode.FAILED_PRECONDITION,
        "no binding commitment",
        trailing_metadata=[("macp-error-code", RECEIPT_UNAVAILABLE)],
    )
    stub.GetDecisionReceipt.side_effect = error
    stub.GetReceiptKeys.side_effect = error

    with pytest.raises(MacpTransportError) as excinfo:
        _rpc(client, rpc)

    assert excinfo.value.code == "FAILED_PRECONDITION"
    assert excinfo.value.macp_code == RECEIPT_UNAVAILABLE


# ------------------------------------------------------------------ gate


class TestCapabilityGate:
    def test_initializes_once_across_both_rpcs(self) -> None:
        client, stub = _ready_client()

        client.get_decision_receipt("s1")
        client.get_receipt_keys()
        client.get_decision_receipt("s2")

        assert stub.Initialize.call_count == 1
        assert stub.GetDecisionReceipt.call_count == 2

    def test_uses_capabilities_from_an_earlier_initialize(self) -> None:
        client, stub = _ready_client()
        client.server_capabilities = _caps(receipts=True)

        client.get_receipt_keys()

        stub.Initialize.assert_not_called()

    def test_signed_alone_does_not_open_the_gate(self) -> None:
        client, stub = client_with_stub()
        client.server_capabilities = _caps(receipts=False, signed=True)

        with pytest.raises(MacpSdkError, match="GetReceiptKeys refused"):
            client.get_receipt_keys()
        stub.GetReceiptKeys.assert_not_called()

    @pytest.mark.parametrize("rpc", ["receipt", "keys"])
    def test_refuses_when_decision_receipt_absent(self, rpc: str) -> None:
        client, stub = client_with_stub()
        client.server_capabilities = core_pb2.Capabilities()

        with pytest.raises(MacpSdkError, match=r"macp-proto >= 0\.1\.13"):
            _rpc(client, rpc)
        stub.Initialize.assert_not_called()

    @pytest.mark.parametrize("rpc", ["receipt", "keys"])
    def test_refuses_under_bindings_without_the_rpc(
        self, rpc: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Older macp-proto: no decision_receipt field, no request messages,
        # no stub method. The gate must refuse before touching any of them.
        client, _ = client_with_stub()
        for name in ("GetDecisionReceiptRequest", "GetReceiptKeysRequest"):
            monkeypatch.delattr(core_pb2, name)
        client.stub = MagicMock(spec=["Initialize"])
        client.server_capabilities = SimpleNamespace(policy_registry=None)

        with pytest.raises(MacpSdkError, match="RFC-MACP-0013 §13"):
            _rpc(client, rpc)

    def test_get_decision_receipt_requires_auth(self) -> None:
        client, stub = client_with_stub()
        client.auth = None

        with pytest.raises(MacpSdkError, match="requires auth"):
            client.get_decision_receipt("s1")
        stub.Initialize.assert_not_called()

    def test_get_receipt_keys_needs_no_auth(self) -> None:
        client, stub = _ready_client()
        client.auth = None

        client.get_receipt_keys()

        for call in (stub.Initialize.call_args, stub.GetReceiptKeys.call_args):
            assert call.kwargs["metadata"] == client._metadata(None)

    @pytest.mark.parametrize("rpc", ["receipt", "keys"])
    def test_forwards_auth_and_timeout(self, rpc: str) -> None:
        client, stub = _ready_client()
        auth = AuthConfig.for_dev_agent("observer")
        method = stub.GetDecisionReceipt if rpc == "receipt" else stub.GetReceiptKeys

        if rpc == "receipt":
            client.get_decision_receipt("s1", auth=auth, timeout=3.0)
        else:
            client.get_receipt_keys(auth=auth, timeout=3.0)

        for call in (stub.Initialize.call_args, method.call_args):
            assert call.kwargs["metadata"] == auth.metadata()
            assert call.kwargs["timeout"] == 3.0


# ------------------------------------------------------------------ errors


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (grpc.StatusCode.NOT_FOUND, "SESSION_NOT_FOUND"),
            (grpc.StatusCode.PERMISSION_DENIED, "FORBIDDEN"),
            (grpc.StatusCode.UNIMPLEMENTED, None),
        ],
    )
    def test_rpc_error_is_a_transport_error(
        self, status: grpc.StatusCode, code: str | None
    ) -> None:
        client, stub = _ready_client()
        trailing = [("macp-error-code", code)] if code else []
        stub.GetDecisionReceipt.side_effect = FakeRpcError(
            status, "refused", trailing_metadata=trailing
        )

        with pytest.raises(MacpTransportError) as excinfo:
            client.get_decision_receipt("s1")
        assert excinfo.value.code == status.name
        assert excinfo.value.macp_code == code

    def test_failed_lazy_initialize_is_a_transport_error(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.side_effect = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")

        with pytest.raises(MacpTransportError) as excinfo:
            client.get_receipt_keys()
        assert excinfo.value.code == "UNAVAILABLE"
        assert client.server_capabilities is None
        stub.GetReceiptKeys.assert_not_called()

    def test_deeply_nested_receipt_is_an_sdk_error(self) -> None:
        client, stub = _ready_client()
        stub.GetDecisionReceipt.return_value = core_pb2.GetDecisionReceiptResponse(
            receipt_json='{"a":' * 100_000 + "1" + "}" * 100_000
        )

        with pytest.raises(MacpSdkError, match="not valid JSON"):
            client.get_decision_receipt("s1")

    @pytest.mark.parametrize(
        "jwks_json",
        ["", "nope", "[]", "{}", '{"keys": {}}', '{"keys": null}', '{"keys": [NaN]}'],
        ids=["empty", "text", "array", "no-keys", "keys-object", "keys-null", "nan"],
    )
    def test_get_receipt_keys_rejects_a_non_key_set(self, jwks_json: str) -> None:
        client, stub = _ready_client()
        stub.GetReceiptKeys.return_value = core_pb2.GetReceiptKeysResponse(jwks_json=jwks_json)

        with pytest.raises(MacpSdkError, match="GetReceiptKeys jwks_json"):
            client.get_receipt_keys()

    def test_unsigned_runtime_empty_key_set(self) -> None:
        client, stub = _ready_client()
        stub.GetReceiptKeys.return_value = core_pb2.GetReceiptKeysResponse(jwks_json='{"keys":[]}')

        assert client.get_receipt_keys() == {"keys": []}

    def test_result_is_frozen(self) -> None:
        result = DecisionReceiptResult(receipt={}, raw="{}")
        with pytest.raises(AttributeError):
            result.raw = "x"  # type: ignore[misc]


# ------------------------------------------------------------------ async


def _async_client() -> tuple[AsyncMacpClient, MagicMock]:
    client = AsyncMacpClient(
        target="localhost:0",
        allow_insecure=True,
        auth=AuthConfig.for_bearer("tok", sender_hint="alice"),
    )
    stub = MagicMock()
    stub.Initialize = AsyncMock(
        return_value=core_pb2.InitializeResponse(capabilities=_caps(receipts=True))
    )
    stub.GetDecisionReceipt = AsyncMock(
        return_value=core_pb2.GetDecisionReceiptResponse(receipt_json=RECEIPT_JSON)
    )
    stub.GetReceiptKeys = AsyncMock(
        return_value=core_pb2.GetReceiptKeysResponse(jwks_json=KEY_SET_JSON)
    )
    client.stub = stub
    return client, stub


class TestAsync:
    def test_returns_parsed_and_raw_and_keys(self) -> None:
        async def go() -> tuple[DecisionReceiptResult, Any, MagicMock]:
            client, stub = _async_client()
            return await client.get_decision_receipt("s1"), await client.get_receipt_keys(), stub

        result, keys, stub = asyncio.run(go())
        assert result.raw == RECEIPT_JSON
        assert verify_receipt(result.receipt, trusted_keys_from_jwks(keys))["verdict"] == "attested"
        assert stub.Initialize.await_count == 1

    def test_requires_capability(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            client.server_capabilities = _caps(receipts=False)
            for call in (lambda: client.get_decision_receipt("s1"), client.get_receipt_keys):
                with pytest.raises(MacpSdkError, match="RFC-MACP-0013 §13"):
                    await call()
            stub.GetDecisionReceipt.assert_not_called()
            stub.GetReceiptKeys.assert_not_called()

        asyncio.run(go())

    def test_rejects_non_json(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            stub.GetDecisionReceipt.return_value = core_pb2.GetDecisionReceiptResponse(
                receipt_json="[]"
            )
            stub.GetReceiptKeys.return_value = core_pb2.GetReceiptKeysResponse(jwks_json="{}")
            with pytest.raises(MacpSdkError, match="receipt_json"):
                await client.get_decision_receipt("s1")
            with pytest.raises(MacpSdkError, match="jwks_json"):
                await client.get_receipt_keys()

        asyncio.run(go())

    def test_receipt_unavailable_surfaces_macp_code(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            stub.GetDecisionReceipt.side_effect = FakeRpcError(
                grpc.StatusCode.FAILED_PRECONDITION,
                "open session",
                trailing_metadata=[("macp-error-code", RECEIPT_UNAVAILABLE)],
            )
            with pytest.raises(MacpTransportError) as excinfo:
                await client.get_decision_receipt("s1")
            assert excinfo.value.macp_code == RECEIPT_UNAVAILABLE

        asyncio.run(go())

    def test_get_receipt_keys_initializes_once_without_auth(self) -> None:
        async def go() -> MagicMock:
            client, stub = _async_client()
            client.auth = None
            await client.get_receipt_keys()
            await client.get_receipt_keys()
            return stub

        stub = asyncio.run(go())
        assert stub.Initialize.await_count == 1
        assert stub.GetReceiptKeys.await_args.kwargs["metadata"] == []

    def test_get_decision_receipt_requires_auth(self) -> None:
        async def go() -> MagicMock:
            client, stub = _async_client()
            client.auth = None
            with pytest.raises(MacpSdkError, match="requires auth"):
                await client.get_decision_receipt("s1")
            return stub

        stub = asyncio.run(go())
        stub.Initialize.assert_not_called()

    def test_get_receipt_keys_rpc_error_is_a_transport_error(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            stub.GetReceiptKeys.side_effect = FakeRpcError(
                grpc.StatusCode.PERMISSION_DENIED,
                "no",
                trailing_metadata=[("macp-error-code", "FORBIDDEN")],
            )
            with pytest.raises(MacpTransportError) as excinfo:
                await client.get_receipt_keys()
            assert excinfo.value.macp_code == "FORBIDDEN"

        asyncio.run(go())

    def test_failed_lazy_initialize_is_a_transport_error(self) -> None:
        async def go() -> AsyncMacpClient:
            client, stub = _async_client()
            stub.Initialize.side_effect = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")
            with pytest.raises(MacpTransportError):
                await client.get_receipt_keys()
            stub.GetReceiptKeys.assert_not_called()
            return client

        assert asyncio.run(go()).server_capabilities is None
