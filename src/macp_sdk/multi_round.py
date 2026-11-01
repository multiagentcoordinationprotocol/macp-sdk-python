"""Session helper and projection for the ``ext.multi_round.v1`` extension mode.

The mode's normative semantics live in a comment block in
``macp/modes/multi_round/v1/multi_round.proto`` (spec #241); this module is
client sugar over them and mirrors macp-sdk-typescript's ``multi-round``
module name for name. Declared participants ``Contribute`` string values
until every latest value is equal (``all_equal``, sticky); the session then
stays OPEN until its initiator commits. The runtime decides: the projection's
convergence is advisory, and nothing here evaluates governance policy.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from google.protobuf.message import DecodeError  # type: ignore[import-untyped]
from macp.modes.multi_round.v1 import multi_round_pb2
from macp.v1 import core_pb2, envelope_pb2

from .async_session import AsyncBaseSession
from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession, _SessionCore
from .constants import MODE_MULTI_ROUND
from .envelope import build_commitment_payload, build_contribute_payload
from .errors import MacpSessionError
from .validation import validate_required_field

__all__ = [
    "MULTI_ROUND_CONVERGED_ACTION",
    "MULTI_ROUND_MODE_VERSION",
    "AsyncMultiRoundSession",
    "MultiRoundProjection",
    "MultiRoundSession",
]

MULTI_ROUND_MODE_VERSION = "1.0.0"
"""The only ``mode_version`` the runtime accepts for this mode."""

MULTI_ROUND_CONVERGED_ACTION = "multi_round.converged"
"""The conventional Commitment action; it matches no outcome suffix."""

# RFC-MACP-0001 §7.3 as macp-runtime enforces it (``mode/util.rs``
# ``validate_outcome_positive``): a case-sensitive suffix match.
_NEGATIVE_SUFFIXES = ("rejected", "failed", "declined")
_POSITIVE_SUFFIXES = ("selected", "accepted", "completed", "approved")


class _NotContributeJsonError(Exception):
    """The bytes are not JSON that serde reads as ``ContributeJson``."""


def _reject_constant(name: str) -> Any:
    raise _NotContributeJsonError(name)  # NaN / Infinity are not JSON to serde


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _NotContributeJsonError("duplicate key")  # serde refuses a duplicate field
    return dict(pairs)


def _check_strings(obj: Any) -> None:
    # serde_json refuses a lone-surrogate escape anywhere in the document.
    if isinstance(obj, str):
        obj.encode("utf-8")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            key.encode("utf-8")
            _check_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            _check_strings(item)


def _json_value(payload: bytes) -> str | None:
    """The ``value`` of legacy JSON exactly as the runtime's serde reads it."""
    try:
        obj = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
        _check_strings(obj)
    except (_NotContributeJsonError, UnicodeError, ValueError, RecursionError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("value"), str):
        value: str = obj["value"]
        return value
    return None


