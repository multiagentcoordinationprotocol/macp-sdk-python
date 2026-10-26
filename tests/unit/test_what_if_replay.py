"""WhatIfReplay (RFC-MACP-0012 §8.1): capability gate, request, errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from macp.v1 import core_pb2, envelope_pb2, policy_pb2

from macp_sdk import (
    HISTORY_UNAVAILABLE,
    AsyncMacpClient,
    AuthConfig,
    MacpSdkError,
    MacpTransportError,
    build_decision_policy,
    parse_error_details,
)
from tests.conftest import FakeRpcError, client_with_stub


def _init_response(*, what_if: bool) -> core_pb2.InitializeResponse:
    return core_pb2.InitializeResponse(
        capabilities=core_pb2.Capabilities(
            policy_registry=policy_pb2.PolicyRegistryCapability(what_if_replay=what_if)
        )
    )


def _report() -> policy_pb2.WhatIfReplayResponse:
    denial = {"reasons": ["vetoed"], "denials": [{"code": "veto", "message": "vetoed"}]}
    return policy_pb2.WhatIfReplayResponse(
        candidate_policy_id="policy.candidate",
        recorded_policy_id="policy.default",
        recorded=policy_pb2.WhatIfOutcome(
            state=envelope_pb2.SESSION_STATE_RESOLVED,
            resolved_by_history_index=4,
            resolved_by_message_id="m4",
            resolved_by_message_type="Commitment",
        ),
        candidate=policy_pb2.WhatIfOutcome(state=envelope_pb2.SESSION_STATE_OPEN),
        first_divergence=4,
        denials=[
            policy_pb2.WhatIfDenial(
                history_index=4, message_id="m4", details=json.dumps(denial).encode()
            )
        ],
    )


CANDIDATE = build_decision_policy("policy.candidate", "stricter")


class TestCapabilityGate:
    def test_initializes_once_then_calls_the_rpc(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.return_value = _init_response(what_if=True)
        stub.WhatIfReplay.return_value = _report()

        report = client.what_if_replay("s1", CANDIDATE)
        client.what_if_replay("s1", CANDIDATE)

        assert stub.Initialize.call_count == 1
        request = stub.WhatIfReplay.call_args.args[0]
        assert request == policy_pb2.WhatIfReplayRequest(session_id="s1", candidate=CANDIDATE)
        assert report.HasField("first_divergence")
        assert report.first_divergence == 4
        (denial,) = parse_error_details(report.denials[0].details).denials
        assert denial.code == "veto"

    def test_uses_capabilities_from_an_earlier_initialize(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.return_value = _init_response(what_if=True)
        client.initialize()
        assert client.server_capabilities is not None
        stub.WhatIfReplay.return_value = policy_pb2.WhatIfReplayResponse()

        report = client.what_if_replay("s1", CANDIDATE)

        assert stub.Initialize.call_count == 1
        # Unset first_divergence means the replays agree; 0 would be the SessionStart.
        assert not report.HasField("first_divergence")

    def test_refuses_without_sending_when_not_advertised(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.return_value = _init_response(what_if=False)

        with pytest.raises(MacpSdkError, match=r"RFC-MACP-0012 §8\.1"):
            client.what_if_replay("s1", CANDIDATE)
        stub.WhatIfReplay.assert_not_called()

    def test_refuses_when_policy_registry_absent(self) -> None:
        client, stub = client_with_stub()
        client.server_capabilities = core_pb2.Capabilities()

        with pytest.raises(MacpSdkError, match="what_if_replay"):
            client.what_if_replay("s1", CANDIDATE)
        stub.Initialize.assert_not_called()
        stub.WhatIfReplay.assert_not_called()

    def test_requires_auth(self) -> None:
        client, stub = client_with_stub()
        client.auth = None
        with pytest.raises(MacpSdkError, match="requires auth"):
            client.what_if_replay("s1", CANDIDATE)
        stub.Initialize.assert_not_called()

    def test_forwards_auth_and_timeout(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.return_value = _init_response(what_if=True)
        auth = AuthConfig.for_dev_agent("observer")

        client.what_if_replay("s1", CANDIDATE, auth=auth, timeout=3.0)

        for call in (stub.Initialize.call_args, stub.WhatIfReplay.call_args):
            assert call.kwargs["metadata"] == auth.metadata()
            assert call.kwargs["timeout"] == 3.0


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (grpc.StatusCode.FAILED_PRECONDITION, HISTORY_UNAVAILABLE),
            (grpc.StatusCode.NOT_FOUND, "SESSION_NOT_FOUND"),
            (grpc.StatusCode.INVALID_ARGUMENT, "INVALID_POLICY_DEFINITION"),
        ],
    )
    def test_rpc_error_is_a_transport_error_with_macp_code(
        self, status: grpc.StatusCode, code: str
    ) -> None:
        client, stub = client_with_stub()
        stub.Initialize.return_value = _init_response(what_if=True)
        stub.WhatIfReplay.side_effect = FakeRpcError(
            status, "refused", trailing_metadata=[("macp-error-code", code)]
        )

        with pytest.raises(MacpTransportError) as excinfo:
            client.what_if_replay("s1", CANDIDATE)
        assert excinfo.value.code == status.name
        assert excinfo.value.macp_code == code

    def test_failed_lazy_initialize_is_a_transport_error(self) -> None:
        client, stub = client_with_stub()
        stub.Initialize.side_effect = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")

        with pytest.raises(MacpTransportError) as excinfo:
            client.what_if_replay("s1", CANDIDATE)
        assert excinfo.value.code == "UNAVAILABLE"
        assert client.server_capabilities is None
        stub.WhatIfReplay.assert_not_called()


def _async_client() -> tuple[AsyncMacpClient, MagicMock]:
    client = AsyncMacpClient(
        target="localhost:0",
        allow_insecure=True,
        auth=AuthConfig.for_bearer("tok", sender_hint="alice"),
    )
    stub = MagicMock()
    client.stub = stub
    return client, stub


class TestAsync:
    def test_initializes_once_then_calls_the_rpc(self) -> None:
        async def go() -> tuple[Any, MagicMock]:
            client, stub = _async_client()
            stub.Initialize = AsyncMock(return_value=_init_response(what_if=True))
            stub.WhatIfReplay = AsyncMock(return_value=_report())
            await client.what_if_replay("s1", CANDIDATE)
            return await client.what_if_replay("s1", CANDIDATE), stub

        report, stub = asyncio.run(go())
        assert report.first_divergence == 4
        assert stub.Initialize.await_count == 1

    def test_refuses_without_sending_when_not_advertised(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            stub.Initialize = AsyncMock(return_value=_init_response(what_if=False))
            stub.WhatIfReplay = AsyncMock()
            with pytest.raises(MacpSdkError, match=r"RFC-MACP-0012 §8\.1"):
                await client.what_if_replay("s1", CANDIDATE)
            stub.WhatIfReplay.assert_not_called()

        asyncio.run(go())

    def test_rpc_error_is_a_transport_error_with_macp_code(self) -> None:
        async def go() -> None:
            client, stub = _async_client()
            client.server_capabilities = _init_response(what_if=True).capabilities
            stub.WhatIfReplay = AsyncMock(
                side_effect=FakeRpcError(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "compacted",
                    trailing_metadata=[("macp-error-code", HISTORY_UNAVAILABLE)],
                )
            )
            with pytest.raises(MacpTransportError) as excinfo:
                await client.what_if_replay("s1", CANDIDATE)
            assert excinfo.value.macp_code == HISTORY_UNAVAILABLE

        asyncio.run(go())
