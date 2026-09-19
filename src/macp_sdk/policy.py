"""Typed policy builders for all MACP governance modes.

Each builder produces a :class:`PolicyDescriptor` with JSON-encoded rules
that match the normative rule schemas defined in RFC-MACP-0012 and the
Rust runtime's ``src/policy/rules.rs``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from macp.v1 import policy_pb2

from .errors import MacpSessionError

# ── Shared commitment rules (all modes) ─────────────────────────────


@dataclass(frozen=True, slots=True)
class CommitmentRules:
    """Commitment authority configuration shared by all mode policies."""

    authority: str = "initiator_only"
    designated_roles: list[str] = field(default_factory=list)
    # RFC-MACP-0012 §4.1, Decision mode only. Under schema_version 1 and 2,
    # this flag has two independent roles: gating a vote-authorized commitment
    # on quorum, and deciding whether an empty tally blocks a positive
    # commitment. Under schema_version 3 and above the second role is gone
    # (empty tallies already fail closed), so setting this true over an
    # effective participation floor of zero gates nothing and is equivalent to
    # false ("Vacuous participation floor").
    require_vote_quorum: bool = False
    # RFC-MACP-0012 schema_version 2, Decision mode only: when True, a
    # reject-majority resolves the session with a committed *negative* outcome
    # (``outcome_positive = false``) instead of denying commitment. Emitted only
    # by ``build_decision_policy`` (see ``_commitment_dict`` note) so version-1
    # quorum/proposal/task/handoff schemas are unaffected. Default False keeps
    # version-1 behaviour for existing callers.
    allow_decline_over_approval: bool = False


def _commitment_dict(c: CommitmentRules) -> dict[str, object]:
    # Shared by all five mode builders — intentionally excludes the Decision-only
    # ``allow_decline_over_approval`` field so it does not leak into the still
    # version-1 quorum/proposal/task/handoff commitment schemas. Decision emits
    # that field itself in ``build_decision_policy``.
    #
    # ``authority: "designated_role"`` with an empty (or unset) ``designated_roles``
    # names no one, so no sender could ever satisfy it. All five rule schemas
    # now reject this combination at admission (spec PR #121); previously only
    # decision-rules.schema.json enforced it, so a caller building a
    # quorum/proposal/task/handoff policy this way got no client-side signal.
    # Validated once, here, so every mode builder gets it uniformly.
    if c.authority == "designated_role" and not c.designated_roles:
        raise MacpSessionError(
            "commitment.authority is 'designated_role' but designated_roles is "
            "empty -- this names no one, so no sender could ever satisfy it. "
            "Pass at least one role/participant id in designated_roles."
        )
    return {
        "authority": c.authority,
        "designated_roles": c.designated_roles,
        "require_vote_quorum": c.require_vote_quorum,
    }


# ── Decision mode ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VotingRules:
    """Voting configuration for a decision policy."""

    algorithm: str = "none"
    threshold: float = 0.5
    quorum_type: str | None = None
    quorum_value: float | None = None
    weights: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class ObjectionHandlingRules:
    """Objection handling configuration for a decision policy."""

    critical_severity_vetoes: bool = False
    veto_threshold: int = 1
    # RFC-MACP-0012 schema_version 2: action taken when a critical objection
    # would block commitment. One of ``"deny"`` (reject the commitment; legacy
    # default), ``"finalize_decline"`` (resolve the session as a negative
    # outcome), or ``"hold"`` (leave the session open). Default ``"deny"``
    # preserves version-1 behaviour.
    critical_objection_action: str = "deny"


@dataclass(frozen=True, slots=True)
class EvaluationRules:
    """Evaluation configuration for a decision policy."""

    minimum_confidence: float = 0.0
    required_before_voting: bool = False


_DECISION_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_DECISION_ALGORITHMS = frozenset(
    {"none", "majority", "supermajority", "unanimous", "weighted", "plurality"}
)


def build_decision_policy(
    policy_id: str,
    description: str,
    *,
    voting: VotingRules | None = None,
    objection_handling: ObjectionHandlingRules | None = None,
    evaluation: EvaluationRules | None = None,
    commitment: CommitmentRules | None = None,
    schema_version: int = 2,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Decision mode governance.

    ``schema_version`` selects the RFC-MACP-0012 rule semantics the runtime
    evaluates this policy under (validated, never re-validated, at
    admission — RFC-MACP-0012 §8 item 4):

    - ``1``/``2``: an empty decisive tally is fail-*open* (satisfies the
      voting requirement).
    - ``3``: an empty decisive tally is fail-*closed* for every algorithm
      except ``"none"`` (RFC-MACP-0012 §4.1, adopted in spec PR #99).

    Defaults to ``2`` to keep existing callers' semantics unchanged; this is
    a deliberate default, not an oversight — pass ``schema_version=3``
    explicitly to opt into fail-closed empty tallies.
    """
    if schema_version not in _DECISION_SCHEMA_VERSIONS:
        raise MacpSessionError(
            f"schema_version must be one of {sorted(_DECISION_SCHEMA_VERSIONS)}, "
            f"got {schema_version!r}"
        )
    v = voting or VotingRules()
    o = objection_handling or ObjectionHandlingRules()
    e = evaluation or EvaluationRules()
    c = commitment or CommitmentRules()

    # Match decision-rules.schema.json before the runtime does (same rationale
    # as build_quorum_policy's threshold checks below): a bad descriptor fails
    # immediately client-side instead of round-tripping to RegisterPolicy.
    if v.algorithm not in _DECISION_ALGORITHMS:
        raise MacpSessionError(
            f"voting algorithm must be one of {sorted(_DECISION_ALGORITHMS)}, got {v.algorithm!r}"
        )
    if not (0 < v.threshold <= 1):
        raise MacpSessionError(f"voting threshold must be > 0 and <= 1, got {v.threshold!r}")
    if v.algorithm == "majority" and v.threshold < 0.5:
        raise MacpSessionError(
            f"'majority' requires threshold >= 0.5 (an even split approves), got {v.threshold!r}"
        )
    if v.algorithm == "supermajority" and v.threshold <= 0.5:
        raise MacpSessionError(
            "'supermajority' requires threshold > 0.5 -- the field's own "
            f"default of 0.5 is a bare majority wearing the name, got {v.threshold!r}. "
            "Pass an explicit threshold (e.g. 0.67) for supermajority."
        )
    if v.algorithm == "weighted" and not v.weights:
        raise MacpSessionError("'weighted' algorithm requires a non-empty 'weights' map")
    # The weighted electorate is meaningful only when non-empty and strictly
    # positive-valued -- enforced unconditionally, at every algorithm, not
    # only 'weighted' (decision-rules.schema.json: minProperties 1,
    # additionalProperties.exclusiveMinimum 0). A weight-0 participant is
    # expressed by omission from this map, never by an explicit 0.
    if v.weights is not None:
        if not v.weights:
            raise MacpSessionError("'weights', if provided, must be non-empty")
        for participant, weight in v.weights.items():
            if weight <= 0:
                raise MacpSessionError(
                    f"weights['{participant}'] must be > 0, got {weight!r} -- a "
                    "weight-0 observer is expressed by omission from the map, "
                    "not by an explicit 0"
                )

    voting_section: dict[str, object] = {
        "algorithm": v.algorithm,
        "threshold": v.threshold,
    }
    # Emit ``quorum`` only when the caller sets it, mirroring the conditional
    # ``weights`` emission below. This keeps the Decision ``rules`` JSON
    # byte-identical to the typescript-sdk builder (which omits an unset quorum)
    # and to the canonical example descriptors, which drop optional voting fields
    # they do not set. ``quorum`` is optional with defaults ``type="count"``,
    # ``value=0`` in decision-rules.schema.json, so omission is schema-equivalent.
    if v.quorum_type is not None or v.quorum_value is not None:
        voting_section["quorum"] = {
            "type": v.quorum_type or "count",
            "value": v.quorum_value or 0,
        }
    if v.weights is not None:
        voting_section["weights"] = v.weights

    # Decision-only: extend the shared commitment rules with the schema_version 2
    # decline-over-approval switch without polluting the other four builders.
    commitment_section = _commitment_dict(c)
    commitment_section["allow_decline_over_approval"] = c.allow_decline_over_approval

    rules: dict[str, object] = {
        "voting": voting_section,
        "objection_handling": {
            "critical_severity_vetoes": o.critical_severity_vetoes,
            "veto_threshold": o.veto_threshold,
            "critical_objection_action": o.critical_objection_action,
        },
        "evaluation": {
            "minimum_confidence": e.minimum_confidence,
            "required_before_voting": e.required_before_voting,
        },
        "commitment": commitment_section,
    }

    return policy_pb2.PolicyDescriptor(
        policy_id=policy_id,
        mode="macp.mode.decision.v1",
        description=description,
        rules=json.dumps(rules).encode(),
        schema_version=schema_version,
    )


