from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from macp.modes.proposal.v1 import proposal_pb2
from macp.v1 import envelope_pb2

from .auth import AuthConfig
from .base_projection import BaseProjection
from .base_session import BaseSession
from .constants import MODE_PROPOSAL
from .envelope import build_envelope, serialize_message
from .errors import MacpSessionError
from .validation import validate_required_field

# ---------------------------------------------------------------------------
# Projection records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProposalRecord:
    proposal_id: str
    title: str
    summary: str
    proposer: str
    supersedes: str  # "" if original
    # Reachable values only. An Accept is recorded on the projection's
    # ``accepts`` list (and surfaced via ``accepted_proposal`` /
    # ``is_accepted``), never on this field -- no code path assigns
    # "accepted" here. A non-terminal Reject likewise leaves this "open";
    # only ``terminal=True`` sets "rejected".
    #
    # That is by design, not an omission (issue #112). Acceptance is a
    # per-sender, supersedable relation (RFC-MACP-0008 §5 rule 5), not a
    # per-proposal fact, so a scalar field here cannot hold it: "alice
    # accepts p2 while bob still accepts p1" is a legal state. This mirrors
    # the runtime, whose ``ProposalDisposition`` is {Live, Withdrawn} with
    # acceptance in a separate ``accepts`` map, and typescript-sdk's
    # ``projections/proposal.ts``, which also never assigns "accepted".
    # ``task.py``/``handoff.py`` do set "accepted" because their acceptance
    # is one actor claiming one slot. See docs/modes/proposal.md.
    status: str  # "open" | "rejected" | "withdrawn"
    tags: list[str]


@dataclass(slots=True)
class ProposalRejectRecord:
    proposal_id: str
    reason: str
    sender: str
    terminal: bool


