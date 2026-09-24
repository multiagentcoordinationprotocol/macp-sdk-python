"""Transport adapters for the agent event loop.

Provides a :class:`TransportAdapter` protocol and two implementations:

- :class:`GrpcTransportAdapter` — uses a bidirectional ``StreamSession`` RPC.
- :class:`HttpTransportAdapter` — polls an HTTP endpoint for new envelopes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any, Protocol

from .._logging import logger
from ..auth import AuthConfig
from ..client import MacpClient
from ..errors import MacpTransportError
from ..retry import RetryPolicy
from .types import IncomingMessage


class TransportAdapter(Protocol):
    """Protocol for delivering session envelopes to a Participant."""

    def start(self) -> Iterator[IncomingMessage]:
        """Yield incoming messages from the transport."""
        ...

    def stop(self) -> None:
        """Signal the transport to stop delivering messages."""
        ...


class GrpcTransportAdapter:
    """Delivers messages via the bidirectional ``StreamSession`` gRPC RPC."""

    def __init__(
        self,
        client: MacpClient,
        session_id: str,
        *,
        auth: AuthConfig | None = None,
        timeout: float | None = None,
        subscribe_retry: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._auth = auth
        self._timeout = timeout
        self._stream: Any = None
        self._stopped = False
        self._subscribe_retry = subscribe_retry or RetryPolicy()

    def start(self) -> Iterator[IncomingMessage]:
        """Open a stream and yield messages for the target session.

        A subscribe issued before a sibling participant's ``SessionStart``
        has reached the runtime is a normal startup race, not a fatal
        error — the runtime returns a transient ``NOT_FOUND`` for a session
        it hasn't created yet (#75). That specific case is retried with
        backoff (``subscribe_retry``) before giving up; once any envelope
        has been delivered the session demonstrably exists, so a later
        ``NOT_FOUND`` is raised immediately instead of retried.
        """
        attempt = 0
        received_any = False
        while True:
            if self._stopped:
                return
            self._stream = self._client.open_stream(auth=self._auth, timeout=self._timeout)
            try:
                # RFC-MACP-0006-A1: Subscribe to the session with history replay.
                # The runtime replays accepted envelopes then switches to live
                # broadcast, ensuring non-initiator agents receive SessionStart +
                # Proposal regardless of spawn order or connection timing.
                self._stream.send_subscribe(self._session_id)

                for envelope in self._stream.responses():
                    received_any = True
                    if self._stopped:
                        return
                    if envelope.session_id != self._session_id:
                        continue
                    yield _envelope_to_message(envelope)
                return
            except MacpTransportError as exc:
                retry = self._subscribe_retry
                if (
                    received_any
                    or self._stopped
                    or exc.code != "NOT_FOUND"
                    or attempt >= retry.max_retries
                ):
                    raise
                delay = min(retry.backoff_base * (2**attempt), retry.backoff_max)
                logger.debug(
                    "session %r not found yet (subscribe attempt %d/%d), retrying in %.2fs",
                    self._session_id,
                    attempt + 1,
                    retry.max_retries,
                    delay,
                )
                attempt += 1
                time.sleep(delay)
            finally:
                if self._stream is not None:
                    self._stream.close()
                    self._stream = None

    def stop(self) -> None:
        self._stopped = True
        if self._stream is not None:
            self._stream.close()


class HttpTransportAdapter:
    """Delivers messages by polling an HTTP endpoint for new envelopes.

    Expects the endpoint to return a JSON array of envelope objects at
    ``GET {base_url}/sessions/{session_id}/events?after={last_seq}``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        participant_id: str,
        poll_interval_ms: int = 1000,
        auth_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id
        self._participant_id = participant_id
        self._poll_interval = poll_interval_ms / 1000.0
        self._auth_token = auth_token
        self._stopped = False
        self._last_seq = -1

    def start(self) -> Iterator[IncomingMessage]:
        """Poll the HTTP endpoint and yield messages."""
        import urllib.request

        url = f"{self._base_url}/sessions/{self._session_id}/events"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        while not self._stopped:
            try:
                req_url = f"{url}?after={self._last_seq}"
                req = urllib.request.Request(req_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())

                if isinstance(data, list):
                    for item in data:
                        seq = item.get("seq", self._last_seq + 1)
                        if seq > self._last_seq:
                            self._last_seq = seq
                        yield IncomingMessage(
                            message_type=item.get("message_type", ""),
                            sender=item.get("sender", ""),
                            payload=item.get("payload", {}),
                            proposal_id=item.get("proposal_id"),
                            seq=seq,
                        )
            except Exception:
                logger.debug("http poll error, retrying in %ss", self._poll_interval)

            if not self._stopped:
                time.sleep(self._poll_interval)

    def stop(self) -> None:
        self._stopped = True


def _envelope_to_message(envelope: Any) -> IncomingMessage:
    """Convert a protobuf Envelope to an IncomingMessage."""
    from ..proto_registry import ProtoRegistry

    payload_dict: dict[str, Any] = {}
    if envelope.payload:
        try:
            registry = ProtoRegistry()
            decoded = registry.decode_known_payload(
                envelope.mode, envelope.message_type, envelope.payload
            )
            payload_dict = decoded if decoded is not None else json.loads(envelope.payload)
        except Exception:
            try:
                payload_dict = json.loads(envelope.payload)
            except Exception:
                payload_dict = {}

    proposal_id: str | None = (
        payload_dict.get("proposal_id") or payload_dict.get("proposalId") or None
    )
    if proposal_id is not None:
        proposal_id = str(proposal_id)

    return IncomingMessage(
        message_type=envelope.message_type,
        sender=envelope.sender,
        payload=payload_dict,
        proposal_id=proposal_id,
        raw=envelope,
    )
