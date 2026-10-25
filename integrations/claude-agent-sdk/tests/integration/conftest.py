"""Integration tests need a live MACP runtime.

Default ``127.0.0.1:50051``; override with ``MACP_RUNTIME_TARGET``. Start one
with::

    docker run -p 50051:50051 -e MACP_ALLOW_INSECURE=1 -e MACP_MEMORY_ONLY=1 \\
        ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0

When none is reachable the directory is skipped, so a bare ``pytest`` stays
green. CI asserts the suite actually ran (see claude-agent-sdk.yml).
"""

from __future__ import annotations

import os
import socket

import pytest

RUNTIME_TARGET = os.environ.get("MACP_RUNTIME_TARGET", "127.0.0.1:50051")


@pytest.fixture(scope="session", autouse=True)
def _require_runtime() -> None:
    host, _, port = RUNTIME_TARGET.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1.0):
            pass
    except OSError:
        pytest.skip(
            f"no MACP runtime reachable at {RUNTIME_TARGET}; "
            "start one with MACP_ALLOW_INSECURE=1 (see README)"
        )