# ── Quorum mode ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QuorumThreshold:
    """Quorum threshold configuration (RFC-MACP-0012 §4.2 ``threshold`` object).

    ``threshold`` is strictly the **approval bar** — the number/percentage of
    approvals required to commit. There is no separate participation quorum in
    schema_version <= 3. ``value`` is an **integer** in the canonical
    ``quorum-rules.schema.json``: an approval count for ``n_of_m``, and an
    integer percentage 1-100 for ``percentage`` (rounded up to an effective
    approval count). ``value`` must be strictly greater than 0 — a zero
    approval bar is trivially satisfied by any ballot set, so a
    restrictive-looking quorum policy would approve everything; the schema
    enforces ``exclusiveMinimum: 0`` and ``build_quorum_policy`` mirrors that
    at build time. A fractional value (e.g. ``0.75``) is likewise rejected by
    the runtime's schema validation, so this is typed ``int``.

    The ``"weighted"`` identifier some earlier drafts mentioned for this field
    is **reserved** — it was removed from the canonical schema without ever
    having defined semantics (no weights vocabulary, no electorate rule, no
    termination arithmetic) and must not be used.
    """

    type: str = "n_of_m"
    value: int = 1


@dataclass(frozen=True, slots=True)
class AbstentionRules:
    """Abstention handling configuration (RFC: ``abstention`` object)."""

    counts_toward_quorum: bool = False
    interpretation: str = "neutral"


