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
from ..errors import MacpSdkError, MacpTransportError
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
        # Count of distinct envelopes actually handed to the consumer
        # (incremented only after each is yielded) — the resume cursor
        # passed as ``after_sequence`` on the next ``send_subscribe``.
        # Mirrors typescript-sdk's ``delivered`` counter (transports.ts:58).
        # Per-adapter-instance, not per-session: a fresh instance (e.g. one
        # ``Participant.run()`` constructs anew on restart) starts at 0 —
        # this only carries over across a reconnect that reuses the *same*
        # adapter instance. The only reconnect path today (the bounded
        # NOT_FOUND retry in start(), below) never fires after anything has
        # been delivered, so it does not exercise this counter in practice.
        # Incrementing after yield (rather than before) means a consumer
        # that abandons iteration between receiving an envelope and asking
        # for the next one (e.g. breaking out of a for-loop) leaves this
        # envelope uncounted — a same-instance restart would then resume one
        # envelope early (a harmless duplicate; BaseProjection dedups on
        # message_id), never a gap. Matches typescript-sdk's own counter.
        self._delivered = 0
        # A separate, unrelated monotonic counter feeding IncomingMessage.seq
        # on the gRPC path — not the resume cursor above, and not asserted to
        # stay equal to it (mirrors typescript-sdk's own ``seq`` field,
        # transports.ts:57, distinct from its ``delivered`` counter).
        self._seq = 0

    @property
    def last_sequence(self) -> int:
        """Count of distinct envelopes yielded so far (the resume cursor).

        Parity with typescript-sdk's public ``delivered`` getter.
        """
        return self._delivered

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
                # #80: recheck immediately after assignment. stop()/cancel()
                # set self._stopped before reading self._stream; this line
                # assigns self._stream before reading self._stopped -- so at
                # least one side always observes the other's write, closing
                # the window without a lock (see cancel()/stop() below for
                # the matching local-bind fix to the read side).
                if self._stopped:
                    self._stream.cancel()
                    return
                # RFC-MACP-0006-A1: Subscribe to the session with history replay.
                # The runtime replays accepted envelopes then switches to live
                # broadcast, ensuring non-initiator agents receive SessionStart +
                # Proposal regardless of spawn order or connection timing.
                # after_sequence resumes from self._delivered rather than
                # always 0, so a reconnect on this same adapter instance
                # doesn't replay envelopes already handed to the consumer.
                self._stream.send_subscribe(self._session_id, after_sequence=self._delivered)

                for envelope in self._stream.responses():
                    received_any = True
                    if self._stopped:
                        return
                    if envelope.session_id != self._session_id:
                        continue
                    self._seq += 1
                    yield _envelope_to_message(envelope, seq=self._seq)
                    self._delivered += 1
                return
            except MacpTransportError as exc:
                if self._stopped:
                    # An intentional stop()/cancel() (possibly from another
                    # thread while blocked in responses()) surfaces here as
                    # a transport error -- e.g. MacpTransportError(code=
                    # "CANCELLED") per MacpStream.cancel()'s own docs. That's
                    # an expected clean shutdown, not a failure to propagate.
                    return
                retry = self._subscribe_retry
                if received_any or exc.code != "NOT_FOUND" or attempt >= retry.max_retries:
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
                # #80 (known, accepted gap): a cancel()/stop() landing here
                # isn't woken early -- this sleep isn't a threading.Event.wait,
                # so it can't be interrupted. Fixing that needs a _stop_event
                # and touches tests that poke self._stopped directly as a raw
                # bool; deferred as a separate, larger change (see the plan's
                # Phase 2 Edge cases).
                time.sleep(delay)
            except MacpSdkError:
                if self._stopped:
                    # #89: send_subscribe()'s own already-closed check
                    # (client.py's MacpStream.send_subscribe) raises
                    # MacpSdkError directly, not MacpTransportError, when a
                    # cancel() lands between the #80 recheck above and this
                    # call -- same clean shutdown as the MacpTransportError
                    # case above, just a different sibling exception type.
                    # Narrow: every other call in this try block either
                    # raises MacpTimeoutError/MacpTransportError (already
                    # caught above -- read()/responses(), client.py:213-230)
                    # or a non-MacpSdkError type (_stream.cancel() raises
                    # gRPC errors; _envelope_to_message raises ValueError),
                    # so this clause is reached only by send_subscribe()'s
                    # already-closed check, not a blanket MacpSdkError catch.
                    return
                raise
            finally:
                if self._stream is not None:
                    self._stream.close()
                    self._stream = None

    def stop(self) -> None:
        self._stopped = True
        # Bind to a local before checking: self._stream is read once here
        # rather than twice, so a concurrent finally-block reassignment
        # (start()'s own finally, on another thread) can't null it out
        # between the None-check and the call.
        stream = self._stream
        if stream is not None:
            stream.close()

    def cancel(self) -> None:
        """Immediately abort a blocked stream read, safe to call from
        another thread.

        Unlike :meth:`stop` (which half-closes via :meth:`MacpStream.close`
        and is only checked cooperatively between yielded messages -- a
        read blocked on an idle stream with nothing pending won't return
        until the next message arrives or the stream ends), this cancels
        the underlying gRPC call via :meth:`MacpStream.cancel` so a
        :meth:`start` iterator parked inside a blocked read unblocks right
        away. Feature-detected by ``Participant.stop()`` via ``getattr``,
        so a custom :class:`TransportAdapter` without cancellation support
        just falls back to :meth:`stop`. Idempotent, and safe to call
        before :meth:`start` has ever run (no-op beyond setting the
        stopped flag).
        """
        self._stopped = True
        # See stop()'s comment above -- same single-read local-bind fix.
        stream = self._stream
        if stream is not None:
            stream.cancel()


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

                # Accept both a bare JSON array and typescript-sdk's
                # {"events": [...]} wrapper shape (transports.ts:288-304).
                events = data.get("events") if isinstance(data, dict) else data
                if isinstance(events, list):
                    for item in events:
                        seq = item.get("seq", self._last_seq + 1)
                        if seq > self._last_seq:
                            self._last_seq = seq
                        yield IncomingMessage(
                            message_type=item.get("message_type", ""),
                            sender=item.get("sender", ""),
                            payload=_decode_http_payload(item.get("payload", {})),
                            proposal_id=item.get("proposal_id"),
                            seq=seq,
                        )
            except Exception:
                logger.debug("http poll error, retrying in %ss", self._poll_interval)

            if not self._stopped:
                time.sleep(self._poll_interval)

    def stop(self) -> None:
        self._stopped = True


def _decode_http_payload(value: Any) -> dict[str, Any]:
    """Normalize an HTTP-polled event's ``payload`` field to a dict.

    Mirrors typescript-sdk's ``tryParsePayload`` (transports.ts:318-325):
    a string/bytes payload is JSON-decoded; anything that isn't already a
    dict, or that fails to decode to one, safely falls back to ``{}`` rather
    than breaking ``IncomingMessage.payload``'s ``dict[str, Any]`` contract.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _envelope_to_message(envelope: Any, *, seq: int | None = None) -> IncomingMessage:
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
        seq=seq,
    )
