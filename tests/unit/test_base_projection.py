"""Direct unit tests for the shared ``BaseProjection`` ABC."""

from __future__ import annotations

import pytest
from google.protobuf.message import DecodeError
from macp.v1 import core_pb2

from macp_sdk.base_projection import (
    ANOMALY_DUPLICATE_BALLOT,
    ANOMALY_DUPLICATE_VOTE,
    BaseProjection,
    ProjectionAnomaly,
)
from tests.conftest import make_envelope

TEST_MODE = "macp.mode.test.v1"


class _Projection(BaseProjection):
    MODE = TEST_MODE

    def __init__(self) -> None:
        super().__init__()
        self.mode_messages: list[str] = []

    def _apply_mode_message(self, envelope) -> None:
        self.mode_messages.append(envelope.message_type)


class _TranscriptMutatingProjection(BaseProjection):
    """A projection whose ``_apply_mode_message`` appends directly to
    ``self.transcript`` before raising, then raises. Simulates the
    subclass-append hazard the identity-guarded pop in apply_envelope's
    except block defends against (no subclass in this SDK does this today
    — every one mutates only its own collections — but BaseProjection is a
    public ABC third parties may subclass differently).
    """

    MODE = TEST_MODE

    def _apply_mode_message(self, envelope) -> None:
        self.transcript.append("sentinel-not-an-envelope")
        raise RuntimeError("boom: mutated transcript before raising")


class _FlakyProjection(BaseProjection):
    """A projection whose ``_apply_mode_message`` fails a fixed number of
    times before succeeding, for exercising apply_envelope's rollback path.
    """

    MODE = TEST_MODE

    def __init__(self, fail_times: int = 1) -> None:
        super().__init__()
        self.mode_messages: list[str] = []
        self._fail_remaining = fail_times

    def _apply_mode_message(self, envelope) -> None:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("boom: simulated apply failure")
        self.mode_messages.append(envelope.message_type)


def _commitment(outcome_positive: bool = True) -> core_pb2.CommitmentPayload:
    return core_pb2.CommitmentPayload(
        commitment_id="c1",
        action="done",
        authority_scope="test",
        reason="ok",
        outcome_positive=outcome_positive,
    )


class TestApplyEnvelope:
    def test_wrong_mode_envelope_ignored(self):
        proj = _Projection()
        env = make_envelope("macp.mode.other.v1", "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        assert proj.transcript == []
        assert proj.mode_messages == []

    def test_matching_mode_appended_and_dispatched(self):
        proj = _Projection()
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.mode_messages == ["Proposal"]

    def test_commitment_not_dispatched_to_mode_handler(self):
        proj = _Projection()
        proj.apply_envelope(make_envelope(TEST_MODE, "Commitment", _commitment()))
        assert proj.mode_messages == []


class TestCommitmentState:
    def test_initial_state(self):
        proj = _Projection()
        assert not proj.is_committed
        assert proj.commitment is None
        assert proj.phase == ""
        assert proj.is_positive_outcome is None

    def test_commitment_sets_state(self):
        proj = _Projection()
        proj.apply_envelope(make_envelope(TEST_MODE, "Commitment", _commitment()))
        assert proj.is_committed
        assert proj.phase == "Committed"
        assert proj.commitment is not None
        assert proj.commitment.commitment_id == "c1"
        assert proj.is_positive_outcome is True

    def test_negative_outcome_surfaces(self):
        proj = _Projection()
        proj.apply_envelope(
            make_envelope(TEST_MODE, "Commitment", _commitment(outcome_positive=False))
        )
        assert proj.is_committed
        assert proj.is_positive_outcome is False


class TestIdempotentApply:
    """message_id-idempotent apply (RFC-MACP-0006 §3.2).

    Two setups are easy to swap by accident and both pass while proving
    nothing if swapped:
      - cardinality/distinctness tests need two envelopes with DIFFERENT
        non-empty message_ids (two make_envelope(...) calls, relying on the
        default fresh uuid4 per call).
      - redelivery/dedup tests need the SAME non-empty message_id applied
        twice — reuse the same envelope object (or pass an explicit shared
        message_id=) rather than calling make_envelope twice.
    """

    def test_same_envelope_applied_twice_is_noop(self):
        proj = _Projection()
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.mode_messages == ["Proposal"]

    def test_distinct_message_ids_both_applied(self):
        proj = _Projection()
        env1 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        env2 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        assert env1.message_id != env2.message_id
        proj.apply_envelope(env1)
        proj.apply_envelope(env2)
        assert len(proj.transcript) == 2

    def test_empty_message_id_never_deduped(self):
        # message_id="" is the proto3 default, not an identity: it means "no id
        # was set," not "these two envelopes are the same message." Treating it
        # as a dedup key would collapse every id-less envelope into one.
        proj = _Projection()
        env1 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload(), message_id="")
        env2 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload(), message_id="")
        assert env1.message_id == ""
        assert env2.message_id == ""
        proj.apply_envelope(env1)
        proj.apply_envelope(env2)
        assert len(proj.transcript) == 2

    def test_wrong_mode_envelope_does_not_poison_seen_set(self):
        proj = _Projection()
        env = make_envelope("macp.mode.other.v1", "Proposal", core_pb2.SessionStartPayload())
        proj.apply_envelope(env)
        assert proj.transcript == []

        # Same object, mode corrected in place, same message_id: the first
        # (wrong-mode) call must never have entered _seen_message_ids, or this
        # second call would be wrongly treated as a redelivery.
        env.mode = TEST_MODE
        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.mode_messages == ["Proposal"]

    def test_commitment_redelivery_is_noop(self):
        proj = _Projection()
        env = make_envelope(TEST_MODE, "Commitment", _commitment())
        proj.apply_envelope(env)
        proj.apply_envelope(env)
        assert proj.is_committed
        assert len(proj.transcript) == 1

    def test_make_envelope_ids_are_unique_by_default(self):
        env1 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        env2 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        assert env1.message_id != env2.message_id
        assert env1.message_id != ""
        assert env2.message_id != ""


