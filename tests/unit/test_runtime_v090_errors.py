"""Runtime 0.9.0 (S13) error surface: ``macp-error-code`` status metadata,
structured ``POLICY_DENIED`` denials, and the pre-0.9 fallback mapping."""

from __future__ import annotations

import json

import grpc
import pytest
from macp.v1 import envelope_pb2

from macp_sdk import (
    AGENT_NOT_FOUND,
    INVALID_REQUEST,
    KNOWN_DENY_CODES,
    STREAM_LAGGED,
    ErrorDetails,
    MacpAckError,
    MacpTransportError,
    PolicyDenial,
    build_quorum_policy,
    macp_error_code,
    parse_error_details,
)
from macp_sdk.errors import DENY_UNSPECIFIED, DENY_VETO
from tests.conftest import FakeRpcError
from tests.conftest import client_with_stub as _client_with_stub

_DENIED = {
    "reasons": [
        "no qualifying evaluation meets minimum confidence threshold: 0.70",
        "blocked by 1 blocking objection(s) (veto threshold: 1)",
    ],
    "denials": [
        {
            "code": "confidence_below_minimum",
            "message": "no qualifying evaluation meets minimum confidence threshold: 0.70",
        },
        {
            "code": "veto",
            "message": "blocked by 1 blocking objection(s) (veto threshold: 1)",
            "participants": ["agent://compliance"],
        },
    ],
}


def _envelope() -> envelope_pb2.Envelope:
    return envelope_pb2.Envelope(session_id="s1", message_id="m1", sender="alice")


class TestParseErrorDetails:
    def test_reasons_and_denials(self):
        details = parse_error_details(json.dumps(_DENIED).encode())
        assert details.reasons == _DENIED["reasons"]
        assert details.denials == [
            PolicyDenial(
                code="confidence_below_minimum",
                message=_DENIED["denials"][0]["message"],
            ),
            PolicyDenial(
                code=DENY_VETO,
                message=_DENIED["denials"][1]["message"],
                participants=("agent://compliance",),
            ),
        ]

    def test_pre_090_reasons_only(self):
        details = parse_error_details(b'{"reasons": ["a", "b"]}')
        assert details == ErrorDetails(reasons=["a", "b"], denials=[])

    def test_unknown_code_passed_through(self):
        raw = {"denials": [{"code": "brand_new_rule", "message": "x"}]}
        (denial,) = parse_error_details(json.dumps(raw)).denials
        assert denial.code == "brand_new_rule"
        assert denial.code not in KNOWN_DENY_CODES

    def test_missing_code_is_unspecified_and_missing_message_kept(self):
        raw = {"denials": [{"participants": ["a"]}, {"code": "", "message": "m"}]}
        first, second = parse_error_details(json.dumps(raw)).denials
        assert first == PolicyDenial(code=DENY_UNSPECIFIED, message="", participants=("a",))
        assert second.code == DENY_UNSPECIFIED

    def test_missing_or_mistyped_fields(self):
        raw = {
            "reasons": ["ok", 3],
            "denials": [
                "not-an-object",
                {"code": 7, "message": None, "participants": "agent://x"},
                {"participants": ["agent://a", 1]},
            ],
        }
        details = parse_error_details(json.dumps(raw))
        assert details.reasons == ["ok"]
        assert details.denials == [
            PolicyDenial(code=DENY_UNSPECIFIED),
            PolicyDenial(code=DENY_UNSPECIFIED, participants=("agent://a",)),
        ]

    @pytest.mark.parametrize("raw", [None, b"", b"not json", b"[1, 2]", b'"str"'])
    def test_malformed_yields_empty(self, raw):
        assert parse_error_details(raw) == ErrorDetails()

    def test_known_codes_cover_runtime_set(self):
        assert {
            "veto",
            "veto_hold",
            "quorum_not_met",
            "threshold_not_met",
            "confidence_below_minimum",
            "evaluation_required",
            "no_votes",
            "decline_over_approval",
            "decline_not_backed",
            "criterion_not_met",
            "output_required",
            "reassignment_not_allowed",
            "unsupported_schema_version",
            "invalid_rules",
            "unspecified",
        } == KNOWN_DENY_CODES


