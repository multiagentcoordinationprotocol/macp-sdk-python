from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .._logging import logger
from ..auth import AuthConfig
from ..base_projection import BaseProjection
from ..client import MacpClient
from ..constants import (
    MODE_DECISION,
    MODE_HANDOFF,
    MODE_PROPOSAL,
    MODE_QUORUM,
    MODE_TASK,
)
from ..envelope import build_commitment_payload, build_envelope, serialize_message
from ..handoff import HandoffProjection
from ..projections import DecisionProjection
from ..proposal import ProposalProjection
from ..quorum import QuorumProjection
from ..task import TaskProjection
from .dispatcher import Dispatcher
from .transports import GrpcTransportAdapter, TransportAdapter, _envelope_to_message
from .types import (
    HandlerContext,
    IncomingMessage,
    MessageHandler,
    PhaseChangeHandler,
    SessionInfo,
    TerminalHandler,
    TerminalResult,
)

_MODE_PROJECTIONS: dict[str, type[BaseProjection]] = {
    MODE_DECISION: DecisionProjection,
    MODE_PROPOSAL: ProposalProjection,
    MODE_QUORUM: QuorumProjection,
    MODE_TASK: TaskProjection,
    MODE_HANDOFF: HandoffProjection,
}

# Canonical set of terminal projection phases across all modes. Mirrors
# ``TERMINAL_PHASES`` in typescript-sdk/src/agent/participant.ts so that both
# SDKs fire ``on_terminal`` at the same observable point in a session. When a
# mode introduces a new terminal phase, add it here and in the TypeScript SDK
# in the same change.
TERMINAL_PHASES: frozenset[str] = frozenset(
    {"Committed", "Accepted", "Declined", "Cancelled", "TerminalRejected"}
)


@dataclass
class InitiatorConfig:
    """Configuration for an initiator agent's SessionStart + kickoff.

    Parity with typescript-sdk's ``InitiatorConfig`` interface. Passed to
    :class:`Participant` via ``initiator_config``; when set, the participant
    emits ``SessionStart`` (and, if configured, a kickoff envelope) before
    opening the stream.
    """

    intent: str
    participants: list[str]
    ttl_ms: int
    context_id: str = ""
    extensions: dict[str, bytes] = field(default_factory=dict)
    roots: list[dict[str, str]] | None = None
    mode_version: str | None = None
    configuration_version: str | None = None
    policy_version: str | None = None
    # Runtime v0.5.0 per-session maximum-suspension cap (ms); 0 = runtime
    # default (currently 7 days). Negative values are rejected at build time.
    max_suspend_ms: int = 0
    kickoff_message_type: str | None = None
    kickoff_payload: dict[str, Any] = field(default_factory=dict)


