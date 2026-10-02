"""Unit tests for ``macp_sdk.agent.cancel_callback``.

Spin the HTTP server up on port 0 so the OS picks a free port, POST a
JSON body, and verify the ``on_cancel`` callback fires with the
parsed ``runId`` / ``reason``. Also covers path-mismatch 404 behaviour
and graceful shutdown via ``close()``.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from unittest.mock import MagicMock

import pytest

from macp_sdk.agent.cancel_callback import start_cancel_callback_server


def _post(url: str, body: dict | None, *, timeout: float = 2.0):
    """POST ``body`` (or empty) to ``url`` and return the HTTP response."""
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)


class TestCancelCallbackServer:
    def test_post_invokes_handler_with_run_id_and_reason(self):
        got = threading.Event()
        received: dict[str, str] = {}

        def on_cancel(run_id: str, reason: str) -> None:
            received["run_id"] = run_id
            received["reason"] = reason
            got.set()

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/cancel", on_cancel=on_cancel
        )
        try:
            host, port = server.address
            resp = _post(f"http://{host}:{port}/cancel", {"runId": "run-42", "reason": "stop"})
            assert resp.status == 202
            assert got.wait(timeout=2.0), "on_cancel never fired"
            assert received == {"run_id": "run-42", "reason": "stop"}
        finally:
            server.close()

    def test_snake_case_run_id_also_accepted(self):
        """The TS reference POSTs ``runId`` but some producers use
        ``run_id``; accept both so the Python SDK is robust."""
        got = threading.Event()
        received: dict[str, str] = {}

        def on_cancel(run_id: str, reason: str) -> None:
            received["run_id"] = run_id
            got.set()

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=on_cancel
        )
        try:
            host, port = server.address
            _post(f"http://{host}:{port}/c", {"run_id": "snake-run"})
            assert got.wait(timeout=2.0)
            assert received == {"run_id": "snake-run"}
        finally:
            server.close()

    def test_path_mismatch_returns_404(self):
        on_cancel = MagicMock()
        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/cancel", on_cancel=on_cancel
        )
        try:
            host, port = server.address
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"http://{host}:{port}/wrong", {"runId": "x"})
            assert exc_info.value.code == 404
            on_cancel.assert_not_called()
        finally:
            server.close()

    def test_malformed_json_treated_as_empty_body(self):
        got = threading.Event()
        received: dict[str, str] = {}

        def on_cancel(run_id: str, reason: str) -> None:
            received["run_id"] = run_id
            received["reason"] = reason
            got.set()

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=on_cancel
        )
        try:
            host, port = server.address
            req = urllib.request.Request(
                f"http://{host}:{port}/c",
                data=b"not-json",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=2.0)
            assert resp.status == 202
            assert got.wait(timeout=2.0)
            assert received == {"run_id": "", "reason": ""}
        finally:
            server.close()

    def test_handler_exception_returns_500(self):
        """If ``on_cancel`` raises, the HTTP client should see a 500 so
        the control-plane can retry or alert; the server must keep
        running for subsequent requests."""

        def on_cancel(run_id: str, reason: str) -> None:
            raise RuntimeError("boom")

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=on_cancel
        )
        try:
            host, port = server.address
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"http://{host}:{port}/c", {"runId": "x"})
            assert exc_info.value.code == 500
        finally:
            server.close()

    def test_close_is_idempotent_and_stops_thread(self):
        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=lambda *_: None
        )
        server.close()
        # Second close must not raise — common shutdown ordering.
        server.close()

    def test_path_without_leading_slash_normalises(self):
        got = threading.Event()

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="cancel", on_cancel=lambda *_: got.set()
        )
        try:
            host, port = server.address
            resp = _post(f"http://{host}:{port}/cancel", {"runId": "x"})
            assert resp.status == 202
            assert got.wait(timeout=2.0)
        finally:
            server.close()

    def test_concurrent_close_calls_do_not_raise(self):
        """Phase 6 gives ``Participant`` two independent call sites that
        can both invoke close() on the same bound server -- stop() and
        run()'s finally -- and its docstring states either may run
        first, or both. Prove concurrent close() calls from different
        threads are safe, not just sequential ones (the idempotency
        test above only proves the single-thread case)."""
        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=lambda *_: None
        )
        errors: list[Exception] = []

        def _close() -> None:
            try:
                server.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_close) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)

        assert not errors
        assert all(not t.is_alive() for t in threads)

    def test_close_from_within_on_cancel_does_not_deadlock(self):
        """close()'s self-deadlock-avoidance branch (only joining the
        shutdown thread when not called from the server's own thread,
        per its own docstring) is exercised when on_cancel itself
        closes the server synchronously -- as a handler that tears
        everything down in one call might."""
        done = threading.Event()

        def on_cancel(run_id: str, reason: str) -> None:
            server.close()
            done.set()

        server = start_cancel_callback_server(
            host="127.0.0.1", port=0, path="/c", on_cancel=on_cancel
        )
        host, port = server.address
        resp = _post(f"http://{host}:{port}/c", {"runId": "x"})
        assert resp.status == 202
        assert done.wait(timeout=2.0), "on_cancel (and its close()) never completed"
