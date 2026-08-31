from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from macp.v1 import core_pb2, envelope_pb2

from ._logging import logger

# Cross-SDK contract with macp-sdk-typescript#55 — these two string values are
# part of the wire-adjacent public API and must match the TypeScript SDK
# byte-for-byte. Do not rename, and do not add a third without coordinating
# there first.
ANOMALY_DUPLICATE_VOTE = "duplicate_vote"  # RFC-MACP-0007 §5.3
ANOMALY_DUPLICATE_BALLOT = "duplicate_ballot"  # RFC-MACP-0011 §5


@dataclass(frozen=True, slots=True)
class ProjectionAnomaly:
    """A discarded-message observation recorded by a projection.

    Cross-SDK contract with macp-sdk-typescript#55: the field names, their
    order, and both ``kind`` string values (``ANOMALY_DUPLICATE_VOTE``,
    ``ANOMALY_DUPLICATE_BALLOT``) are a byte-for-byte contract shared with the
    TypeScript SDK. Do not rename, reorder, or extend the field set without
    coordinating there first.

    Honesty clause: this records an **observation**, not a spec-violation
    verdict. A projection cannot distinguish a genuinely non-conforming
    source from a conforming source fed through an unfiltered loader --
    acceptance is not a wire property (see ``apply_envelope``'s docstring).
    Treat ``kind`` as "a second distinct message of this shape was observed
    and discarded; the first stands," not as "this transcript violates the
    spec."

    Deliberately does not carry the full ``Envelope``: too heavy a public
    commitment. The discarded envelope remains recoverable by correlating
    ``message_id`` against a projection's ``transcript``.
    """

    kind: str
    mode: str
    message_type: str
    message_id: str
    sender: str
    subject_id: str  # proposal_id / request_id
    detail: str = ""


