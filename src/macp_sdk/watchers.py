"""High-level watcher classes wrapping raw client streaming RPCs.

Each watcher provides three consumption patterns:
- ``changes()`` / ``signals()`` — a Python iterator
- ``watch(handler)`` — blocking callback loop
- ``next_change()`` / ``next_signal()`` — pull a single item
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import MacpTransportError

if TYPE_CHECKING:
    from .auth import AuthConfig
    from .client import MacpClient


@dataclass(slots=True)
class PolicyChange:
    """A snapshot of governance policy changes from the runtime."""

    descriptors: list[Any] = field(default_factory=list)
    observed_at_unix_ms: int = 0


TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES: frozenset[str] = frozenset(
    {"RESOLVED", "EXPIRED", "CANCELLED"}
)
"""Event-type names after which a session emits no further lifecycle events.

``SUSPENDED`` / ``RESUMED`` are deliberately absent: a suspended session
can still be resumed, so neither is terminal.

Cross-SDK note -- read this before comparing anything against it.
These are the proto enum names with the ``EVENT_TYPE_`` prefix
**stripped** (see ``_session_event_name``), which is the shape
``SessionLifecycleEvent.event_type`` carries in this SDK.
``macp-sdk-typescript`` exports a differently-shaped set under the
confusingly similar name ``TERMINAL_SESSION_LIFECYCLE_EVENT_TYPES``
(``src/watchers.ts:20-24``), holding the **un-stripped** names
(``"EVENT_TYPE_RESOLVED"``). This constant is named ``..._NAMES``
rather than ``..._TYPES`` precisely so the two cannot be mistaken for
the same contract. The value difference is deliberate and will not be
reconciled by changing Python: the stripped form is this SDK's
published field *value*, so changing it would break every
``event_type == "RESOLVED"`` call site with no deprecation path
available (a value, unlike a name, cannot carry a warning). To test a
prefixed string from a TypeScript-written log, normalise it first:
``t.removeprefix("EVENT_TYPE_") in TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES``.
"""


@dataclass(slots=True)
class SessionLifecycleEvent:
    """A single session lifecycle event from ``WatchSessions``.

    Runtime event types (per the wire message's own
    ``SessionLifecycleEvent.EventType`` enum, since macp-proto 0.1.3):
    ``CREATED`` on SessionStart acceptance (also emitted
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
        emit more events. ``SUSPENDED`` / ``RESUMED`` are non-terminal.

        See ``TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES`` for the exported,
        reusable form of this set.
        """
        return self.event_type in TERMINAL_SESSION_LIFECYCLE_EVENT_NAMES


class ModeRegistryWatcher:
    """Watch for mode registry changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Generator[Any, None, None]:
        """Yield ``WatchModeRegistryResponse`` items from the runtime stream."""
        yield from self._client.watch_mode_registry(auth=self._auth)

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each registry change."""
        with closing(self.changes()) as stream:
            for change in stream:
                handler(change)

    def next_change(self) -> Any:
        """Pull a single change from the stream and return it."""
        with closing(self.changes()) as stream:
            for change in stream:
                return change
        raise MacpTransportError("stream ended before receiving a change")


class RootsWatcher:
    """Watch for root changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Generator[Any, None, None]:
        """Yield ``WatchRootsResponse`` items from the runtime stream."""
        yield from self._client.watch_roots(auth=self._auth)

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each root change."""
        with closing(self.changes()) as stream:
            for change in stream:
                handler(change)

    def next_change(self) -> Any:
        """Pull a single change from the stream and return it."""
        with closing(self.changes()) as stream:
            for change in stream:
                return change
        raise MacpTransportError("stream ended before receiving a change")


class SignalWatcher:
    """Watch for ambient signal envelopes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def signals(self) -> Generator[Any, None, None]:
        """Yield envelope objects extracted from ``WatchSignalsResponse``.

        Forwards the watcher's stored ``auth`` — runtime v0.5.0 requires
        authentication for ``WatchSignals``.

        Wraps the inner ``watch_signals()`` generator in its own
        ``closing()`` -- a bare ``for`` loop here would only release it
        by refcounting when this generator is itself closed, which a
        caller-held reference cycle (or a non-refcounting GC) can defer
        indefinitely, undermining the whole point of the caller side
        wrapping *this* generator in ``closing()`` in turn.
        """
        with closing(self._client.watch_signals(auth=self._auth)) as responses:
            for response in responses:
                if hasattr(response, "envelope") and response.envelope.ByteSize() > 0:
                    yield response.envelope

    def watch(self, handler: Callable[[Any], None]) -> None:
        """Block and invoke *handler* for each signal envelope."""
        with closing(self.signals()) as stream:
            for envelope in stream:
                handler(envelope)

    def next_signal(self) -> Any:
        """Pull a single signal envelope from the stream and return it."""
        with closing(self.signals()) as stream:
            for envelope in stream:
                return envelope
        raise MacpTransportError("stream ended before receiving a signal")


_SESSION_EVENT_PREFIX = "EVENT_TYPE_"


def _session_event_name(event_type: int) -> str:
    """Map the wire message's ``SessionLifecycleEvent.EventType`` enum ints to
    short string names.

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
    a ``SessionLifecycleEvent`` record carrying the event type as a short
    string (``CREATED`` / ``RESOLVED`` / ``EXPIRED`` / ``CANCELLED`` /
    ``SUSPENDED`` / ``RESUMED``) and the full ``SessionMetadata``. The
    runtime emits an initial CREATED event for every session currently in
    its registry at subscribe time -- regardless of state, so a terminal
    session still within its eviction window arrives as CREATED too, with
    the real state readable from the event's ``session.state`` -- then live
    events thereafter. See ``runtime/src/server.rs::watch_sessions`` and
    ``runtime/src/watch_sync.rs``.
    """

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Generator[SessionLifecycleEvent, None, None]:
        """Yield ``SessionLifecycleEvent`` items from the runtime stream.

        Wraps the inner ``watch_sessions()`` generator in its own
        ``closing()`` -- see ``SignalWatcher.signals()``'s docstring for
        why a bare ``for`` loop here isn't enough.
        """
        with closing(self._client.watch_sessions(auth=self._auth)) as responses:
            for response in responses:
                event = getattr(response, "event", None)
                if event is None:
                    continue
                yield SessionLifecycleEvent(
                    event_type=_session_event_name(event.event_type),
                    observed_at_unix_ms=event.observed_at_unix_ms,
                    session=event.session,
                )

    def watch(self, handler: Callable[[SessionLifecycleEvent], None]) -> None:
        """Block and invoke *handler* for each lifecycle event."""
        with closing(self.changes()) as stream:
            for change in stream:
                handler(change)

    def next_change(self) -> SessionLifecycleEvent:
        """Pull a single lifecycle event from the stream and return it."""
        with closing(self.changes()) as stream:
            for change in stream:
                return change
        raise MacpTransportError("stream ended before receiving a session lifecycle event")


class PolicyWatcher:
    """Watch for governance policy changes from the runtime."""

    def __init__(self, client: MacpClient, *, auth: AuthConfig | None = None) -> None:
        self._client = client
        self._auth = auth

    def changes(self) -> Generator[PolicyChange, None, None]:
        """Yield ``PolicyChange`` items from the runtime stream.

        Wraps the inner ``watch_policies()`` generator in its own
        ``closing()`` -- see ``SignalWatcher.signals()``'s docstring for
        why a bare ``for`` loop here isn't enough.
        """
        with closing(self._client.watch_policies(auth=self._auth)) as responses:
            for response in responses:
                descriptors = list(response.descriptors) if hasattr(response, "descriptors") else []
                observed = getattr(response, "observed_at_unix_ms", 0)
                yield PolicyChange(descriptors=descriptors, observed_at_unix_ms=observed)

    def watch(self, handler: Callable[[PolicyChange], None]) -> None:
        """Block and invoke *handler* for each policy change."""
        with closing(self.changes()) as stream:
            for change in stream:
                handler(change)

    def next_change(self) -> PolicyChange:
        """Pull a single policy change from the stream and return it."""
        with closing(self.changes()) as stream:
            for change in stream:
                return change
        raise MacpTransportError("stream ended before receiving a policy change")


# ── Deprecated aliases (issue #103 / multiagentcoordinationprotocol#135) ─────
#
# ``SessionLifecycle`` is the pre-rename name, kept as a module-level lazy
# alias (PEP 562) for one minor version, removed at this SDK's next major.
# Same mechanism and reasoning as ``agent/strategies.py``'s own alias dict --
# see the comment there for the full rationale. This module has no
# ``__path__`` (a plain file, not a package), so
# ``from macp_sdk.watchers import SessionLifecycle`` resolves via a single
# ``getattr`` call -- the warning fires exactly once per such import.
_DEPRECATED_ALIASES = {
    "SessionLifecycle": "SessionLifecycleEvent",
}


def __getattr__(name: str) -> Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