class ParticipantActions:
    """Thin wrapper providing action methods bound to a participant's session."""

    def __init__(
        self,
        client: MacpClient,
        session_id: str,
        auth: AuthConfig | None,
        *,
        mode: str = "",
        participant_id: str = "",
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._auth = auth
        self._mode = mode
        self._participant_id = participant_id

    def send_envelope(self, envelope: Any) -> Any:
        """Send a pre-built envelope via the MACP client."""
        return self._client.send(envelope, auth=self._auth)

    def get_session(self) -> Any:
        """Query session metadata from the runtime."""
        return self._client.get_session(self._session_id, auth=self._auth)

    def cancel_session(self, reason: str = "") -> Any:
        """Cancel the session."""
        return self._client.cancel_session(self._session_id, reason=reason, auth=self._auth)

    def start_session(
        self,
        intent: str,
        participants: list[str],
        ttl_ms: int,
        context_id: str = "",
        extensions: dict[str, bytes] | None = None,
        mode_version: str | None = None,
        configuration_version: str | None = None,
        policy_version: str | None = None,
        max_suspend_ms: int = 0,
    ) -> Any:
        """Send a SessionStart envelope to open the session.

        ``max_suspend_ms`` (runtime v0.5.0) binds a per-session maximum
        suspension cap; ``0`` selects the runtime default.
        """
        from ..constants import (
            DEFAULT_CONFIGURATION_VERSION,
            DEFAULT_MODE_VERSION,
            DEFAULT_POLICY_VERSION,
        )
        from ..envelope import (
            build_envelope,
            build_session_start_payload,
            serialize_message,
        )

        payload = build_session_start_payload(
            intent=intent,
            participants=participants,
            ttl_ms=ttl_ms,
            context_id=context_id,
            extensions=extensions,
            mode_version=mode_version or DEFAULT_MODE_VERSION,
            configuration_version=configuration_version or DEFAULT_CONFIGURATION_VERSION,
            policy_version=policy_version or DEFAULT_POLICY_VERSION,
            max_suspend_ms=max_suspend_ms,
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="SessionStart",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(payload),
        )
        return self.send_envelope(envelope)

    def evaluate(
        self,
        proposal_id: str,
        recommendation: str,
        *,
        confidence: float,
        reason: str = "",
    ) -> Any:
        """Send an Evaluation envelope for a decision-mode session."""
        from macp.modes.decision.v1 import decision_pb2

        payload = decision_pb2.EvaluationPayload(
            proposal_id=proposal_id,
            recommendation=recommendation.upper(),
            confidence=confidence,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="Evaluation",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(payload),
        )
        return self.send_envelope(envelope)

    def vote(
        self,
        proposal_id: str,
        vote: str,
        *,
        reason: str = "",
    ) -> Any:
        """Send a Vote envelope for a decision-mode session."""
        from macp.modes.decision.v1 import decision_pb2

        payload = decision_pb2.VotePayload(
            proposal_id=proposal_id,
            vote=vote.upper(),
            reason=reason,
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="Vote",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(payload),
        )
        return self.send_envelope(envelope)

    def raise_objection(
        self,
        proposal_id: str,
        *,
        reason: str,
        severity: str = "medium",
    ) -> Any:
        """Send an Objection envelope for a decision-mode session."""
        from macp.modes.decision.v1 import decision_pb2

        payload = decision_pb2.ObjectionPayload(
            proposal_id=proposal_id,
            reason=reason,
            severity=severity.lower(),
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="Objection",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(payload),
        )
        return self.send_envelope(envelope)

    def propose(
        self,
        proposal_id: str,
        option: str,
        *,
        rationale: str = "",
        supporting_data: bytes = b"",
    ) -> Any:
        """Send a Proposal envelope for a decision-mode session."""
        from macp.modes.decision.v1 import decision_pb2

        payload = decision_pb2.ProposalPayload(
            proposal_id=proposal_id,
            option=option,
            rationale=rationale,
            supporting_data=supporting_data,
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="Proposal",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(payload),
        )
        return self.send_envelope(envelope)

    def commit(
        self,
        action: str,
        authority_scope: str,
        *,
        reason: str = "",
        commitment_id: str | None = None,
        outcome_positive: bool = True,
    ) -> Any:
        """Send a Commitment envelope for the session."""
        commitment_payload = build_commitment_payload(
            action=action,
            authority_scope=authority_scope,
            reason=reason,
            commitment_id=commitment_id,
            outcome_positive=outcome_positive,
        )
        envelope = build_envelope(
            mode=self._mode,
            message_type="Commitment",
            session_id=self._session_id,
            sender=self._participant_id,
            payload=serialize_message(commitment_payload),
        )
        return self.send_envelope(envelope)


class Participant:
    """High-level agent abstraction for participating in MACP sessions.

    Wraps a :class:`MacpClient`, a :class:`Dispatcher`, and a mode-specific
    projection.  Handlers are registered via ``on()``, ``on_phase_change()``,
    and ``on_terminal()`` with a fluent API.

    The ``run()`` method enters a blocking event loop that polls the
    control-plane for session events and dispatches them to handlers.
    Call ``stop()`` to signal the loop to exit.
    """

    def __init__(
        self,
        *,
        participant_id: str,
        session_id: str,
        mode: str,
        client: MacpClient,
        auth: AuthConfig | None = None,
        participants: list[str] | None = None,
        mode_version: str | None = None,
        configuration_version: str | None = None,
        policy_version: str | None = None,
        transport: TransportAdapter | None = None,
        initiator_config: InitiatorConfig | None = None,
    ) -> None:
        self._participant_id = participant_id
        self._session_id = session_id
        self._mode = mode
        self._client = client
        self._auth = auth
        self._stopped = False
        self._initiator_config = initiator_config

        self._dispatcher = Dispatcher()
        self._session = SessionInfo(
            session_id=session_id,
            mode=mode,
            participants=list(participants or []),
            mode_version=mode_version,
            configuration_version=configuration_version,
            policy_version=policy_version,
        )

        projection_cls = _MODE_PROJECTIONS.get(mode)
        if projection_cls is not None:
            self._projection: BaseProjection | None = projection_cls()
        else:
            self._projection = None

        self._actions = ParticipantActions(
            client,
            session_id,
            auth,
            mode=mode,
            participant_id=participant_id,
        )
        self._last_phase: str | None = None
        self._transport = transport
        self._cancel_callback_server: Any | None = None

    @property
    def participant_id(self) -> str:
        return self._participant_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def projection(self) -> BaseProjection | None:
        return self._projection

    @property
    def actions(self) -> ParticipantActions:
        return self._actions

    @property
    def session(self) -> SessionInfo:
        return self._session

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def on(self, message_type: str, handler: MessageHandler) -> Participant:
        """Register a handler for a message type (fluent API)."""
        self._dispatcher.on(message_type, handler)
        return self

    def on_phase_change(self, phase: str, handler: PhaseChangeHandler) -> Participant:
        """Register a handler for a phase change (fluent API)."""
        self._dispatcher.on_phase_change(phase, handler)
        return self

    def on_terminal(self, handler: TerminalHandler) -> Participant:
        """Register the terminal handler (fluent API)."""
        self._dispatcher.on_terminal(handler)
        return self

    def _build_context(self) -> HandlerContext:
        return HandlerContext(
            participant=self._participant_id,
            projection=self._projection,
            actions=self._actions,
            session=self._session,
            log_fn=logger.info,
        )

    def _process_envelope(self, envelope: Any) -> None:
        """Process a single envelope: apply projection, dispatch handlers,
        and fire phase-change / terminal handlers on phase transitions.

        Terminal dispatch is primarily driven by projection phase
        transitioning into :data:`TERMINAL_PHASES` — matches TypeScript SDK
        semantics so both SDKs fire ``on_terminal`` at the same observable
        point. As a fallback (for envelopes projections don't model — e.g.
        ``SessionCancel``), we fire terminal on the message type itself so
        clients always get a ``stop`` signal for end-of-session envelopes.
        """
        if self._projection is not None:
            self._projection.apply_envelope(envelope)

        message = _envelope_to_message(envelope)
        ctx = self._build_context()

        # Dispatch message handler first, so handlers see the post-apply
        # projection state.
        self._dispatcher.dispatch(message, ctx)

        fired_terminal = False

        # Phase transition path — drives both on_phase_change and on_terminal.
        if self._projection is not None:
            current_phase = self._projection.phase
            if current_phase and current_phase != self._last_phase:
                self._last_phase = current_phase
                self._dispatcher.dispatch_phase_change(current_phase, ctx)

                if current_phase in TERMINAL_PHASES:
                    commitment = getattr(self._projection, "commitment", None)
                    result = TerminalResult(
                        state=current_phase,
                        commitment=commitment
                        if commitment is not None
                        else envelope
                        if envelope.message_type == "Commitment"
                        else None,
                    )
                    self._dispatcher.dispatch_terminal(result)
                    self._stopped = True
                    fired_terminal = True

        # Fallback for envelopes projections don't transition phase on —
        # principally ``SessionCancel``. Keeps terminal dispatch reliable
        # while the phase-driven path remains primary.
        if not fired_terminal and envelope.message_type == "SessionCancel":
            self._dispatcher.dispatch_terminal(TerminalResult(state="Cancelled"))
            self._stopped = True

    def _process_message(self, message: IncomingMessage) -> None:
        """Process a pre-built :class:`IncomingMessage` (from an HTTP polling
        transport that decodes envelopes upstream).

        Terminal dispatch follows the same phase-driven model as
        :meth:`_process_envelope`; since HTTP transports may not carry raw
        envelopes, we fall back to a message-type check for terminal events
        only when no projection is attached.
        """
        ctx = self._build_context()

        self._dispatcher.dispatch(message, ctx)

        if self._projection is not None:
            current_phase = self._projection.phase
            if current_phase and current_phase != self._last_phase:
                self._last_phase = current_phase
                self._dispatcher.dispatch_phase_change(current_phase, ctx)

                if current_phase in TERMINAL_PHASES:
                    commitment = getattr(self._projection, "commitment", None)
                    result = TerminalResult(state=current_phase, commitment=commitment)
                    self._dispatcher.dispatch_terminal(result)
                    self._stopped = True
            return

        # No projection attached — use message-type heuristic for terminal.
        if message.message_type == "Commitment":
            self._dispatcher.dispatch_terminal(TerminalResult(state="Committed"))
            self._stopped = True
        elif message.message_type == "SessionCancel":
            self._dispatcher.dispatch_terminal(TerminalResult(state="Cancelled"))
            self._stopped = True

    def run(self) -> None:
        """Enter the blocking event loop.

        If this participant is the initiator (``initiator_config`` is set),
        emits SessionStart + kickoff before opening the stream.

        Dispatches received events to registered handlers until the session
        reaches a terminal state or ``stop()`` is called.
        """
        logger.info(
            "participant %s joining session %s (mode=%s, initiator=%s)",
            self._participant_id,
            self._session_id,
            self._mode,
            self._initiator_config is not None,
        )

        if self._initiator_config is not None:
            self._emit_initiator_envelopes()

        transport = self._transport or GrpcTransportAdapter(
            self._client,
            self._session_id,
            auth=self._auth,
        )
        try:
            for message in transport.start():
                if self._stopped:
                    break
                if message.raw is not None:
                    self._process_envelope(message.raw)
                else:
                    self._process_message(message)
        finally:
            transport.stop()

    def _emit_initiator_envelopes(self) -> None:
        """Emit SessionStart + kickoff envelope as the initiator."""
        cfg = self._initiator_config
        if cfg is None:
            return

        self._actions.start_session(
            intent=cfg.intent,
            participants=cfg.participants,
            ttl_ms=cfg.ttl_ms,
            context_id=cfg.context_id,
            extensions=cfg.extensions or None,
            mode_version=cfg.mode_version,
            configuration_version=cfg.configuration_version,
            policy_version=cfg.policy_version,
            max_suspend_ms=cfg.max_suspend_ms,
        )
        logger.info("SessionStart emitted (session=%s)", self._session_id)

        if cfg.kickoff_message_type == "Proposal":
            payload = cfg.kickoff_payload or {}
            proposal_id = str(
                payload.get("proposalId")
                or payload.get("proposal_id")
                or f"{self._session_id}-kickoff"
            )
            option = str(payload.get("option", "decide"))
            rationale = str(payload.get("rationale", ""))
            self._actions.propose(proposal_id, option, rationale=rationale)
            logger.info("Kickoff proposal emitted (proposalId=%s)", proposal_id)

    def process_event(self, envelope: Any) -> None:
        """Manually process a single envelope (for testing or polling transports)."""
        self._process_envelope(envelope)

    def stop(self) -> None:
        """Signal the event loop to stop.

        Also shuts down a bound cancel-callback HTTP server (if one was
        started by :func:`from_bootstrap` for this participant).
        """
        self._stopped = True
        server = self._cancel_callback_server
        if server is not None:
            self._cancel_callback_server = None
            try:
                server.close()
            except Exception:
                logger.exception("cancel_callback server close failed")

    def attach_cancel_callback_server(self, server: Any) -> None:
        """Attach a :class:`CancelCallbackServer` to this participant.

        The server's lifetime is then tied to :meth:`stop` — the event
        loop exit (or an incoming cancel POST that calls ``stop``) shuts
        it down.
        """
        self._cancel_callback_server = server
