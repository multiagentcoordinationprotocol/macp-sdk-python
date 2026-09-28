"""Tests for MacpClient's timeout resolution and the UNBOUNDED sentinel
(issue #93 item 6).

Before this fix, every RPC call site merged a per-call ``timeout`` against
``self.default_timeout`` via ``timeout or self.default_timeout`` -- since
both ``None`` and ``0`` are falsy, once ``default_timeout`` was set to a
non-None value, no per-call argument could express "no deadline for this
one call". ``UNBOUNDED`` is the explicit value that now does.
"""

from __future__ import annotations

from macp.v1 import core_pb2

from macp_sdk.client import UNBOUNDED, MacpClient
from tests.conftest import client_with_stub


class TestResolveTimeout:
    def test_none_uses_default_timeout(self):
        client, _ = client_with_stub()
        client.default_timeout = 5.0
        assert client._resolve_timeout(None) == 5.0

    def test_explicit_value_overrides_default(self):
        client, _ = client_with_stub()
        client.default_timeout = 5.0
        assert client._resolve_timeout(1.5) == 1.5

    def test_unbounded_returns_none_even_with_default_set(self):
        """The exact deadlock #93 item 6 describes: neither None nor 0 could
        request "no deadline" once default_timeout was set -- UNBOUNDED can.
        """
        client, _ = client_with_stub()
        client.default_timeout = 5.0
        assert client._resolve_timeout(UNBOUNDED) is None

    def test_none_with_no_default_configured_is_unbounded_already(self):
        client, _ = client_with_stub()
        assert client.default_timeout is None
        assert client._resolve_timeout(None) is None

    def test_zero_is_a_real_zero_second_timeout_not_unbounded(self):
        """0 remains a real (if degenerate) timeout value, distinct from
        UNBOUNDED -- this sentinel adds a new way to request "no deadline",
        it does not repurpose 0 to mean that."""
        client, _ = client_with_stub()
        client.default_timeout = 5.0
        assert client._resolve_timeout(0) == 0


class TestUnboundedEndToEnd:
    """Confirm UNBOUNDED actually reaches the stub call as timeout=None,
    through a couple of representative call sites, with a non-None
    default_timeout configured -- the exact scenario item 6 was unfixable
    under."""

    def test_get_manifest_unbounded_overrides_default(self):
        client, stub = client_with_stub()
        client.default_timeout = 5.0
        stub.GetManifest.return_value = core_pb2.GetManifestResponse()

        client.get_manifest(timeout=UNBOUNDED)

        assert stub.GetManifest.call_args.kwargs["timeout"] is None

    def test_get_manifest_default_still_applies_when_unspecified(self):
        client, stub = client_with_stub()
        client.default_timeout = 5.0
        stub.GetManifest.return_value = core_pb2.GetManifestResponse()

        client.get_manifest()

        assert stub.GetManifest.call_args.kwargs["timeout"] == 5.0

    def test_initialize_unbounded_overrides_default(self):
        client, stub = client_with_stub()
        client.default_timeout = 5.0
        stub.Initialize.return_value = core_pb2.InitializeResponse()

        client.initialize(timeout=UNBOUNDED)

        assert stub.Initialize.call_args.kwargs["timeout"] is None


def test_unbounded_repr():
    assert repr(UNBOUNDED) == "UNBOUNDED"


def test_unbounded_is_exported_from_package_root():
    from macp_sdk import UNBOUNDED as UNBOUNDED_FROM_ROOT

    assert UNBOUNDED_FROM_ROOT is UNBOUNDED
    assert isinstance(MacpClient, type)  # sanity: import path is real