def _contribute_value(payload: bytes) -> str | None:
    """The contributed value, or ``None`` when nothing decodes.

    Mirrors the runtime's ``parse_contribute_value`` at ``semantics_rev`` 3:
    JSON is trusted only when serde would read it as ``{"value": <string>}``
    (an object, no duplicate keys, no lone surrogate, no NaN) and the bytes
    are not also the canonical protobuf encoding; otherwise the bytes decode
    as protobuf. ``None`` (a no-op for the projection) covers an empty
    payload and bytes that are neither: the proto lets a history reader
    return "nothing decodable" rather than refuse.
    """
    if not payload:
        return None
    try:
        proto: multi_round_pb2.ContributePayload | None = (
            multi_round_pb2.ContributePayload.FromString(payload)
        )
    except DecodeError:
        proto = None
    from_json = _json_value(payload)
    if from_json is not None:
        # Re-encode from the value alone: prost drops unknown fields, and a
        # payload carrying any is not canonical.
        canonical = (
            proto is not None
            and multi_round_pb2.ContributePayload(value=proto.value).SerializeToString() == payload
        )
        if not canonical:
            return from_json
    return proto.value if proto is not None else None


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class MultiRoundProjection(BaseProjection):
    """In-process state tracking for ``ext.multi_round.v1`` sessions.

    Mirrors the runtime's ``handle_contribute``: a sender's first value, or a
    value that differs from its previous one, advances ``round`` by one;
    re-sending the same value changes nothing. The session has converged once
    every declared participant has contributed and all latest values are
    equal; convergence is sticky, and later Contributes are no-ops.

    ``participants`` and ``initiator`` are learned from the accepted
    SessionStart. Pass them to the constructor to seed a projection that never
    sees one (a participant joining mid-session); a later SessionStart
    overwrites both. Convergence is evaluated only once participants are
    known.

    Phases: ``"Pending"`` → ``"Contributing"`` (SessionStart) →
    ``"Converged"`` → ``"Committed"``.

    A session's own projection is fed the envelopes the session built, so it
    attributes them only when the sender resolves locally: pass ``sender=`` or
    give a bearer ``AuthConfig`` a ``sender_hint``. With neither, the runtime
    still accepts the messages (it stamps the authenticated sender), but this
    projection drops the unattributed Contributes as non-participant ones.
    Subscribing to the session stream does not have this limit.

    Limitation: the projection cannot see a session's ``semantics_rev``, so a
    session the runtime recorded below revision 3 that carried a canonical
    proto value of byte length 13, 32 or 123 decodes here under the
    tie-break, not the JSON-first reading it was accepted with.
    """

    MODE = MODE_MULTI_ROUND

    def __init__(
        self,
        *,
        participants: Sequence[str] | None = None,
        initiator: str | None = None,
    ) -> None:
        super().__init__()
        self.phase = "Pending"
        self.participants: list[str] = list(participants or [])
        self.initiator: str | None = initiator
        self.contributions: dict[str, str] = {}
        self.round = 0
        self.converged = False
        self.converged_value: str | None = None

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "SessionStart":
            p = core_pb2.SessionStartPayload()
            p.ParseFromString(envelope.payload)
            self.participants = list(p.participants)
            # A locally built envelope under bearer auth without a sender hint
            # has an empty sender; the runtime stamps the real one only on its
            # copy, so keep what is known.
            if envelope.sender:
                self.initiator = envelope.sender
            self._set_phase("Contributing")
            return

        if mt == "Contribute":
            value = _contribute_value(envelope.payload)
            if value is None or self.converged:
                return
            # authorize_sender: only a declared participant may contribute.
            if self.participants and envelope.sender not in self.participants:
                return
            if self.contributions.get(envelope.sender) != value:
                self.round += 1
                self.contributions[envelope.sender] = value
            self._check_convergence()

    def _check_convergence(self) -> None:
        if not self.participants:
            return
        if any(p not in self.contributions for p in self.participants):
            return
        values = {self.contributions[p] for p in self.participants}
        if len(values) == 1:
            self.converged = True
            self.converged_value = values.pop()
            self._set_phase("Converged")

    def is_participant(self, sender: str) -> bool:
        """Whether *sender* may contribute; ``True`` while participants are unknown."""
        return not self.participants or sender in self.participants


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class _MultiRoundActions(_SessionCore):
    """Envelope builders shared by :class:`MultiRoundSession` and
    :class:`AsyncMultiRoundSession`.

    Each builder refuses locally what the runtime would reject, as far as the
    local projection can tell, and returns the envelope to send.
    """

    MODE = MODE_MULTI_ROUND

    def _create_projection(self) -> BaseProjection:
        return MultiRoundProjection()

    def _multi_round_state(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    def _start_envelope(
        self,
        *,
        intent: str,
        participants: list[str],
        ttl_ms: int,
        context_id: str,
        extensions: Mapping[str, bytes] | None,
        roots: Iterable[Any] | None,
        max_suspend_ms: int,
        sender: str | None,
        auth: AuthConfig | None,
    ) -> envelope_pb2.Envelope:
        if self.mode_version != MULTI_ROUND_MODE_VERSION:
            raise MacpSessionError(
                f"ext.multi_round.v1 requires mode_version {MULTI_ROUND_MODE_VERSION!r}, "
                f"not {self.mode_version!r}"
            )
        return super()._start_envelope(
            intent=intent,
            participants=participants,
            ttl_ms=ttl_ms,
            context_id=context_id,
            extensions=extensions,
            roots=roots,
            max_suspend_ms=max_suspend_ms,
            sender=sender,
            auth=auth,
        )

    def _contribute_envelope(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Envelope:
        if not value:
            raise MacpSessionError(
                "contribution value must be non-empty: an empty value encodes to zero bytes, "
                "which the runtime rejects"
            )
        projection = self._multi_round_state()
        if projection.converged:
            raise MacpSessionError("the session has converged; the runtime rejects any Contribute")
        resolved = self._sender_for(sender, auth=auth)
        if resolved and not projection.is_participant(resolved):
            raise MacpSessionError(
                f"{resolved!r} is not a declared participant; the runtime rejects its Contribute"
            )
        return self._envelope(
            "Contribute", build_contribute_payload(value), sender=sender, auth=auth
        )

    def _multi_round_commit_envelope(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str,
        commitment_id: str | None,
        outcome_positive: bool | None,
        supersedes: core_pb2.CommitmentRef | None,
        sender: str | None,
        auth: AuthConfig | None,
    ) -> envelope_pb2.Envelope:
        initiator = self._multi_round_state().initiator
        resolved = self._sender_for(sender, auth=auth)
        if initiator and resolved and resolved != initiator:
            raise MacpSessionError(
                f"only the session initiator {initiator!r} may commit, not {resolved!r}"
            )
        if commitment_id is not None:
            validate_required_field("commitment_id", commitment_id)
        validate_required_field("action", action)
        validate_required_field("authority_scope", authority_scope)
        validate_required_field("reason", reason)
        if outcome_positive is not None:
            if outcome_positive and action.endswith(_NEGATIVE_SUFFIXES):
                raise MacpSessionError(f"action {action!r} has a negative suffix")
            if not outcome_positive and action.endswith(_POSITIVE_SUFFIXES):
                raise MacpSessionError(f"action {action!r} has a positive suffix")
        if supersedes is not None:
            validate_required_field("supersedes.session_id", supersedes.session_id)
        payload = build_commitment_payload(
            action=action,
            authority_scope=authority_scope,
            reason=reason,
            commitment_id=commitment_id,
            mode_version=self.mode_version,
            configuration_version=self.configuration_version,
            policy_version=self.policy_version,
            outcome_positive=outcome_positive,
            supersedes=supersedes,
        )
        return self._envelope("Commitment", payload, sender=sender, auth=auth)


class MultiRoundSession(BaseSession, _MultiRoundActions):
    """High-level helper for ``ext.multi_round.v1`` sessions.

    ``start()`` refuses a ``mode_version`` other than ``"1.0.0"``.
    ``contribute()`` refuses an empty value, a converged session and a sender
    the projection knows is not a declared participant. ``commit()`` defaults
    ``action`` to ``"multi_round.converged"`` and refuses a sender other than
    the known initiator; it does not wait for convergence, which the runtime
    checks.
    """

    MODE = MODE_MULTI_ROUND

    @property
    def multi_round_projection(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    def contribute(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Contribute *value* (compared for equality only, never interpreted)."""
        return self._send_and_track(
            self._contribute_envelope(value, sender=sender, auth=auth), auth=auth
        )

    def commit(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str = MULTI_ROUND_CONVERGED_ACTION,
        commitment_id: str | None = None,
        outcome_positive: bool | None = None,
        supersedes: core_pb2.CommitmentRef | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send the Commitment that resolves the session (initiator only)."""
        return self._send_and_track(
            self._multi_round_commit_envelope(
                authority_scope=authority_scope,
                reason=reason,
                action=action,
                commitment_id=commitment_id,
                outcome_positive=outcome_positive,
                supersedes=supersedes,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )


class AsyncMultiRoundSession(AsyncBaseSession, _MultiRoundActions):
    """Asyncio twin of :class:`MultiRoundSession`, driven by an
    :class:`~macp_sdk.async_client.AsyncMacpClient`.
    """

    MODE = MODE_MULTI_ROUND

    @property
    def multi_round_projection(self) -> MultiRoundProjection:
        assert isinstance(self.projection, MultiRoundProjection)
        return self.projection

    async def contribute(
        self,
        value: str,
        *,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Contribute *value* (compared for equality only, never interpreted)."""
        return await self._send_and_track(
            self._contribute_envelope(value, sender=sender, auth=auth), auth=auth
        )

    async def commit(
        self,
        *,
        authority_scope: str,
        reason: str,
        action: str = MULTI_ROUND_CONVERGED_ACTION,
        commitment_id: str | None = None,
        outcome_positive: bool | None = None,
        supersedes: core_pb2.CommitmentRef | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send the Commitment that resolves the session (initiator only)."""
        return await self._send_and_track(
            self._multi_round_commit_envelope(
                authority_scope=authority_scope,
                reason=reason,
                action=action,
                commitment_id=commitment_id,
                outcome_positive=outcome_positive,
                supersedes=supersedes,
                sender=sender,
                auth=auth,
            ),
            auth=auth,
        )
