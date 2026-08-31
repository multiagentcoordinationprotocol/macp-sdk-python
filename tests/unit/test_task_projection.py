from __future__ import annotations

from macp.modes.task.v1 import task_pb2

from macp_sdk.constants import MODE_TASK
from macp_sdk.task import TaskProjection
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
                ),
                sender="planner",
            )
        )
        assert p.get_task("t1") is not None
        assert p.get_task("t1").task_id == "t1"
        assert p.phase == "Requested"

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
    ``updates`` (task.py:123), ``completions`` (task.py:138), and ``failures``
    (task.py:154).

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
