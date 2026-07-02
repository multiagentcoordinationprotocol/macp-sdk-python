"""Typed policy builders for all MACP governance modes.

Each builder produces a :class:`PolicyDescriptor` with JSON-encoded rules
that match the normative rule schemas defined in RFC-MACP-0012 and the
Rust runtime's ``src/policy/rules.rs``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from macp.v1 import policy_pb2

# ── Shared commitment rules (all modes) ─────────────────────────────


@dataclass(frozen=True, slots=True)
class CommitmentRules:
    """Commitment authority configuration shared by all mode policies."""

    authority: str = "initiator_only"
    designated_roles: list[str] = field(default_factory=list)
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


def build_decision_policy(
    policy_id: str,
    description: str,
    *,
    voting: VotingRules | None = None,
    objection_handling: ObjectionHandlingRules | None = None,
    evaluation: EvaluationRules | None = None,
    commitment: CommitmentRules | None = None,
) -> policy_pb2.PolicyDescriptor:
    """Build a PolicyDescriptor for Decision mode governance."""
    v = voting or VotingRules()
    o = objection_handling or ObjectionHandlingRules()
    e = evaluation or EvaluationRules()
    c = commitment or CommitmentRules()

    voting_section: dict[str, object] = {
        "algorithm": v.algorithm,
        "threshold": v.threshold,
        "quorum": {"type": v.quorum_type or "count", "value": v.quorum_value or 0},
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
        schema_version=2,
    )


# ── Quorum mode ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QuorumThreshold:
    """Quorum threshold configuration (RFC: ``threshold`` object)."""

    type: str = "n_of_m"
    value: float = 0


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