class TestAtomicApply:
    """apply_envelope must roll back its own bookkeeping on failure, so a
    caller can retry the same envelope and have it apply.

    Deliberately NOT "atomic": the rollback covers ``transcript`` and
    ``_seen_message_ids`` only. ``self.phase`` and subclass collections are
    not restored -- see the rollback-scope comment in ``base_projection.py``
    for why the narrow guarantee is sufficient today (every subclass raises
    before it mutates) and what future ``_apply_mode_message`` implementations
    must preserve to keep it so.

    Regression coverage for the partial-apply wedge: before this fix,
    ``_seen_message_ids.add(...)`` and ``transcript.append(...)`` both ran
    BEFORE the actual effect (Commitment ParseFromString, or the subclass's
    ``_apply_mode_message`` dispatch). If that effect raised, the id was
    already marked seen and the envelope already sat in transcript, even
    though its effect never applied — so a retry of the SAME envelope (e.g.
    a supervisor resubscribing after a crash, replaying accepted history
    from the start per agent/transports.py:60, back into
    agent/participant.py:410's unguarded apply_envelope call) would be
    silently swallowed by the dedup gate rather than recovering.
    """

    def test_failed_apply_rolls_back_then_retry_recovers(self):
        proj = _FlakyProjection(fail_times=1)
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())

        with pytest.raises(RuntimeError):
            proj.apply_envelope(env)

        # Rolled back completely: neither the transcript append nor the
        # seen-set add from the failed call survives.
        assert proj.transcript == []
        assert env.message_id not in proj._seen_message_ids
        assert proj.mode_messages == []

        # A retry with the SAME envelope object is not swallowed as a
        # "redelivery" — it fully applies, because the failed first attempt
        # left no trace behind.
        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.transcript[0] is env
        assert proj.mode_messages == ["Proposal"]

    def test_failed_commitment_parse_rolls_back(self):
        # Malformed Commitment payload: ParseFromString raises DecodeError,
        # reached via the same try/except as the _apply_mode_message branch.
        proj = _Projection()
        env = make_envelope(TEST_MODE, "Commitment", core_pb2.SessionStartPayload())
        env.payload = b"\xff\xfe\x00not-a-valid-commitment-payload"

        with pytest.raises(DecodeError):
            proj.apply_envelope(env)

        assert proj.transcript == []
        assert env.message_id not in proj._seen_message_ids
        assert proj.commitment is None
        assert proj.phase == ""
        assert not proj.is_committed

        # Retry with a valid payload on the same envelope object now
        # applies cleanly — proves the failed attempt left no trace.
        env.payload = _commitment().SerializeToString()
        proj.apply_envelope(env)
        assert proj.is_committed
        assert len(proj.transcript) == 1

    def test_original_exception_propagates_unchanged(self):
        # The rollback must not mask or replace the original error.
        proj = _FlakyProjection(fail_times=1)
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        with pytest.raises(RuntimeError, match=r"^boom: simulated apply failure$"):
            proj.apply_envelope(env)

    def test_except_block_pop_guard_skips_non_matching_last_entry(self):
        # Covers the false arm of the identity guard
        # (`self.transcript[-1] is envelope`) in apply_envelope's except
        # block. No subclass in this SDK appends to self.transcript itself,
        # but the guard exists precisely because BaseProjection is a public
        # ABC third parties may subclass differently — this test proves the
        # guard actually declines to pop when the last entry isn't the
        # envelope this call appended, rather than corrupting it.
        proj = _TranscriptMutatingProjection()
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())

        with pytest.raises(RuntimeError, match=r"^boom: mutated transcript before raising$"):
            proj.apply_envelope(env)

        # Neither entry was popped: transcript[-1] was the sentinel, not
        # env, so the guard correctly declined to pop anything.
        assert proj.transcript == [env, "sentinel-not-an-envelope"]
        # The seen-set rollback is independent of the pop guard and still
        # runs, so the message_id is still released for retry.
        assert env.message_id not in proj._seen_message_ids

    def test_failed_apply_with_empty_message_id_rolls_back_transcript_only(self):
        # message_id="" never enters _seen_message_ids in the first place
        # (the empty-id guard), so the rollback's "only discard the seen-set
        # entry if this call added one" branch must not touch it here.
        proj = _FlakyProjection(fail_times=1)
        env = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload(), message_id="")

        with pytest.raises(RuntimeError):
            proj.apply_envelope(env)

        assert proj.transcript == []
        assert proj._seen_message_ids == set()

        proj.apply_envelope(env)
        assert len(proj.transcript) == 1
        assert proj.mode_messages == ["Proposal"]