class BaseProjection(ABC):
    """Abstract base for in-process mode state tracking.

    Maintains a local transcript and delegates mode-specific message handling
    to subclasses.  Needed because the runtime's ``GetSession`` RPC returns
    metadata only, not mode state or transcript.
    """

    MODE: ClassVar[str]

    def __init__(self) -> None:
        self.transcript: list[envelope_pb2.Envelope] = []
        self.phase: str = ""
        self.commitment: core_pb2.CommitmentPayload | None = None
        # Implementation detail, not public API: tracks message_id values already
        # applied to this projection so a redelivered envelope is a no-op. See
        # apply_envelope's docstring for the contract.
        self._seen_message_ids: set[str] = set()
        # Public: discarded-message observations recorded via _record_anomaly.
        # Inert as of this phase -- nothing appends to this list yet. See
        # ProjectionAnomaly's docstring for the cross-SDK contract it carries.
        self.anomalies: list[ProjectionAnomaly] = []

    @property
    def is_committed(self) -> bool:
        return self.commitment is not None

    @property
    def has_anomalies(self) -> bool:
        return bool(self.anomalies)

    @property
    def is_positive_outcome(self) -> bool | None:
        """Return the outcome polarity, or ``None`` if not yet committed."""
        if self.commitment is None:
            return None
        return getattr(self.commitment, "outcome_positive", True)

    def apply_envelope(self, envelope: envelope_pb2.Envelope) -> None:
        """Apply one envelope from **accepted history** to local state.

        Precondition — accepted history only, not enforced here: callers MUST
        feed only envelopes that a conforming runtime accepted. A subscribe
        stream satisfies this automatically (RFC-MACP-0006 §3.2 obligation 4:
        a runtime never replays rejected envelopes). Any other feed — hand-built
        fixtures, captured logs, transcripts carrying non-accepted entries — MUST
        be filtered by the caller before it reaches this method, the way
        ``tests/conformance/test_conformance_projections.py:153`` filters out
        non-``accept`` fixture entries. This method has no way to verify that
        precondition, and derived state is **undefined** without it.

        Idempotency: applying an envelope whose ``message_id`` has already been
        seen by this projection is a total no-op — it is not appended to
        ``transcript`` and is not dispatched to mode handling. This makes
        redelivery (e.g. a stream resubscribe replaying accepted history from
        the start) safe to re-apply without corrupting derived state. An empty
        ``message_id`` (the proto3 default for hand-built envelopes) is never
        deduplicated — every such envelope is applied. See the code comment on
        the dedup gate below for the precise normative basis.

        Honest limitation: this dedups **redelivery of the same envelope**; it
        cannot and does not detect a *genuine* duplicate — e.g. a second,
        distinct ``Vote`` from a sender who already voted on the same proposal,
        which carries its own new ``message_id`` and is applied like any other
        envelope. A rejected vote from a sender who never votes again corrupts
        derived state with nothing here to detect it. No mechanism in this
        method closes that gap; do not imply one exists.

        Determinism holds in both directions: replaying ``transcript`` through a
        fresh projection reproduces the same state, whether or not duplicates or
        redeliveries were present in the original feed.

        Acceptance is not a wire property: nothing on the envelope itself marks
        it as "accepted" — it is accepted only by virtue of appearing in
        accepted history at all, per the precondition above. If a caller sees a
        surprising result, correlate the offending envelope's ``message_id``
        against their own acceptance metadata (what they actually received back
        from the runtime) to determine whether the surprise came from a
        non-conforming source or from an unfiltered feed, rather than assuming
        this method mis-processed it.

        Not thread-safe: the ``message_id`` dedup check-then-add below is not
        atomic across threads, and this class holds no lock. A single
        projection instance must not be fed concurrently from multiple threads.

        On failure: if applying the envelope raises, this method removes the
        envelope from ``transcript`` and releases its ``message_id`` from the
        dedup set before the exception propagates, so the caller may retry
        the identical envelope and it will be applied rather than silently
        swallowed as a redelivery. This rollback covers only ``transcript``
        and the dedup set — subclass-derived state (e.g. ``self.phase``, or
        any subclass collection) is not rolled back.
        """
        if envelope.mode != self.MODE:
            return

        # Empty-id guard — the real reason, not a test accommodation: message_id
        # is a proto3 scalar, so its unset value is "" — not a real identity.
        # Deduplicating on "" would collapse every envelope lacking an id into a
        # single logical message. Hand-built envelopes without an id are a real,
        # exercised path in this repo: tests/unit/test_client_helpers.py:142 and
        # tests/unit/test_client_stream.py:160 both construct one. So the guard
        # only dedupes non-empty ids.
        #
        # At-least-once basis for dedup, cited precisely:
        #   - Load-bearing: RFC-MACP-0001-core.md:306 — "MACP assumes
        #     at-least-once delivery semantics at the transport layer."
        #     Unqualified, about the transport, so it binds any consumer of that
        #     transport including client projections. From this alone it
        #     follows that a projection may be handed the same envelope twice.
        #     This is an INFERENCE from a transport-layer premise, not a MUST
        #     addressed to clients.
        #   - Corroborating only, not compelling on its own: RFC-MACP-0001 §8.2
        #     (:316) — "Runtimes MUST enforce idempotent handling of duplicates
        #     using message_id." That MUST is addressed to runtimes at their
        #     ingress boundary, not to clients; its contribution here is only
        #     the *identity* to key dedup on (message_id, not envelope bytes).
        #   - Now normative for clients directly: RFC-MACP-0006 §3.2 (spec PR
        #     #80, RFC-0006 1.4.0-draft) requires a client to tolerate an
        #     already-observed envelope and to key duplicate detection on
        #     message_id.
        # Mirrors the runtime's own boundary idempotency: src/macp_sdk/client.py
        # :419-422 already treats ack.duplicate as idempotent success.
        message_id = envelope.message_id
        seen_id_added = False
        if message_id:
            if message_id in self._seen_message_ids:
                return
            self._seen_message_ids.add(message_id)
            seen_id_added = True

        self.transcript.append(envelope)

        # Rollback scope — narrower than "atomic" might suggest: the
        # dedup-set add and transcript append above both happen BEFORE the
        # actual effect (Commitment ParseFromString, or the subclass's
        # _apply_mode_message dispatch), either of which can raise. On such
        # a raise, the except block below undoes exactly those two
        # mutations — transcript and _seen_message_ids — and re-raises, so
        # a retry of the SAME envelope is not swallowed as a redelivery.
        # Concretely: a supervisor that catches an exception out of
        # Participant.run() and resubscribes (agent/transports.py:60
        # replays accepted history from after_sequence=0) re-feeds the same
        # envelope to the same projection (agent/participant.py:410,
        # apply_envelope unguarded) — without this rollback the dedup gate
        # would silently swallow that retry, permanently losing the
        # envelope's effect while transcript still claims it is present.
        #
        # What this does NOT cover: self.phase (assigned directly on
        # BaseProjection by subclasses — see projections.py:84,
        # task.py:103, handoff.py:71) and any subclass-owned collection
        # (evaluations, objections, accepts, rejections, updates,
        # completions, failures) are never rolled back, because this
        # method has no way to know what a subclass mutated. That is safe
        # only because every _apply_mode_message implementation in this
        # SDK today performs its one fallible operation (ParseFromString)
        # strictly before any mutation, and every record type it
        # constructs is a plain slotted dataclass whose construction
        # cannot raise — so there is presently no reachable path that
        # mutates subclass state and then raises. This is a
        # raise-before-mutate invariant that _apply_mode_message
        # implementations must preserve. BaseProjection is exported for
        # third-party subclassing (see __init__.py:143), and upcoming
        # first-wins/anomaly-tracking logic must keep fallible work ahead
        # of mutations to keep this guarantee honest.
        #
        # Catches Exception, not BaseException: KeyboardInterrupt/SystemExit
        # signal that the process is being torn down, not a recoverable
        # per-envelope failure, and should propagate immediately rather than
        # be treated as "this envelope's apply failed, try rolling back."
        try:
            if envelope.message_type == "Commitment":
                payload = core_pb2.CommitmentPayload()
                payload.ParseFromString(envelope.payload)
                self.commitment = payload
                self.phase = "Committed"
                return

            self._apply_mode_message(envelope)
        except Exception:
            # Roll back exactly what this call added, nothing more. In
            # every path exercised by this SDK today, the append two lines
            # above is the only mutation of transcript between there and
            # here, so transcript[-1] is the entry this call just
            # appended. But BaseProjection is a public ABC (exported at
            # __init__.py:143) that third parties may subclass, so guard
            # with an identity check rather than popping unconditionally.
            # The two ways the guard could see something else at [-1] are:
            #   - a subclass's _apply_mode_message appending to
            #     self.transcript itself before raising (no subclass in
            #     this SDK does this — each mutates only its own
            #     collections, never self.transcript directly);
            #   - a re-entrant call to apply_envelope on the same instance
            #     while this call is still on the stack (nothing in this
            #     SDK calls apply_envelope from within apply_envelope or
            #     from a mode handler).
            # Neither happens here, but a wrong-entry pop under either
            # would silently corrupt an unrelated transcript entry — data
            # corruption strictly worse than the bug this rollback fixes.
            # `is` identity, not `==`: protobuf messages compare by value,
            # so two distinct envelopes can be equal without being the
            # same object.
            if self.transcript and self.transcript[-1] is envelope:
                self.transcript.pop()
            if seen_id_added:
                self._seen_message_ids.discard(message_id)
            raise

    @abstractmethod
    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        """Handle a mode-specific (non-Commitment) envelope."""

    def _record_anomaly(
        self,
        *,
        kind: str,
        message_type: str,
        message_id: str,
        sender: str,
        subject_id: str,
        detail: str = "",
    ) -> None:
        """Append a `ProjectionAnomaly` and emit exactly one WARNING log line.

        Protected and keyword-only: subclasses are the only callers, and
        keyword-only arguments prevent a positional call from silently
        transposing two same-typed fields (e.g. sender/subject_id). ``mode``
        is filled in from ``self.MODE`` rather than accepted as an argument,
        so call sites cannot drift from the projection's own mode.

        Inert in this phase: nothing calls this yet. Phase 4 wires up the two
        call sites (Decision Vote, Quorum ballot) that actually invoke it.
        """
        anomaly = ProjectionAnomaly(
            kind=kind,
            mode=self.MODE,
            message_type=message_type,
            message_id=message_id,
            sender=sender,
            subject_id=subject_id,
            detail=detail,
        )
        self.anomalies.append(anomaly)
        logger.warning(
            "projection anomaly kind=%s mode=%s message_type=%s message_id=%s "
            "sender=%s subject_id=%s detail=%s",
            anomaly.kind,
            anomaly.mode,
            anomaly.message_type,
            anomaly.message_id,
            anomaly.sender,
            anomaly.subject_id,
            anomaly.detail,
        )
