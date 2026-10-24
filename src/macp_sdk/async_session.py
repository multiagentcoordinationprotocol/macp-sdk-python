"""Asyncio base class for mode session helpers.

:class:`AsyncBaseSession` is the ``async`` twin of
:class:`macp_sdk.base_session.BaseSession`. Both derive from the shared
``_SessionCore``, which owns validation, sender resolution and envelope
construction, so a sync and an async session given the same arguments send
the same envelope. Only the send path differs: here it awaits
:meth:`AsyncMacpClient.send` and then feeds the accepted envelope into the
projection.

The concrete mode helpers (``AsyncDecisionSession``, ``AsyncProposalSession``,
``AsyncTaskSession``, ``AsyncHandoffSession``, ``AsyncQuorumSession``) live
next to their sync counterparts in each mode module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from macp.v1 import core_pb2, envelope_pb2

from .async_client import AsyncMacpClient, AsyncMacpStream
from .auth import AuthConfig
from .base_session import _SessionCore
from .constants import (
    DEFAULT_CONFIGURATION_VERSION,
    DEFAULT_MODE_VERSION,
    DEFAULT_POLICY_VERSION,
)

__all__ = ["AsyncBaseSession"]


class AsyncBaseSession(_SessionCore):
    """Abstract base for asyncio mode session helpers.

    Same lifecycle surface as :class:`macp_sdk.base_session.BaseSession`
    (``start``, ``commit``, ``cancel``, ``suspend``, ``resume``,
    ``metadata``, ``open_stream``), with every runtime call awaited.
    """

    client: AsyncMacpClient

    def __init__(
        self,
        client: AsyncMacpClient,
        *,
        session_id: str | None = None,
        mode_version: str = DEFAULT_MODE_VERSION,
        configuration_version: str = DEFAULT_CONFIGURATION_VERSION,
        policy_version: str = DEFAULT_POLICY_VERSION,
        auth: AuthConfig | None = None,
    ) -> None:
        super().__init__(
            client,
            session_id=session_id,
            mode_version=mode_version,
            configuration_version=configuration_version,
            policy_version=policy_version,
            auth=auth,
        )

    async def _send_and_track(
        self,
        envelope: envelope_pb2.Envelope,
        *,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        self._log_send(envelope)
        ack = await self.client.send(envelope, auth=auth or self.auth)
        self._track_ack(envelope, ack)
        return ack

    async def start(
        self,
        *,
        intent: str,
        participants: list[str],
        ttl_ms: int,
        context_id: str = "",
        extensions: Mapping[str, bytes] | None = None,
        roots: Iterable[Any] | None = None,
        max_suspend_ms: int = 0,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send SessionStart and begin tracking via the projection.

        See :meth:`macp_sdk.base_session.BaseSession.start`.
        """
        envelope = self._start_envelope(
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
        return await self._send_and_track(envelope, auth=auth or self.auth)

    async def commit(
        self,
        *,
        action: str,
        authority_scope: str,
        reason: str,
        commitment_id: str | None = None,
        outcome_positive: bool | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send Commitment to resolve the session.

        See :meth:`macp_sdk.base_session.BaseSession.commit`.
        """
        envelope = self._commit_envelope(
            action=action,
            authority_scope=authority_scope,
            reason=reason,
            commitment_id=commitment_id,
            outcome_positive=outcome_positive,
            sender=sender,
            auth=auth,
        )
        return await self._send_and_track(envelope, auth=auth)

    async def metadata(self, *, auth: AuthConfig | None = None) -> core_pb2.GetSessionResponse:
        """Query session metadata from the runtime."""
        return await self.client.get_session(self.session_id, auth=auth or self.auth)

    async def cancel(self, *, reason: str = "", auth: AuthConfig | None = None) -> envelope_pb2.Ack:
        """Cancel the session (terminates as ``CANCELLED``)."""
        return await self.client.cancel_session(
            self.session_id, reason=reason, auth=auth or self.auth
        )

    async def suspend(
        self, *, reason: str = "", auth: AuthConfig | None = None
    ) -> envelope_pb2.Ack:
        """Suspend this session (non-terminal ``SUSPENDED``) until :meth:`resume`."""
        return await self.client.suspend_session(
            self.session_id, reason=reason, auth=auth or self.auth
        )

    async def resume(self, *, reason: str = "", auth: AuthConfig | None = None) -> envelope_pb2.Ack:
        """Resume this suspended session, restoring ``OPEN``."""
        return await self.client.resume_session(
            self.session_id, reason=reason, auth=auth or self.auth
        )

    def open_stream(self, *, auth: AuthConfig | None = None) -> AsyncMacpStream:
        """Open a bidirectional stream for this session (call from a running loop)."""
        return self.client.open_stream(auth=auth or self.auth)