class TestDocstringContract:
    """Phase 1 acceptance criterion 7 (plan's D4 decision): apply_envelope's
    docstring must name RFC-MACP-0006, "accepted", and "undefined" — these
    are contract elements the plan requires be stated explicitly, not
    incidental wording. Verified by inspection when D4 landed, but nothing
    guarded against a future rewrite silently dropping them; this test is
    that guard.
    """

    def test_docstring_names_rfc_and_acceptance_and_undefined_contract(self):
        doc = BaseProjection.apply_envelope.__doc__
        assert doc is not None
        assert "RFC-MACP-0006" in doc
        assert "accepted" in doc
        assert "undefined" in doc


class TestReplayDeterminism:
    """Phase-1-level check of the docstring's determinism claim: replaying
    ``transcript`` through a fresh projection reproduces the same state.
    The richer, anomaly-inclusive version of this claim is Phase 4's job;
    this only exercises what Phase 1 actually changed (dedup on message_id).
    """

    def test_replay_of_transcript_reproduces_mode_messages(self):
        proj = _Projection()

        env1 = make_envelope(TEST_MODE, "Proposal", core_pb2.SessionStartPayload())
        env2 = make_envelope(TEST_MODE, "Evaluation", core_pb2.SessionStartPayload())
        redelivery = env1  # same object/message_id as env1 — a resubscribe replay

        proj.apply_envelope(env1)
        proj.apply_envelope(env2)
        proj.apply_envelope(redelivery)

        assert len(proj.transcript) == 2
        assert proj.mode_messages == ["Proposal", "Evaluation"]

        replay = _Projection()
        for env in proj.transcript:
            replay.apply_envelope(env)

        assert len(replay.transcript) == len(proj.transcript)
        assert replay.mode_messages == proj.mode_messages


