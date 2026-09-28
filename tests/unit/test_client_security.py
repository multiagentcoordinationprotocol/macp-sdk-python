"""Transport security defaults for MacpClient (PY-2).

RFC-MACP-0004 §2 (Transport Security) requires TLS 1.2+ in production. The SDK defaults to
``secure=True`` and insists on an explicit ``allow_insecure=True`` opt-in
before building a plaintext channel. This keeps the dev loop cheap while
ensuring new code can't silently ship without TLS.
"""

from __future__ import annotations

import pytest

from macp_sdk.auth import AuthConfig
from macp_sdk.client import MacpClient
from macp_sdk.errors import MacpSdkError


class TestMacpClientSecureDefault:
    def test_default_is_secure(self):
        client = MacpClient(target="runtime.example.com:50051")
        assert client.secure is True
        client.close()

    def test_allow_insecure_opt_in(self):
        client = MacpClient(target="localhost:50051", allow_insecure=True)
        assert client.secure is False
        client.close()

    def test_secure_false_without_opt_in_raises(self):
        with pytest.raises(MacpSdkError, match="allow_insecure=True"):
            MacpClient(target="localhost:50051", secure=False)

    def test_secure_false_with_opt_in_succeeds(self):
        client = MacpClient(target="localhost:50051", secure=False, allow_insecure=True)
        assert client.secure is False
        client.close()

    def test_secure_true_explicit(self):
        client = MacpClient(target="runtime.example.com:50051", secure=True)
        assert client.secure is True
        client.close()

    def test_auth_pass_through(self):
        auth = AuthConfig.for_bearer("tok", expected_sender="alice")
        client = MacpClient(target="runtime.example.com:50051", auth=auth)
        assert client.auth is auth
        client.close()
