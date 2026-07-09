"""Shared fixtures for integration tests.

All integration tests require a live MACP runtime (default
``127.0.0.1:50051``, override with ``MACP_RUNTIME_TARGET``) started with
``MACP_ALLOW_INSECURE=1``. When no runtime is reachable the whole
directory is skipped instead of failing with connection errors, so a
bare ``pytest tests/`` stays green without one.
"""

from __future__ import annotations

import os
import socket

import pytest

from macp_sdk import AuthConfig, MacpClient

RUNTIME_TARGET = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")


def make_client(agent: str) -> MacpClient:
    """A dev-auth client for ``agent`` against the shared runtime target."""
    return MacpClient(
        target=RUNTIME_TARGET,
        allow_insecure=True,
        auth=AuthConfig.for_dev_agent(agent),
    )


@pytest.fixture(scope="session", autouse=True)
def _require_runtime() -> None:
    host, _, port = RUNTIME_TARGET.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1.0):
            pass
    except OSError:
        pytest.skip(
            f"no MACP runtime reachable at {RUNTIME_TARGET}; "
            "start one with MACP_ALLOW_INSECURE=1 (see make test-integration)"
        )
