"""High-level watcher classes wrapping raw client streaming RPCs.

Each watcher provides three consumption patterns:
- ``changes()`` / ``signals()`` — a Python iterator
- ``watch(handler)`` — blocking callback loop
- ``next_change()`` / ``next_signal()`` — pull a single item
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auth import AuthConfig
    from .client import MacpClient


@dataclass(slots=True)
class PolicyChange:
    """A snapshot of governance policy changes from the runtime."""

    descriptors: list[Any] = field(default_factory=list)
    observed_at_unix_ms: int = 0


@dataclass(slots=True)
class SessionLifecycle:
    """A single session lifecycle event from ``WatchSessions``.

    Runtime event types (per ``SessionLifecycleEvent.EventType``, since
    macp-proto 0.1.3): ``CREATED`` on SessionStart acceptance (also emitted
    for pre-existing sessions at subscribe time), ``RESOLVED`` on
    mode-determined terminal outcome, ``EXPIRED`` on TTL/policy expiry,
    ``CANCELLED`` on an accepted ``CancelSession`` (previously surfaced as
    ``EXPIRED``), and the non-terminal pair ``SUSPENDED`` / ``RESUMED`` from
    ``SuspendSession`` / ``ResumeSession``.
    """

    event_type: str = "UNSPECIFIED"
    observed_at_unix_ms: int = 0
    session: Any = None

    @property
    def is_created(self) -> bool:
        return self.event_type == "CREATED"

    @property
    def is_resolved(self) -> bool:
        return self.event_type == "RESOLVED"

    @property
    def is_expired(self) -> bool:
        """``True`` only for TTL/policy expiry; explicit cancellation now
        surfaces as ``CANCELLED`` — see ``is_cancelled``."""
        return self.event_type == "EXPIRED"

    @property
    def is_cancelled(self) -> bool:
        """``True`` for an accepted ``CancelSession`` (terminal)."""
        return self.event_type == "CANCELLED"

    @property
    def is_suspended(self) -> bool:
        """``True`` after ``SuspendSession`` — non-terminal; the session can
        still ``RESUMED``."""
        return self.event_type == "SUSPENDED"

    @property
    def is_resumed(self) -> bool:
        """``True`` after ``ResumeSession`` returns a suspended session to OPEN."""
        return self.event_type == "RESUMED"

    @property
    def is_terminal(self) -> bool:
        """``True`` for RESOLVED, EXPIRED, or CANCELLED — the session won't
        emit more events. ``SUSPENDED`` / ``RESUMED`` are non-terminal."""
        return self.event_type in ("RESOLVED", "EXPIRED", "CANCELLED")


class ModeRegistryWatcher:
    """Watch for mode registry changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Iterator[Any]:
        """Yield ``WatchModeRegistryResponse`` items from the runtime stream."""
        yield from self._client.watch_mode_registry()

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each registry change."""
        for change in self.changes():
            handler(change)

    def next_change(self) -> Any:
        """Pull a single change from the stream and return it."""
        for change in self.changes():
            return change
        raise RuntimeError("stream ended before receiving a change")


class RootsWatcher:
    """Watch for root changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Iterator[Any]:
        """Yield ``WatchRootsResponse`` items from the runtime stream."""
        yield from self._client.watch_roots()

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each root change."""
        for change in self.changes():
            handler(change)

    def next_change(self) -> Any:
        """Pull a single change from the stream and return it."""
        for change in self.changes():
            return change
        raise RuntimeError("stream ended before receiving a change")


class SignalWatcher:
    """Watch for ambient signal envelopes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def signals(self) -> Iterator[Any]:
        """Yield envelope objects extracted from ``WatchSignalsResponse``."""
        for response in self._client.watch_signals():
            if hasattr(response, "envelope") and response.envelope.ByteSize() > 0:
                yield response.envelope

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each signal envelope."""
        for envelope in self.signals():
            handler(envelope)

    def next_signal(self) -> Any:
        """Pull a single signal envelope from the stream and return it."""
        for envelope in self.signals():
            return envelope
        raise RuntimeError("stream ended before receiving a signal")


_SESSION_EVENT_PREFIX = "EVENT_TYPE_"


def _session_event_name(event_type: int) -> str:
    """Map ``SessionLifecycleEvent.EventType`` enum ints to short string names.

    The proto enum spells values as ``EVENT_TYPE_CREATED``; strip the
    prefix so consumers can compare against ``"CREATED"`` without
    importing the proto module.
    """
    from macp.v1 import core_pb2

    name = core_pb2.SessionLifecycleEvent.EventType.Name(event_type)
    if name.startswith(_SESSION_EVENT_PREFIX):
        return name[len(_SESSION_EVENT_PREFIX) :]
    return name


class SessionLifecycleWatcher:
    """Watch for session lifecycle events from the runtime.

    Wraps ``MacpClient.watch_sessions()`` and normalises each response into
    a ``SessionLifecycle`` record carrying the event type as a short
    string (``CREATED`` / ``RESOLVED`` / ``EXPIRED`` / ``CANCELLED`` /
    ``SUSPENDED`` / ``RESUMED``) and the full ``SessionMetadata``. The
    runtime emits an initial CREATED event for every already-open session at
    subscribe time, then live events thereafter — see
    ``runtime/src/server.rs::watch_sessions``.
    """

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Iterator[SessionLifecycle]:
        """Yield ``SessionLifecycle`` items from the runtime stream."""
        for response in self._client.watch_sessions(auth=self._auth):
            event = getattr(response, "event", None)
            if event is None:
                continue
            yield SessionLifecycle(
                event_type=_session_event_name(event.event_type),
                observed_at_unix_ms=event.observed_at_unix_ms,
                session=event.session,
            )

    def watch(self, handler: Callable[[SessionLifecycle], None]) -> None:
        """Block and invoke *handler* for each lifecycle event."""
        for change in self.changes():
            handler(change)

    def next_change(self) -> SessionLifecycle:
        """Pull a single lifecycle event from the stream and return it."""
        for change in self.changes():
            return change
        raise RuntimeError("stream ended before receiving a session lifecycle event")


class PolicyWatcher:
    """Watch for governance policy changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Iterator[PolicyChange]:
        """Yield ``PolicyChange`` items from the runtime stream."""
        for response in self._client.watch_policies():
            descriptors = list(response.descriptors) if hasattr(response, "descriptors") else []
            observed = getattr(response, "observed_at_unix_ms", 0)
            yield PolicyChange(descriptors=descriptors, observed_at_unix_ms=observed)

    def watch(self, handler: Callable[[PolicyChange], None]) -> None:
        """Block and invoke *handler* for each policy change."""
        for change in self.changes():
            handler(change)

    def next_change(self) -> PolicyChange:
        """Pull a single policy change from the stream and return it."""
        for change in self.changes():
            return change
        raise RuntimeError("stream ended before receiving a policy change")