def build_quorum_policy(
    policy_id: str,
    description: str,
    *,
    threshold: QuorumThreshold | None = None,
    abstention: AbstentionRules | None = None,
    commitment: CommitmentRules | None = None,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Quorum mode governance."""
    t = threshold or QuorumThreshold()
    a = abstention or AbstentionRules()
    c = commitment or CommitmentRules()

    # Match the canonical schema's constraints before the runtime does, so a
    # bad descriptor fails immediately client-side instead of round-tripping
    # to an INVALID_POLICY_DEFINITION from RegisterPolicy. Order matters:
    # type first (a float or bool reaching the range checks below would
    # compare fine numerically but produce a confusing message), then > 0,
    # then the percentage-specific <= 100 cap.
    #
    # bool is checked explicitly because isinstance(True, int) is True in
    # Python -- QuorumThreshold(value=True) must not silently mean 1.
    if isinstance(t.value, bool) or not isinstance(t.value, int):
        raise MacpSessionError(
            "quorum threshold value must be an integer (e.g. 75 for 75%), got "
            f"{t.value!r}. The canonical quorum-rules schema declares "
            "'value' as an integer for every threshold type; a fractional "
            "value like 0.75 would produce a schema-invalid descriptor that "
            "the runtime rejects at RegisterPolicy with worse diagnostics."
        )
    if t.value <= 0:
        raise MacpSessionError(
            f"quorum threshold value must be > 0, got {t.value}. A zero approval "
            "bar is trivially satisfied by any ballot set, so the canonical "
            "quorum-rules schema declares 'value' with exclusiveMinimum 0."
        )
    if t.type == "percentage" and t.value > 100:
        raise MacpSessionError(
            f"quorum threshold value must be 1-100 for type 'percentage', got {t.value}"
        )
    if t.type not in ("n_of_m", "percentage"):
        raise MacpSessionError(
            f"quorum threshold type must be 'n_of_m' or 'percentage', got {t.type!r}. "
            "'weighted' is reserved -- it was removed from the canonical schema "
            "without ever having defined semantics and must not be used."
        )

    rules: dict[str, object] = {
        "threshold": {"type": t.type, "value": t.value},
        "abstention": {
            "counts_toward_quorum": a.counts_toward_quorum,
            "interpretation": a.interpretation,
        },
        "commitment": _commitment_dict(c),
    }

    return policy_pb2.PolicyDescriptor(
        policy_id=policy_id,
        mode="macp.mode.quorum.v1",
        description=description,
        rules=json.dumps(rules).encode(),
        schema_version=1,
    )


# ── Proposal mode ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProposalAcceptanceRules:
    """Acceptance criterion for proposal policies."""

    criterion: str = "all_parties"


@dataclass(frozen=True, slots=True)
class CounterProposalRules:
    """Counter-proposal limits for proposal policies."""

    max_rounds: int = 0


@dataclass(frozen=True, slots=True)
class RejectionRules:
    """Rejection handling for proposal policies."""

    terminal_on_any_reject: bool = False


def build_proposal_policy(
    policy_id: str,
    description: str,
    *,
    acceptance: ProposalAcceptanceRules | None = None,
    counter_proposal: CounterProposalRules | None = None,
    rejection: RejectionRules | None = None,
    commitment: CommitmentRules | None = None,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Proposal mode governance."""
    acc = acceptance or ProposalAcceptanceRules()
    cp = counter_proposal or CounterProposalRules()
    rej = rejection or RejectionRules()
    c = commitment or CommitmentRules()

    rules: dict[str, object] = {
        "acceptance": {"criterion": acc.criterion},
        "counter_proposal": {"max_rounds": cp.max_rounds},
        "rejection": {"terminal_on_any_reject": rej.terminal_on_any_reject},
        "commitment": _commitment_dict(c),
    }

    return policy_pb2.PolicyDescriptor(
        policy_id=policy_id,
        mode="macp.mode.proposal.v1",
        description=description,
        rules=json.dumps(rules).encode(),
        schema_version=1,
    )


# ── Task mode ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskAssignmentRules:
    """Task assignment configuration."""

    allow_reassignment_on_reject: bool = False


@dataclass(frozen=True, slots=True)
class TaskCompletionRules:
    """Task completion configuration."""

    require_output: bool = False


def build_task_policy(
    policy_id: str,
    description: str,
    *,
    assignment: TaskAssignmentRules | None = None,
    completion: TaskCompletionRules | None = None,
    commitment: CommitmentRules | None = None,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Task mode governance."""
    a = assignment or TaskAssignmentRules()
    comp = completion or TaskCompletionRules()
    c = commitment or CommitmentRules()

    rules: dict[str, object] = {
        "assignment": {"allow_reassignment_on_reject": a.allow_reassignment_on_reject},
        "completion": {"require_output": comp.require_output},
        "commitment": _commitment_dict(c),
    }

    return policy_pb2.PolicyDescriptor(
        policy_id=policy_id,
        mode="macp.mode.task.v1",
        description=description,
        rules=json.dumps(rules).encode(),
        schema_version=1,
    )


# ── Handoff mode ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HandoffAcceptanceRules:
    """Handoff acceptance configuration."""

    implicit_accept_timeout_ms: int = 0


def build_handoff_policy(
    policy_id: str,
    description: str,
    *,
    acceptance: HandoffAcceptanceRules | None = None,
    commitment: CommitmentRules | None = None,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Handoff mode governance."""
    acc = acceptance or HandoffAcceptanceRules()
    c = commitment or CommitmentRules()

    rules: dict[str, object] = {
        "acceptance": {"implicit_accept_timeout_ms": acc.implicit_accept_timeout_ms},
        "commitment": _commitment_dict(c),
    }

    return policy_pb2.PolicyDescriptor(
        policy_id=policy_id,
        mode="macp.mode.handoff.v1",
        description=description,
        rules=json.dumps(rules).encode(),
        schema_version=1,
    )