@dataclass(slots=True)
class ProposalAcceptRecord:
    proposal_id: str
    reason: str
    sender: str


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class ProposalProjection(BaseProjection):
    """In-process state tracking for Proposal mode sessions."""

    MODE = MODE_PROPOSAL

    def __init__(self) -> None:
        super().__init__()
        self.phase = "Negotiating"
        self.proposals: dict[str, ProposalRecord] = {}
        self.accepts: list[ProposalAcceptRecord] = []
        self.rejections: list[ProposalRejectRecord] = []
        # Tracks each sender's most recent Accept, so a later Accept from the
        # same sender supersedes an earlier one (RFC-MACP-0008 §5 rule 5).
        # `self.accepts` remains the full audit trail; this is the derived
        # "current position per sender" view `is_accepted`/`accepted_proposal`
        # read from. Mirrors typescript-sdk's `latestAcceptBySender` map.
        self._latest_accept_by_sender: dict[str, str] = {}

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "Proposal":
            p = proposal_pb2.ProposalPayload()
            p.ParseFromString(envelope.payload)
            self.proposals[p.proposal_id] = ProposalRecord(
                proposal_id=p.proposal_id,
                title=p.title,
                summary=p.summary,
                proposer=envelope.sender,
                supersedes="",
                status="open",
                tags=list(p.tags),
            )
            return

        if mt == "CounterProposal":
            p = proposal_pb2.CounterProposalPayload()
            p.ParseFromString(envelope.payload)
            self.proposals[p.proposal_id] = ProposalRecord(
                proposal_id=p.proposal_id,
                title=p.title,
                summary=p.summary,
                proposer=envelope.sender,
                supersedes=p.supersedes_proposal_id,
                status="open",
                tags=[],
            )
            return

        if mt == "Accept":
            p = proposal_pb2.AcceptPayload()
            p.ParseFromString(envelope.payload)
            self.accepts.append(
                ProposalAcceptRecord(
                    proposal_id=p.proposal_id,
                    reason=p.reason,
                    sender=envelope.sender,
                )
            )
            self._latest_accept_by_sender[envelope.sender] = p.proposal_id
            return

        if mt == "Reject":
            p = proposal_pb2.RejectPayload()
            p.ParseFromString(envelope.payload)
            self.rejections.append(
                ProposalRejectRecord(
                    proposal_id=p.proposal_id,
                    reason=p.reason,
                    sender=envelope.sender,
                    terminal=p.terminal,
                )
            )
            if p.terminal:
                rec = self.proposals.get(p.proposal_id)
                # A terminal Reject must name a proposal this projection has
                # actually seen (RFC-MACP-0008 §5 rule 3) before it ends the
                # negotiation. "TerminalRejected" is in TERMINAL_PHASES
                # (agent/participant.py:57-59), so moving phase here for an
                # unknown proposal_id would fire a Participant's on_terminal
                # and tear down its stream for a session that never actually
                # terminated. Same shape as task.py's per-task gates
                # (:172/:196/:212): gate the state transition, keep the
                # audit append above unconditional. has_terminal_rejection()
                # / is_terminally_rejected() deliberately still read
                # self.rejections, not phase, so they stay True either way.
                if rec is not None:
                    rec.status = "rejected"
                    self._set_phase("TerminalRejected")
            return

        if mt == "Withdraw":
            p = proposal_pb2.WithdrawPayload()
            p.ParseFromString(envelope.payload)
            rec = self.proposals.get(p.proposal_id)
            if rec is not None:
                rec.status = "withdrawn"

    # -- State query helpers --

    def live_proposals(self) -> dict[str, ProposalRecord]:
        """Return proposals that have not been withdrawn."""
        return {k: v for k, v in self.proposals.items() if v.status != "withdrawn"}

    def accepted_proposal(self) -> str | None:
        """Return the proposal_id that all accepting senders currently agree on,
        or None. Each sender's *most recent* Accept is what counts — a later
        Accept from the same sender supersedes an earlier one from them
        (RFC-MACP-0008 §5 rule 5), so a sender who moved on doesn't keep an
        earlier proposal_id in this comparison.
        """
        if not self._latest_accept_by_sender:
            return None
        ids = set(self._latest_accept_by_sender.values())
        if len(ids) == 1:
            return ids.pop()
        return None

    def has_terminal_rejection(self) -> bool:
        return any(r.terminal for r in self.rejections)

    def active_proposals(self) -> list[ProposalRecord]:
        """Return proposals whose status is 'open'."""
        return [p for p in self.proposals.values() if p.status == "open"]

    def latest_proposal(self) -> ProposalRecord | None:
        """Return the most recently added proposal, or None."""
        if not self.proposals:
            return None
        return list(self.proposals.values())[-1]

    def is_accepted(self, proposal_id: str) -> bool:
        """True if *proposal_id* is some sender's current (most recent) accept.

        Superseded by a later Accept from the same sender — see
        ``accepted_proposal``.
        """
        return proposal_id in self._latest_accept_by_sender.values()

    def is_terminally_rejected(self, proposal_id: str) -> bool:
        """True if a terminal rejection exists for *proposal_id*."""
        return any(r.proposal_id == proposal_id and r.terminal for r in self.rejections)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class ProposalSession(BaseSession):
    """High-level helper for Proposal mode sessions."""

    MODE = MODE_PROPOSAL

    def _create_projection(self) -> BaseProjection:
        return ProposalProjection()

    @property
    def proposal_projection(self) -> ProposalProjection:
        assert isinstance(self.projection, ProposalProjection)
        return self.projection

    def propose(
        self,
        proposal_id: str,
        title: str,
        *,
        summary: str = "",
        details: bytes = b"",
        tags: list[str] | None = None,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        validate_required_field("title", title)
        payload = proposal_pb2.ProposalPayload(
            proposal_id=proposal_id,
            title=title,
            summary=summary,
            details=details,
            tags=tags or [],
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Proposal",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def counter_propose(
        self,
        proposal_id: str,
        supersedes_proposal_id: str,
        title: str,
        *,
        summary: str = "",
        details: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        validate_required_field("title", title)
        payload = proposal_pb2.CounterProposalPayload(
            proposal_id=proposal_id,
            supersedes_proposal_id=supersedes_proposal_id,
            title=title,
            summary=summary,
            details=details,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="CounterProposal",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def accept(
        self,
        proposal_id: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        payload = proposal_pb2.AcceptPayload(
            proposal_id=proposal_id,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Accept",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def reject(
        self,
        proposal_id: str,
        *,
        terminal: bool = False,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("proposal_id", proposal_id)
        payload = proposal_pb2.RejectPayload(
            proposal_id=proposal_id,
            terminal=terminal,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Reject",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def withdraw(
        self,
        proposal_id: str,
        *,
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        if not proposal_id or not proposal_id.strip():
            raise MacpSessionError("proposal_id must be non-empty for withdraw")
        payload = proposal_pb2.WithdrawPayload(
            proposal_id=proposal_id,
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="Withdraw",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)


# ── Deprecated aliases (issue #103 / multiagentcoordinationprotocol#135) ─────
#
# ``RejectRecord``/``AcceptRecord`` are the pre-rename names, kept as
# module-level lazy aliases (PEP 562) for one minor version, removed at this
# SDK's next major. Same mechanism and reasoning as ``agent/strategies.py``'s
# own alias dict -- see the comment there for the full rationale (plain
# assignment is silent; a wrapper subclass is unnecessary complexity here
# too). This module has no ``__path__`` (a plain file, not a package), so
# ``from macp_sdk.proposal import RejectRecord`` resolves via a single
# ``getattr`` call -- the warning fires exactly once per such import.
_DEPRECATED_ALIASES = {
    "RejectRecord": "ProposalRejectRecord",
    "AcceptRecord": "ProposalAcceptRecord",
}


def __getattr__(name: str) -> Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