class TestProjectionAnomaly:
    """Phase 3: the ProjectionAnomaly public surface is INERT here.

    Nothing in production code calls ``_record_anomaly`` yet -- that is
    Phase 4. These tests only prove the contract shape and the recording
    mechanism itself, using the `_Projection` test harness directly.
    """

    def test_contract_field_names_and_order(self):
        # This is the byte-for-byte cross-SDK contract with
        # macp-sdk-typescript#55: field names AND their declaration order.
        # If this assertion fails, it means the contract has been broken --
        # do NOT "fix" this test to match a reordered/renamed dataclass.
        # Coordinate with the TypeScript SDK side first.
        assert list(ProjectionAnomaly.__annotations__) == [
            "kind",
            "mode",
            "message_type",
            "message_id",
            "sender",
            "subject_id",
            "detail",
        ]
        # Note: this module has `from __future__ import annotations`, so
        # `ProjectionAnomaly.__annotations__.values()` are the *strings*
        # "str", never the type `str` -- an `annotation is str` check here
        # would be silently dead code, and only a `== "str"` spelling
        # comparison would carry the assert. Resolve through
        # `typing.get_type_hints()` instead so this genuinely checks the
        # resolved field *type*, not the annotation's source spelling.
        import typing

        resolved = typing.get_type_hints(ProjectionAnomaly)
        for name in ProjectionAnomaly.__annotations__:
            assert resolved[name] is str, name

    def test_only_detail_has_a_default(self):
        import dataclasses

        no_default = {
            f.name
            for f in dataclasses.fields(ProjectionAnomaly)
            if f.default is dataclasses.MISSING
        }
        assert no_default == {
            "kind",
            "mode",
            "message_type",
            "message_id",
            "sender",
            "subject_id",
        }
        detail_field = next(f for f in dataclasses.fields(ProjectionAnomaly) if f.name == "detail")
        assert detail_field.default == ""

    def test_kind_constants_are_exact_strings(self):
        # Asserted against string literals, not by reference to themselves --
        # a typo in the constant's value would pass an `assert X == X` check.
        assert ANOMALY_DUPLICATE_VOTE == "duplicate_vote"
        assert ANOMALY_DUPLICATE_BALLOT == "duplicate_ballot"

    def test_frozen_and_slots(self):
        assert ProjectionAnomaly.__dataclass_params__.frozen is True
        assert hasattr(ProjectionAnomaly, "__slots__")

        anomaly = ProjectionAnomaly(
            kind=ANOMALY_DUPLICATE_VOTE,
            mode=TEST_MODE,
            message_type="Vote",
            message_id="m1",
            sender="alice",
            subject_id="p1",
        )
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            anomaly.kind = "changed"  # type: ignore[misc]

    def test_detail_defaults_to_empty_string(self):
        anomaly = ProjectionAnomaly(
            kind=ANOMALY_DUPLICATE_VOTE,
            mode=TEST_MODE,
            message_type="Vote",
            message_id="m1",
            sender="alice",
            subject_id="p1",
        )
        assert anomaly.detail == ""

    def test_initial_state_has_no_anomalies(self):
        proj = _Projection()
        assert proj.anomalies == []
        assert proj.has_anomalies is False

    def test_record_anomaly_appends_and_warns_once(self, caplog):
        import logging

        proj = _Projection()
        with caplog.at_level(logging.WARNING, logger="macp_sdk"):
            proj._record_anomaly(
                kind=ANOMALY_DUPLICATE_VOTE,
                message_type="Vote",
                message_id="m1",
                sender="alice",
                subject_id="p1",
                detail="kept first vote 'APPROVE'; discarded 'REJECT'",
            )

        assert len(proj.anomalies) == 1
        assert proj.has_anomalies is True
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

        # Pins a deliberate contract from the plan: `_record_anomaly` must
        # use lazy `%`-formatting (`logger.warning("...%s...", a, b, ...)`)
        # rather than an f-string or pre-interpolated string, so the message
        # is never built when the WARNING level is disabled. The count
        # assertion above would keep passing if a future edit switched to an
        # f-string; these two do not -- an f-string bakes the values into
        # `record.msg` and leaves `record.args` empty.
        record = warnings[0]
        assert record.args, "expected non-empty record.args (lazy %-formatting, not an f-string)"
        assert "%s" in record.msg, "expected unformatted %s placeholders in record.msg"

    def test_record_anomaly_fills_mode_from_projection(self):
        proj = _Projection()
        proj._record_anomaly(
            kind=ANOMALY_DUPLICATE_BALLOT,
            message_type="Approve",
            message_id="m2",
            sender="bob",
            subject_id="r1",
        )
        assert proj.anomalies[0].mode == TEST_MODE
        assert proj.anomalies[0].mode == proj.MODE
