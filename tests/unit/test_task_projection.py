from __future__ import annotations

import dataclasses
import importlib
import warnings

import pytest
from macp.modes.task.v1 import task_pb2
from macp.v1 import core_pb2

from macp_sdk.constants import MODE_TASK
from macp_sdk.task import TaskProjection, TaskRecord
from tests.conftest import make_envelope


class TestTaskProjection:
    def _proj(self) -> TaskProjection:
        return TaskProjection()

    def test_initial_state(self):
        p = self._proj()
        assert p.phase == "Pending"
        assert len(p.tasks) == 0
        assert not p.is_accepted("t1")
        assert not p.is_completed("t1")
        assert not p.is_failed("t1")

    def test_task_request(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(
                    task_id="t1",
                    title="Analyze data",
                    instructions="run the pipeline",
                    requested_assignee="worker",
                    input=b"payload-bytes",
                    deadline_unix_ms=1234567890,
                ),
                sender="planner",
            )
        )
        task = p.get_task("t1")
        assert task is not None
        assert task.task_id == "t1"
        assert task.status == "requested"
        assert task.progress == 0.0
        assert task.assignee is None
        assert task.deadline_unix_ms == 1234567890
        assert task.input == b"payload-bytes"
        assert task.sender == "planner"
        assert p.phase == "Requested"

    def test_repeat_task_request_fully_resets_the_record(self):
        """A second TaskRequest for an existing task_id replaces the record
        wholesale -- status/progress/assignee/deadline/input all come from
        the new request, not merged with prior state. If the task_id was
        holding the session's active_assignment slot, that slot is freed
        too, keeping the two pieces of per-session state consistent (a
        naive field-reset alone would otherwise desync them and make the
        task permanently unclaimable via ANOMALY_DUPLICATE_TASK_ACCEPT).
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="first"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker1"),
                sender="worker1",
            )
        )
        assert p.current_assignee("t1") == "worker1"
        assert p.active_assignment == ("worker1", "t1")

        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="second"),
                sender="planner",
            )
        )
        task = p.get_task("t1")
        assert task is not None
        assert task.title == "second"
        assert task.status == "requested"
        assert task.progress == 0.0
        assert task.assignee is None
        assert p.active_assignment is None

        # Slot is genuinely free -- a new accept can claim it.
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker2"),
                sender="worker2",
            )
        )
        assert p.current_assignee("t1") == "worker2"
        assert p.anomalies == []

    def test_accept(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker"),
                sender="worker",
            )
        )
        assert p.is_accepted("t1")
        assert p.phase == "InProgress"

    def test_reject(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="t1", assignee="worker", reason="busy"),
                sender="worker",
            )
        )
        assert not p.is_accepted("t1")

    def test_reject_known_task_populates_rejection_record(self):
        """TaskRejectRecord was previously defined and exported but never
        instantiated -- this proves it's actually wired up.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="t1", assignee="worker", reason="busy"),
                sender="worker",
            )
        )
        assert len(p.rejections) == 1
        rejection = p.rejections[0]
        assert rejection.task_id == "t1"
        assert rejection.assignee == "worker"
        assert rejection.reason == "busy"

    def test_update(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskUpdate",
                task_pb2.TaskUpdatePayload(
                    task_id="t1", status="running", progress=0.5, message="halfway"
                ),
                sender="worker",
            )
        )
        assert len(p.updates) == 1
        assert p.latest_progress() == 0.5

    def test_complete(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskComplete",
                task_pb2.TaskCompletePayload(
                    task_id="t1", assignee="worker", summary="done", output=b"result"
                ),
                sender="worker",
            )
        )
        assert p.is_completed("t1")
        assert not p.is_failed("t1")
        assert p.phase == "Completed"
        assert p.progress_of("t1") == 1.0

    def test_fail(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskFail",
                task_pb2.TaskFailPayload(
                    task_id="t1",
                    assignee="worker",
                    error_code="TIMEOUT",
                    reason="too slow",
                    retryable=True,
                ),
                sender="worker",
            )
        )
        assert p.is_failed("t1")
        assert not p.is_completed("t1")
        assert p.is_retryable("t1")
        assert p.phase == "Failed"

    def test_session_scoped_slot_blocks_second_accept(self):
        """A second TaskAccept from a different sender while the session's
        one assignee slot is already held is a no-op on state (RFC-MACP-0009
        §5 rule 3 — session-scoped, not per-task), even for a different
        known task. The transcript still records both messages.
        """
        p = self._proj()
        for task_id in ("t1", "t2"):
            p.apply_envelope(
                make_envelope(
                    MODE_TASK,
                    "TaskRequest",
                    task_pb2.TaskRequestPayload(task_id=task_id, title="x"),
                    sender="planner",
                )
            )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker1"),
                sender="worker1",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t2", assignee="worker2"),
                sender="worker2",
            )
        )
        assert p.current_assignee("t1") == "worker1"
        assert p.current_assignee("t2") is None
        assert p.current_status("t2") == "requested"
        assert len(p.transcript) == 4

    def test_second_task_accept_while_slot_held_records_anomaly(self):
        """Issue #94: a competing TaskAccept discarded because the
        session's assignee slot is already held records a
        `duplicate_task_accept` ProjectionAnomaly -- unlike an unknown
        task_id (test_task_accept_unknown_task_is_noop below), which does
        not, since it isn't caller misuse.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker1"),
                sender="worker1",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker2"),
                sender="worker2",
            )
        )
        assert p.current_assignee("t1") == "worker1"
        assert len(p.anomalies) == 1
        anomaly = p.anomalies[0]
        assert anomaly.kind == "duplicate_task_accept"
        assert anomaly.subject_id == "t1"
        assert anomaly.sender == "worker2"

    def test_duplicate_task_accept_detail_names_the_held_task_not_the_incoming_one(self):
        """The anomaly's detail must name the task the slot is actually held
        for, not just the incoming (different) task_id the competing
        TaskAccept named -- see task.py's held_task_id/held_sender fix.
        """
        p = self._proj()
        for task_id in ("t1", "t2"):
            p.apply_envelope(
                make_envelope(
                    MODE_TASK,
                    "TaskRequest",
                    task_pb2.TaskRequestPayload(task_id=task_id, title="x"),
                    sender="planner",
                )
            )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker1"),
                sender="worker1",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="t2", assignee="worker2"),
                sender="worker2",
            )
        )
        anomaly = p.anomalies[0]
        assert anomaly.subject_id == "t2"  # the incoming (discarded) TaskAccept's own task_id
        assert "worker1" in anomaly.detail
        assert "t1" in anomaly.detail  # the actually-held task, not just "t2"

    def test_late_task_complete_after_commitment_does_not_regress_phase(self):
        """Issue #93 item 5: a mode message arriving after Commitment must
        not move ``phase`` back out of "Committed" -- same terminality
        guard as DecisionProjection's, exercised end-to-end here rather than
        only via the synthetic projection in test_base_projection.py.

        Uses ``TaskComplete``, not ``TaskUpdate``: only ``TaskRequest``
        (:120), ``TaskAccept`` (:132), ``TaskComplete`` (:215), and
        ``TaskFail`` (:232) call ``_set_phase`` in task.py --
        ``TaskUpdate`` never touches ``phase`` at all, so a test built on
        it would pass even with the guard entirely missing (confirmed: an
        independent review reverted every ``_set_phase`` call in this file
        to a direct ``self.phase =`` assignment and the suite stayed
        green with the old ``TaskUpdate``-based version of this test).
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "Commitment",
                core_pb2.CommitmentPayload(
                    commitment_id="c1", action="commit", authority_scope="session"
                ),
            )
        )
        assert p.phase == "Committed"
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskComplete",
                task_pb2.TaskCompletePayload(task_id="t1", assignee="worker1", summary="done"),
                sender="worker1",
            )
        )
        assert p.phase == "Committed"
        assert p.is_completed("t1")  # the complete's own effect is not suppressed

    def test_task_accept_unknown_task_is_noop(self):
        """A TaskAccept for a task_id never seen in a TaskRequest must not
        raise, must not fabricate state, and must not move ``phase``. Also
        must NOT record a ProjectionAnomaly (issue #94) -- an unknown
        task_id can legitimately mean a projection that joined mid-session
        and never saw the TaskRequest, which is not caller misuse.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="ghost", assignee="worker"),
                sender="worker",
            )
        )
        assert p.current_assignee("ghost") is None
        assert p.current_status("ghost") is None
        assert p.phase == "Pending"
        assert p.anomalies == []

    def test_task_reject_unknown_task_is_noop(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="ghost", assignee="worker", reason="n/a"),
                sender="worker",
            )
        )
        assert p.current_status("ghost") is None
        assert p.phase == "Pending"
        # The rejection record itself is kept unconditionally, matching
        # updates/completions/failures' own audit-trail convention.
        assert len(p.rejections) == 1

    def test_task_update_unknown_task_is_noop(self):
        """A TaskUpdate for a task_id never seen in a TaskRequest must not
        fabricate per-task status/progress state, but the update record
        itself is still kept (matches task.ts's unconditional push) -- a
        deliberate carve-out, not a residual bug (#78).
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskUpdate",
                task_pb2.TaskUpdatePayload(
                    task_id="ghost", status="running", progress=0.5, message="halfway"
                ),
                sender="worker",
            )
        )
        assert p.current_status("ghost") is None
        assert p.progress_of("ghost") == 0.0
        assert p.phase == "Pending"
        assert len(p.updates) == 1
        assert p.latest_progress() == 0.5

    def test_task_complete_unknown_task_is_noop(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskComplete",
                task_pb2.TaskCompletePayload(
                    task_id="ghost", assignee="worker", summary="done", output=b"result"
                ),
                sender="worker",
            )
        )
        assert p.current_status("ghost") is None
        assert p.phase == "Pending"
        assert len(p.completions) == 1
        assert p.is_completed("ghost") is False

    def test_task_fail_unknown_task_is_noop(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskFail",
                task_pb2.TaskFailPayload(
                    task_id="ghost",
                    assignee="worker",
                    error_code="TIMEOUT",
                    reason="too slow",
                    retryable=True,
                ),
                sender="worker",
            )
        )
        assert p.current_status("ghost") is None
        assert p.phase == "Pending"
        assert len(p.failures) == 1
        # is_retryable reads the record list directly, not per-task view
        # state, so it is a deliberate carve-out, unaffected by the gate.
        assert p.is_retryable("ghost") is True

    def test_update_known_task_sets_status_and_progress(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskUpdate",
                task_pb2.TaskUpdatePayload(
                    task_id="t1", status="running", progress=0.5, message="halfway"
                ),
                sender="worker",
            )
        )
        assert p.current_status("t1") == "in_progress"
        assert p.progress_of("t1") == 0.5

    def test_reject_from_non_slot_holder_sets_status_but_keeps_slot(self):
        """A TaskReject(task_id=B) from a sender who does NOT hold the
        session's slot still sets B's status to 'rejected' (gated only on
        B being a known task) but must not clear the slot-holder's assignee.
        """
        p = self._proj()
        for task_id in ("A", "B"):
            p.apply_envelope(
                make_envelope(
                    MODE_TASK,
                    "TaskRequest",
                    task_pb2.TaskRequestPayload(task_id=task_id, title="x"),
                    sender="planner",
                )
            )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker1"),
                sender="worker1",
            )
        )
        # A different sender (never held the slot) rejects B.
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="B", assignee="worker2", reason="busy"),
                sender="worker2",
            )
        )
        assert p.current_status("B") == "rejected"
        assert p.current_assignee("A") == "worker1"

    def test_reject_from_slot_holder_frees_held_task_not_named_task(self):
        """A TaskReject(task_id=B) from the session's current slot-holder,
        where the slot is held for a DIFFERENT task A, sets B's status to
        'rejected' AND frees A's assignee — slot-freeing matches on sender
        alone, not on the rejected task_id equaling the held task.
        """
        p = self._proj()
        for task_id in ("A", "B"):
            p.apply_envelope(
                make_envelope(
                    MODE_TASK,
                    "TaskRequest",
                    task_pb2.TaskRequestPayload(task_id=task_id, title="x"),
                    sender="planner",
                )
            )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker1"),
                sender="worker1",
            )
        )
        # The slot-holder (worker1) sends a reject naming B, not A.
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="B", assignee="worker1", reason="changed mind"),
                sender="worker1",
            )
        )
        assert p.current_status("B") == "rejected"
        assert p.current_assignee("A") is None
        # The slot is free again — a new sender can now claim it.
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker3"),
                sender="worker3",
            )
        )
        assert p.current_assignee("A") == "worker3"

    def test_reject_from_slot_holder_same_task(self):
        """The ordinary single-task case: slot-holder rejects the very task
        they hold — status goes to 'rejected' and the slot frees.
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="A", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker1"),
                sender="worker1",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="A", assignee="worker1", reason="busy"),
                sender="worker1",
            )
        )
        assert p.current_status("A") == "rejected"
        assert p.current_assignee("A") is None

    def test_reject_unknown_task_from_slot_holder_still_frees_slot(self):
        """Slot-freeing is gated on sender alone, independent of whether the
        rejected task_id is known — a TaskReject naming an unknown task_id
        still frees the sender's held slot (matches task.ts:158-161, where
        slot-freeing is a separate, unconditional check from the status
        write's task-existence gate).
        """
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="A", title="x"),
                sender="planner",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker1"),
                sender="worker1",
            )
        )
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskReject",
                task_pb2.TaskRejectPayload(task_id="ghost", assignee="worker1", reason="n/a"),
                sender="worker1",
            )
        )
        # The unknown task_id itself never gets a status.
        assert p.current_status("ghost") is None
        # But the slot-holder's slot is freed all the same.
        assert p.current_assignee("A") is None
        # Confirm the slot is genuinely free — a new sender can now claim it.
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskAccept",
                task_pb2.TaskAcceptPayload(task_id="A", assignee="worker2"),
                sender="worker2",
            )
        )
        assert p.current_assignee("A") == "worker2"

    def test_active_tasks(self):
        p = self._proj()
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskRequest",
                task_pb2.TaskRequestPayload(task_id="t1", title="x"),
                sender="planner",
            )
        )
        assert len(p.active_tasks()) == 1
        p.apply_envelope(
            make_envelope(
                MODE_TASK,
                "TaskComplete",
                task_pb2.TaskCompletePayload(task_id="t1", assignee="worker"),
                sender="worker",
            )
        )
        assert len(p.active_tasks()) == 0


class TestReplayIdempotence:
    """Regression coverage for issue #43 Phase 2 — replay inflation.

    Separate bug from vote/ballot cardinality: BaseProjection.apply_envelope's
    message_id dedup guard (Phase 1) also fixes seven previously-unguarded
    ``.append(`` sites across Decision/Proposal/Task, including this file's
    ``updates`` (task.py:188), ``completions`` (task.py:204), and ``failures``
    (task.py:221).

    Real-world trigger: src/macp_sdk/agent/transports.py:60 subscribes with
    after_sequence defaulting to 0, so every (re)subscribe replays the full
    accepted history, and Participant.run() (participant.py:483) has no
    re-entry guard — a supervisor restarting run() re-feeds the whole history
    into the same projection object.

    | Test type   | Requires                         | How                                   |
    |-------------|-----------------------------------|----------------------------------------|
    | Redelivery  | the SAME non-empty message_id     | reuse the same envelope object, or an  |
    |             |                                    | explicit shared message_id=            |
    | Distinctness| two DIFFERENT non-empty ids       | two make_envelope(...) calls (default) |

    Distinctness is not exercised by this class — that coverage lives in
    tests/unit/test_base_projection.py::TestIdempotentApply::
    test_distinct_message_ids_both_applied.

    Every test below is a redelivery test, so every one reuses the same
    envelope object — a test that calls make_envelope twice gets two
    different uuid4 message_ids, dedup never engages, and the test would
    pass while proving nothing.
    """

    def _proj(self) -> TaskProjection:
        return TaskProjection()

    def test_redelivered_task_update_is_noop(self):
        # Trigger: agent/transports.py:60 (after_sequence=0 full replay) +
        # participant.py:483 (run() has no re-entry guard).
        p = self._proj()
        env = make_envelope(
            MODE_TASK,
            "TaskUpdate",
            task_pb2.TaskUpdatePayload(task_id="t1", status="running", progress=0.5),
            sender="worker",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        assert len(p.updates) == 1
        assert len(p.transcript) == 1

    def test_redelivered_task_complete_is_noop(self):
        # Trigger: agent/transports.py:60 + participant.py:483.
        p = self._proj()
        env = make_envelope(
            MODE_TASK,
            "TaskComplete",
            task_pb2.TaskCompletePayload(task_id="t1", assignee="worker", summary="done"),
            sender="worker",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        assert len(p.completions) == 1
        assert len(p.transcript) == 1

    def test_redelivered_task_fail_is_noop(self):
        # Trigger: agent/transports.py:60 + participant.py:483.
        p = self._proj()
        env = make_envelope(
            MODE_TASK,
            "TaskFail",
            task_pb2.TaskFailPayload(
                task_id="t1", assignee="worker", error_code="E1", reason="boom", retryable=True
            ),
            sender="worker",
        )
        p.apply_envelope(env)
        p.apply_envelope(env)
        assert len(p.failures) == 1
        assert len(p.transcript) == 1

    def test_replayed_slot_claim_is_deterministic(self):
        """Redelivering an already-applied TaskAccept after the slot has
        since been freed must stay a no-op (dedup by message_id) — without
        dedup, replaying the original accept would incorrectly re-claim the
        now-free slot for its sender. This is what makes the redelivery
        genuinely exercise the dedup guard rather than passing vacuously.
        """
        p = self._proj()
        request = make_envelope(
            MODE_TASK,
            "TaskRequest",
            task_pb2.TaskRequestPayload(task_id="t1", title="x"),
            sender="planner",
        )
        accept = make_envelope(
            MODE_TASK,
            "TaskAccept",
            task_pb2.TaskAcceptPayload(task_id="t1", assignee="worker"),
            sender="worker",
        )
        reject = make_envelope(
            MODE_TASK,
            "TaskReject",
            task_pb2.TaskRejectPayload(task_id="t1", assignee="worker", reason="busy"),
            sender="worker",
        )
        p.apply_envelope(request)
        p.apply_envelope(accept)
        p.apply_envelope(reject)
        assert p.current_assignee("t1") is None
        # Redeliver the original accept (same envelope object — same
        # message_id, already recorded). Without dedup this would re-claim
        # the now-free slot for "worker".
        p.apply_envelope(accept)
        assert p.current_assignee("t1") is None
        assert p.current_status("t1") == "rejected"
        assert len(p.transcript) == 3


class TestDeprecatedAliases:
    """Issue #108: ``TaskRequestRecord`` -> ``TaskRecord``, kept as a
    deprecated lazy alias.

    ``macp_sdk.task`` is a plain module (no ``__path__``): a `from ...
    import OldName` resolves via a single ``getattr`` call, one warning.
    ``macp_sdk`` (top-level) is a package: CPython's import machinery probes
    it with an internal ``hasattr`` call before the statement's own getattr,
    so the same import shape fires the module's ``__getattr__`` twice — same
    asymmetry issue #103 established empirically, see
    ``src/macp_sdk/task.py``'s own alias comment. Both counts are asserted
    here, not "exactly one" uniformly.
    """

    def test_task_request_record_alias_from_task_module(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk.task import TaskRequestRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 1
        assert "TaskRecord" in str(deprecation_warnings[0].message)
        task_module = importlib.import_module("macp_sdk.task")
        assert TaskRequestRecord is task_module.TaskRecord

    def test_task_request_record_alias_from_macp_sdk_package(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from macp_sdk import TaskRequestRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 2
        assert all("TaskRecord" in str(w.message) for w in deprecation_warnings)
        macp_sdk = importlib.import_module("macp_sdk")
        assert TaskRequestRecord is macp_sdk.TaskRecord

    def test_repeated_plain_attribute_access_warns_each_time(self):
        task_module = importlib.import_module("macp_sdk.task")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = task_module.TaskRequestRecord
            _ = task_module.TaskRequestRecord

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 2

    def test_unrecognized_name_still_raises_attribute_error(self):
        task_module = importlib.import_module("macp_sdk.task")
        macp_sdk = importlib.import_module("macp_sdk")
        try:
            _ = task_module.TotallyBogusName
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")
        try:
            _ = macp_sdk.TotallyBogusName
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")


class TestRequesterDeprecatedAlias:
    """Issue #120/#121 Phase 14: ``TaskRecord.requester`` -> ``.sender``,
    kept as a read-only INSTANCE property alias -- same mechanism as
    ``ProposalRecord.proposer`` (proposal.py), applied here to the second
    remaining ``requester`` field.
    """

    def _record(self, **overrides):
        fields = {
            "task_id": "t1",
            "title": "t",
            "instructions": "i",
            "requested_assignee": "worker",
            "sender": "planner",
        }
        fields.update(overrides)
        return TaskRecord(**fields)

    def test_sender_is_a_real_dataclass_field_requester_is_not(self):
        names = {f.name for f in dataclasses.fields(TaskRecord)}
        assert "sender" in names
        assert "requester" not in names

    def test_requester_returns_sender_value_and_warns_every_access(self):
        record = self._record(sender="planner")
        for _ in range(2):  # not just once -- prove it's not a warn-once cache
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                value = record.requester
            assert value == "planner" == record.sender
            deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 1
            msg = str(deprecation_warnings[0].message)
            assert "TaskRecord.requester" in msg
            assert "TaskRecord.sender" in msg

    def test_requester_has_no_setter(self):
        record = self._record()
        with pytest.raises(AttributeError):
            record.requester = "mallory"

    def test_constructor_rejects_requester_kwarg(self):
        with pytest.raises(TypeError):
            TaskRecord(
                task_id="t1",
                title="t",
                instructions="i",
                requested_assignee="worker",
                requester="planner",
            )
