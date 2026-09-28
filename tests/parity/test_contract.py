"""Cross-SDK parity contract (issue #93 item 1): asserts this SDK's actual
runtime values against every section of the vendored ``contract.json`` (see
``SOURCE.md``) whose ``applies_to`` names ``macp-sdk-python``.
``contract.json`` is non-normative (its own ``$comment`` and the spec repo's
``schemas/parity/README.md`` say so) -- this test reads the live manifest
content rather than hand-copying its values, so drift between the manifest
and this file shows up as a failing assertion here, not just as
``make verify-parity`` drift between the manifest and its canonical source.

Modeled on macp-sdk-typescript's ``tests/parity/contract.test.ts`` (the
richer of the two existing consumers) -- see that file's own header comment
for the pattern this mirrors.

``contribute_acceptance`` (``applies_to: [macp-runtime]`` only) is
deliberately not asserted -- see ``SOURCE.md`` "Open items".
"""

from __future__ import annotations

import dataclasses
import json
import typing
from pathlib import Path
from typing import ClassVar

from macp_sdk.base_projection import (
    ANOMALY_DUPLICATE_BALLOT,
    ANOMALY_DUPLICATE_TASK_ACCEPT,
    ANOMALY_DUPLICATE_VOTE,
    ANOMALY_SETTLED_HANDOFF,
    ProjectionAnomaly,
)
from macp_sdk.commitment_hash import is_canonical_commitment_hash
from macp_sdk.constants import (
    DEFAULT_CONFIGURATION_VERSION,
    DEFAULT_MODE_VERSION,
    DEFAULT_POLICY_VERSION,
    MACP_VERSION,
    MODE_MULTI_ROUND,
    STANDARD_MODES,
)
from macp_sdk.errors import (
    DUPLICATE_MESSAGE,
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_ENVELOPE,
    INVALID_POLICY_DEFINITION,
    INVALID_SESSION_ID,
    MODE_NOT_SUPPORTED,
    PAYLOAD_TOO_LARGE,
    POLICY_DENIED,
    RATE_LIMITED,
    SESSION_ALREADY_EXISTS,
    SESSION_NOT_FOUND,
    SESSION_NOT_OPEN,
    UNAUTHENTICATED,
    UNKNOWN_POLICY_VERSION,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from macp_sdk.policy import build_decision_policy
from macp_sdk.proto_registry import ProtoRegistry
from macp_sdk.retry import RetryPolicy

with (Path(__file__).parent / "contract.json").open() as _f:
    contract: dict[str, typing.Any] = json.load(_f)

sections = contract["sections"]
registry = ProtoRegistry()


def test_pins_contract_version_1_2_0_a_version_bump_means_re_reading_this_whole_file():
    # Not a manifest-content assertion: a tripwire so a future contract_version
    # bump (MINOR or MAJOR, per the manifest's own versioning rule) forces a
    # human to re-review every section below, not just whichever one changed.
    assert contract["contract_version"] == "1.2.0"


class TestProtocol:
    def test_macp_version_matches_constant(self):
        assert sections["protocol"]["macp_version"] == MACP_VERSION


class TestModes:
    def test_standard_mode_ids_match_in_order(self):
        assert list(STANDARD_MODES) == sections["modes"]["standard"]

    def test_extension_mode_id_matches(self):
        assert sections["modes"]["extension"] == [MODE_MULTI_ROUND]


class TestDefaults:
    def test_default_versions_match(self):
        defaults = sections["defaults"]
        assert defaults["mode_version"] == DEFAULT_MODE_VERSION
        assert defaults["configuration_version"] == DEFAULT_CONFIGURATION_VERSION
        assert defaults["policy_version"] == DEFAULT_POLICY_VERSION

    def test_policy_builder_schema_version_matches_build_decision_policy_default(self):
        descriptor = build_decision_policy("policy.parity-probe", "parity probe")
        assert descriptor.schema_version == sections["defaults"]["policy_builder_schema_version"]


class TestErrorCodes:
    _EXPORTED: ClassVar[dict[str, str]] = {
        "UNSUPPORTED_PROTOCOL_VERSION": UNSUPPORTED_PROTOCOL_VERSION,
        "INVALID_ENVELOPE": INVALID_ENVELOPE,
        "SESSION_ALREADY_EXISTS": SESSION_ALREADY_EXISTS,
        "SESSION_NOT_FOUND": SESSION_NOT_FOUND,
        "SESSION_NOT_OPEN": SESSION_NOT_OPEN,
        "MODE_NOT_SUPPORTED": MODE_NOT_SUPPORTED,
        "FORBIDDEN": FORBIDDEN,
        "UNAUTHENTICATED": UNAUTHENTICATED,
        "DUPLICATE_MESSAGE": DUPLICATE_MESSAGE,
        "PAYLOAD_TOO_LARGE": PAYLOAD_TOO_LARGE,
        "RATE_LIMITED": RATE_LIMITED,
        "INTERNAL_ERROR": INTERNAL_ERROR,
        "POLICY_DENIED": POLICY_DENIED,
        "INVALID_SESSION_ID": INVALID_SESSION_ID,
        "UNKNOWN_POLICY_VERSION": UNKNOWN_POLICY_VERSION,
        "INVALID_POLICY_DEFINITION": INVALID_POLICY_DEFINITION,
    }

    def test_exports_exactly_the_16_permanent_codes_order_independent(self):
        assert sorted(self._EXPORTED) == sorted(sections["error_codes"]["permanent"])
        for name, value in self._EXPORTED.items():
            assert value == name, name

    def test_has_no_export_for_the_one_deprecated_code(self):
        # No macp_sdk.errors counterpart for 'UNAUTHORIZED' on purpose -- it's
        # deprecated and should not be re-exported. Hardcoded here rather
        # than imported, since there is nothing to import.
        assert sections["error_codes"]["deprecated"] == ["UNAUTHORIZED"]
        import macp_sdk.errors as errors_module

        assert not hasattr(errors_module, "UNAUTHORIZED")


class TestRetry:
    def test_default_retry_policy_matches_every_pinned_field(self):
        retry = sections["retry"]
        default = RetryPolicy()
        assert default.max_retries == retry["max_retries"]
        assert default.backoff_base == retry["backoff_base_seconds"]
        assert default.backoff_max == retry["backoff_max_seconds"]
        assert sorted(default.retryable_codes) == sorted(retry["retryable_error_codes"])

    def test_illustrative_backoff_schedule_is_derivable_not_a_stored_field(self):
        # backoff_schedule_seconds is documented as a *derived* illustrative
        # value in the manifest, not a real RetryPolicy field -- recompute
        # it from backoff_base/backoff_max rather than looking for a
        # matching attribute. jitter: false documents deliberate absence
        # (this SDK's RetryPolicy has no jitter field); no assertion needed
        # for it beyond confirming the manifest still says so.
        default = RetryPolicy()
        schedule = [
            min(default.backoff_base * (2**attempt), default.backoff_max) for attempt in (0, 1, 2)
        ]
        assert schedule == sections["retry"]["backoff_schedule_seconds"]
        assert sections["retry"]["jitter"] is False


class TestProjectionAnomaly:
    def test_kinds_match_the_four_manifest_pinned_constants(self):
        # All four -- the issue #94 cross-SDK agreement (ANOMALY_DUPLICATE_TASK_ACCEPT
        # / ANOMALY_SETTLED_HANDOFF) settled at contract 1.2.0 once
        # macp-sdk-typescript landed its matching side (PR #134). See
        # SOURCE.md "Open items" and base_projection.py's own comment.
        expected = [
            ANOMALY_DUPLICATE_VOTE,
            ANOMALY_DUPLICATE_BALLOT,
            ANOMALY_DUPLICATE_TASK_ACCEPT,
            ANOMALY_SETTLED_HANDOFF,
        ]
        assert sections["projection_anomaly"]["kinds"] == expected

    def test_fields_match_field_order_in_order(self):
        # field_case_rule only matters for a lowerCamelCase consumer
        # (macp-sdk-typescript) -- Python's own convention is already the
        # manifest's canonical snake_case, so no transform is needed here.
        field_names = [f.name for f in dataclasses.fields(ProjectionAnomaly)]
        assert field_names == sections["projection_anomaly"]["fields"]


class TestCommitmentHash:
    def test_pins_the_manifest_pattern_string(self):
        # Informational -- the predicate itself is exercised below.
        assert sections["commitment_hash"]["pattern"] == "^sha256:[0-9a-f]{64}$"

    def test_accepts_every_manifest_accept_vector(self):
        accept = sections["commitment_hash"]["accept"]
        assert len(accept) == 1
        for value in accept:
            assert is_canonical_commitment_hash(value), value

    def test_rejects_every_manifest_reject_vector(self):
        reject = sections["commitment_hash"]["reject"]
        assert len(reject) == 11
        for value in reject:
            assert not is_canonical_commitment_hash(value), repr(value)


class TestContributePayload:
    def test_encodes_every_vector_to_the_pinned_canonical_protobuf_hex(self):
        vectors = sections["contribute_payload"]["vectors"]
        assert len(vectors) == 8
        for vector in vectors:
            encoded = registry.encode_known_payload(
                MODE_MULTI_ROUND, "Contribute", {"value": vector["value"]}
            )
            assert encoded.hex() == vector["protobuf_hex"], vector["name"]

    def test_decodes_every_vector_carrying_legacy_json_hex_back_to_value(self):
        # The manifest deliberately does not pin a decoded *library return
        # shape* (issue #93's own "does NOT ask for" section, and
        # schemas/parity/README.md's out-of-scope subsection) -- this SDK's
        # decode_known_payload wraps a legacy-JSON decode as
        # {"encoding": "json", "json": parsed} (proto_registry.py), unlike
        # macp-sdk-typescript's flat {value}. Assert against this SDK's own
        # real shape, not TypeScript's.
        vectors = sections["contribute_payload"]["vectors"]
        with_legacy = [v for v in vectors if "legacy_json_hex" in v]
        assert len(with_legacy) > 0
        for vector in with_legacy:
            decoded = registry.decode_known_payload(
                MODE_MULTI_ROUND, "Contribute", bytes.fromhex(vector["legacy_json_hex"])
            )
            expected = {"encoding": "json", "json": {"value": vector["value"]}}
            assert decoded == expected, vector["name"]

    def test_decode_only_vector_round_trips_via_protobuf_only(self):
        vectors = sections["contribute_payload"]["vectors"]
        vector = next(v for v in vectors if v["name"] == "one_byte_varint_boundary")
        assert "legacy_json_hex" not in vector
        decoded = registry.decode_known_payload(
            MODE_MULTI_ROUND, "Contribute", bytes.fromhex(vector["protobuf_hex"])
        )
        assert decoded == {"value": vector["value"]}

    def test_decode_order_is_json_first(self):
        assert sections["contribute_payload"]["decode_order"] == ["json", "protobuf"]
        # Re-assert JSON-first behaviorally by hand-building a legacy-JSON
        # buffer from one of the manifest's own vector *values* (not its
        # legacy_json_hex, so this stays a from-scratch JSON encode rather
        # than reusing the same bytes the legacy-JSON decode test above
        # already exercises).
        vector = sections["contribute_payload"]["vectors"][0]
        encoded = json.dumps({"value": vector["value"]}).encode("utf-8")
        decoded = registry.decode_known_payload(MODE_MULTI_ROUND, "Contribute", encoded)
        assert decoded == {"encoding": "json", "json": {"value": vector["value"]}}