class TestAckNackDenials:
    def test_nack_carries_denials(self):
        client, stub = _client_with_stub()
        resp = stub.Send.return_value
        resp.ack.duplicate = False
        resp.ack.ok = False
        resp.ack.session_id = "s1"
        resp.ack.message_id = "m1"
        resp.ack.error.code = "POLICY_DENIED"
        resp.ack.error.message = "denied"
        resp.ack.error.details = json.dumps(_DENIED).encode()
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        err = excinfo.value
        assert err.reasons == _DENIED["reasons"]
        assert [d.code for d in err.denials] == ["confidence_below_minimum", "veto"]
        assert err.denials[1].participants == ("agent://compliance",)
        assert "denials=" in repr(err)


class TestSendStatusMapping:
    @pytest.mark.parametrize(
        ("status", "macp_code"),
        [
            (grpc.StatusCode.PERMISSION_DENIED, "FORBIDDEN"),
            (grpc.StatusCode.NOT_FOUND, "SESSION_NOT_FOUND"),
            (grpc.StatusCode.NOT_FOUND, "UNKNOWN_POLICY_VERSION"),
            (grpc.StatusCode.INVALID_ARGUMENT, "INVALID_ENVELOPE"),
            (grpc.StatusCode.ALREADY_EXISTS, "DUPLICATE_MESSAGE"),
            (grpc.StatusCode.ALREADY_EXISTS, "SESSION_ALREADY_EXISTS"),
            (grpc.StatusCode.FAILED_PRECONDITION, "SESSION_NOT_OPEN"),
            (grpc.StatusCode.RESOURCE_EXHAUSTED, "RATE_LIMITED"),
            (grpc.StatusCode.RESOURCE_EXHAUSTED, "PAYLOAD_TOO_LARGE"),
            (grpc.StatusCode.INTERNAL, "INTERNAL_ERROR"),
        ],
    )
    def test_branches_on_macp_error_code(self, status, macp_code):
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(
            status, "boom", trailing_metadata=[("macp-error-code", macp_code)]
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        failure = excinfo.value.failure
        assert failure.code == macp_code
        assert failure.message == "boom"
        assert (failure.session_id, failure.message_id) == ("s1", "m1")
        assert excinfo.value.macp_code == macp_code

    def test_policy_denied_status_carries_denials(self):
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(
            grpc.StatusCode.PERMISSION_DENIED,
            "PolicyDenied: ...",
            trailing_metadata=[
                ("macp-error-code", "POLICY_DENIED"),
                ("macp-error-details-bin", json.dumps(_DENIED).encode()),
            ],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        err = excinfo.value
        assert err.failure.code == "POLICY_DENIED"
        assert err.reasons == _DENIED["reasons"]
        assert err.denials[1].code == DENY_VETO

    def test_bytes_metadata_code_decoded(self):
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(
            grpc.StatusCode.NOT_FOUND,
            "",
            trailing_metadata=[("macp-error-code", b"SESSION_NOT_FOUND")],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        assert excinfo.value.failure.code == "SESSION_NOT_FOUND"
        assert excinfo.value.failure.message == "SESSION_NOT_FOUND"

    def test_unauthenticated_is_ack_error(self):
        """Permanent registry code, so retry_send won't retry it (TS parity)."""
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(
            grpc.StatusCode.UNAUTHENTICATED,
            "bad token",
            trailing_metadata=[("macp-error-code", "UNAUTHENTICATED")],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        assert excinfo.value.failure.code == "UNAUTHENTICATED"
        assert excinfo.value.macp_code == "UNAUTHENTICATED"

    def test_pre_090_failed_precondition_still_policy_denied(self):
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "denied",
            trailing_metadata=[("macp-error-details-bin", json.dumps(_DENIED).encode())],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.send(_envelope())
        assert excinfo.value.failure.code == "POLICY_DENIED"
        assert [d.code for d in excinfo.value.denials] == ["confidence_below_minimum", "veto"]

    def test_permission_denied_without_metadata_is_transport(self):
        client, stub = _client_with_stub()
        stub.Send.side_effect = FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, "nope")
        with pytest.raises(MacpTransportError) as excinfo:
            client.send(_envelope())
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert excinfo.value.macp_code is None


class TestTransportErrorMacpCode:
    def test_other_rpc_attaches_macp_code(self):
        client, stub = _client_with_stub()
        stub.CancelSession.side_effect = FakeRpcError(
            grpc.StatusCode.NOT_FOUND,
            "no such session",
            trailing_metadata=[("macp-error-code", "SESSION_NOT_FOUND")],
        )
        with pytest.raises(MacpTransportError) as excinfo:
            client.cancel_session("s1", reason="r")
        assert excinfo.value.code == "NOT_FOUND"
        assert excinfo.value.macp_code == "SESSION_NOT_FOUND"

    def test_read_only_registry_unchanged(self):
        client, stub = _client_with_stub()
        stub.UnregisterPolicy.side_effect = FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION, "policy registry is read-only"
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.unregister_policy("policy.x")
        assert excinfo.value.failure.code == "FAILED_PRECONDITION"
        assert excinfo.value.macp_code is None

    def test_registry_read_only_code(self):
        client, stub = _client_with_stub()
        stub.RegisterPolicy.side_effect = FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "policies are loaded from MACP_POLICIES_DIR",
            trailing_metadata=[("macp-error-code", "REGISTRY_READ_ONLY")],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.register_policy(build_quorum_policy("policy.x", "d"))
        assert excinfo.value.failure.code == "FAILED_PRECONDITION"
        assert excinfo.value.macp_code == "REGISTRY_READ_ONLY"
        assert "macp_code='REGISTRY_READ_ONLY'" in repr(excinfo.value)

    def test_registry_failed_precondition_keeps_macp_code(self):
        """Any FAILED_PRECONDITION is the ack error; macp_code names the cause (TS parity)."""
        client, stub = _client_with_stub()
        stub.UnregisterPolicy.side_effect = FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "other",
            trailing_metadata=[("macp-error-code", "SESSION_NOT_OPEN")],
        )
        with pytest.raises(MacpAckError) as excinfo:
            client.unregister_policy("policy.x")
        assert excinfo.value.failure.code == "FAILED_PRECONDITION"
        assert excinfo.value.macp_code == "SESSION_NOT_OPEN"

    def test_registry_invalid_request_is_transport(self):
        client, stub = _client_with_stub()
        stub.RegisterPolicy.side_effect = FakeRpcError(
            grpc.StatusCode.INVALID_ARGUMENT,
            "missing descriptor",
            trailing_metadata=[("macp-error-code", INVALID_REQUEST)],
        )
        with pytest.raises(MacpTransportError) as excinfo:
            client.register_policy(build_quorum_policy("policy.x", "d"))
        assert excinfo.value.macp_code == INVALID_REQUEST

    def test_watch_lag_carries_stream_lagged(self):
        client, stub = _client_with_stub()
        stub.WatchPolicies.return_value = _raising_iter(
            FakeRpcError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "lagged",
                trailing_metadata=[("macp-error-code", STREAM_LAGGED)],
            )
        )
        with pytest.raises(MacpTransportError) as excinfo:
            list(client.watch_policies())
        assert excinfo.value.code == "RESOURCE_EXHAUSTED"
        assert excinfo.value.macp_code == STREAM_LAGGED


def _raising_iter(exc: Exception):
    raise exc
    yield  # pragma: no cover


class TestMacpErrorCodeHelper:
    def test_raw_rpc_error_code(self):
        client, stub = _client_with_stub()
        stub.GetManifest.side_effect = FakeRpcError(
            grpc.StatusCode.NOT_FOUND,
            "no such agent",
            trailing_metadata=[("macp-error-code", AGENT_NOT_FOUND)],
        )
        with pytest.raises(grpc.RpcError) as excinfo:
            client.get_manifest("agent://nobody")
        assert macp_error_code(excinfo.value) == AGENT_NOT_FOUND

    def test_bytes_value_decoded(self):
        err = FakeRpcError(
            grpc.StatusCode.NOT_FOUND,
            "x",
            trailing_metadata=[("macp-error-code", b"SESSION_NOT_FOUND")],
        )
        assert macp_error_code(err) == "SESSION_NOT_FOUND"

    def test_absent_on_pre_090(self):
        assert macp_error_code(FakeRpcError(grpc.StatusCode.NOT_FOUND, "x")) is None
        assert macp_error_code(grpc.RpcError()) is None
