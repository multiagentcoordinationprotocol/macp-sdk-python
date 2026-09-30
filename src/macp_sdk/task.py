from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from macp.modes.task.v1 import task_pb2
from macp.v1 import envelope_pb2

from .auth import AuthConfig
from .base_projection import ANOMALY_DUPLICATE_TASK_ACCEPT, BaseProjection
from .base_session import BaseSession
from .constants import MODE_TASK
from .envelope import build_envelope, serialize_message
from .validation import validate_required_field

# ---------------------------------------------------------------------------
# Projection records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    instructions: str
    requested_assignee: str
    requester: str
    status: str = "requested"
    progress: float = 0.0
    assignee: str | None = None
    deadline_unix_ms: int = 0
    input: bytes = b""


@dataclass(slots=True)
class TaskRejectRecord:
    task_id: str
    assignee: str
    reason: str


@dataclass(slots=True)
class TaskUpdateRecord:
    task_id: str
    status: str
    progress: float
    message: str


@dataclass(slots=True)
class TaskCompleteRecord:
    task_id: str
    assignee: str
    summary: str
    output: bytes


@dataclass(slots=True)
class TaskFailRecord:
    task_id: str
    assignee: str
    error_code: str
    reason: str
    retryable: bool


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class TaskProjection(BaseProjection):
    """In-process state tracking for Task mode sessions.

    Supports multiple tasks within a single session. Each task's status and
    progress is tracked independently, but the *assignee slot* is scoped to
    the whole session, not to a task: only one participant may hold it at a
    time (RFC-MACP-0009 §5 rule 3), tracked by ``active_assignment``.
    """

    MODE = MODE_TASK

    def __init__(self) -> None:
        super().__init__()
        self.phase = "Pending"
        self.tasks: dict[str, TaskRecord] = {}
        self.updates: list[TaskUpdateRecord] = []
        self.rejections: list[TaskRejectRecord] = []
        self.completions: list[TaskCompleteRecord] = []
        self.failures: list[TaskFailRecord] = []
        # Session-scoped single-assignee slot (RFC-MACP-0009 §5 rule 3: "Only
        # one assignee may become active for the Session in base v1" — scoped
        # to the whole session, not to a task_id). This is the exclusivity
        # guard; each task's own ``assignee`` field on ``self.tasks`` remains
        # the per-task reporting view.
        # Mirrors typescript-sdk's ``activeAssignment`` field.
        self.active_assignment: tuple[str, str] | None = None  # (sender, task_id)

    def _apply_mode_message(self, envelope: envelope_pb2.Envelope) -> None:
        mt = envelope.message_type

        if mt == "TaskRequest":
            p = task_pb2.TaskRequestPayload()
            p.ParseFromString(envelope.payload)
            self.tasks[p.task_id] = TaskRecord(
                task_id=p.task_id,
                title=p.title,
                instructions=p.instructions,
                requested_assignee=p.requested_assignee,
                requester=envelope.sender,
                status="requested",
                progress=0.0,
                assignee=None,
                deadline_unix_ms=p.deadline_unix_ms,
                input=p.input,
            )
            if self.active_assignment is not None and self.active_assignment[1] == p.task_id:
                self.active_assignment = None
            self._set_phase("Requested")
            return

        if mt == "TaskAccept":
            p = task_pb2.TaskAcceptPayload()
            p.ParseFromString(envelope.payload)
            if p.task_id in self.tasks:
                if self.active_assignment is None:
                    assignee = p.assignee or envelope.sender
                    self.active_assignment = (envelope.sender, p.task_id)
                    self.tasks[p.task_id].assignee = assignee
                    self.tasks[p.task_id].status = "accepted"
                    self._set_phase("InProgress")
                else:
                    # RFC-MACP-0009 §5 rule 3a: a second TaskAccept while the
                    # session's one assignee slot is already held is
                    # rejected -- this discard is caller misuse (issue #94),
                    # unlike an unknown task_id (see the `else` branch's
                    # absence below), which is not: a projection that joined
                    # mid-session may legitimately never see the TaskRequest.
                    held_sender, held_task_id = self.active_assignment
                    self._record_anomaly(
                        kind=ANOMALY_DUPLICATE_TASK_ACCEPT,
                        message_type=envelope.message_type,
                        message_id=envelope.message_id,
                        sender=envelope.sender,
                        subject_id=p.task_id,
                        detail=(
                            f"active assignee slot already held by {held_sender!r} for "
                            f"task {held_task_id!r}; discarded competing TaskAccept for "
                            f"task {p.task_id!r} from {envelope.sender!r}"
                        ),
                    )
            return

        if mt == "TaskReject":
            p = task_pb2.TaskRejectPayload()
            p.ParseFromString(envelope.payload)
            # The rejection record is kept unconditionally (mirrors updates/
            # completions/failures' own audit-trail convention); only the
            # per-task status write is gated on the task being known
            # (task.ts:136's `if (task)`). Slot-freeing is a SEPARATE,
            # unconditional check on sender alone (task.ts:158-161) — a
            # slot-holder's TaskReject naming an unknown task_id still frees
            # their held slot.
            self.rejections.append(
                TaskRejectRecord(
                    task_id=p.task_id,
                    assignee=p.assignee or envelope.sender,
                    reason=p.reason,
                )
            )
            if p.task_id in self.tasks:
                self.tasks[p.task_id].status = "rejected"
            slot = self.active_assignment
            if slot is not None and slot[0] == envelope.sender:
                held = self.tasks.get(slot[1])
                if held is not None:
                    held.assignee = None
                self.active_assignment = None
            return

        if mt == "TaskUpdate":
            p = task_pb2.TaskUpdatePayload()
            p.ParseFromString(envelope.payload)
            # The update record is kept unconditionally (task.ts pushes to its
            # update list outside its `if (task)` guard); only the per-task
            # status/progress view is gated on the task being known (#78).
            self.updates.append(
                TaskUpdateRecord(
                    task_id=p.task_id,
                    status=p.status,
                    progress=p.progress,
                    message=p.message,
                )
            )
            if p.task_id in self.tasks:
                self.tasks[p.task_id].status = "in_progress"
                self.tasks[p.task_id].progress = p.progress
            return

        if mt == "TaskComplete":
            p = task_pb2.TaskCompletePayload()
            p.ParseFromString(envelope.payload)
            self.completions.append(
                TaskCompleteRecord(
                    task_id=p.task_id,
                    assignee=p.assignee or envelope.sender,
                    summary=p.summary,
                    output=p.output,
                )
            )
            if p.task_id in self.tasks:
                self.tasks[p.task_id].status = "completed"
                self.tasks[p.task_id].progress = 1.0
                self._set_phase("Completed")
            return

        if mt == "TaskFail":
            p = task_pb2.TaskFailPayload()
            p.ParseFromString(envelope.payload)
            self.failures.append(
                TaskFailRecord(
                    task_id=p.task_id,
                    assignee=p.assignee or envelope.sender,
                    error_code=p.error_code,
                    reason=p.reason,
                    retryable=p.retryable,
                )
            )
            if p.task_id in self.tasks:
                self.tasks[p.task_id].status = "failed"
                self._set_phase("Failed")

    # -- State query helpers --

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return the task's current record for *task_id*, or None.

        Reflects live state, not just the original request: ``status``,
        ``progress``, and ``assignee`` update in place as later messages
        (TaskAccept/TaskUpdate/TaskComplete/TaskFail/TaskReject) arrive.
        """
        return self.tasks.get(task_id)

    def current_assignee(self, task_id: str) -> str | None:
        """Return the current assignee for *task_id*, or None if unassigned."""
        rec = self.tasks.get(task_id)
        return rec.assignee if rec is not None else None

    def current_status(self, task_id: str) -> str | None:
        """Return the current status for *task_id*, or None if unknown."""
        rec = self.tasks.get(task_id)
        return rec.status if rec is not None else None

    def is_accepted(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        return rec is not None and rec.status in ("accepted", "in_progress")

    def is_completed(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        return rec is not None and rec.status == "completed"

    def is_failed(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        return rec is not None and rec.status == "failed"

    def is_retryable(self, task_id: str) -> bool:
        """True if the task failed with ``retryable=True``."""
        return any(f.task_id == task_id and f.retryable for f in self.failures)

    def progress_of(self, task_id: str) -> float:
        """Return the latest progress value for *task_id*, or 0 if unknown."""
        rec = self.tasks.get(task_id)
        return rec.progress if rec is not None else 0.0

    def latest_progress(self) -> float | None:
        return self.updates[-1].progress if self.updates else None

    def active_tasks(self) -> list[TaskRecord]:
        """Return task records that are not in a terminal state."""
        active_statuses = {"requested", "accepted", "in_progress"}
        return [t for t in self.tasks.values() if t.status in active_statuses]


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


class TaskSession(BaseSession):
    """High-level helper for Task mode sessions."""

    MODE = MODE_TASK

    def _create_projection(self) -> BaseProjection:
        return TaskProjection()

    @property
    def task_projection(self) -> TaskProjection:
        assert isinstance(self.projection, TaskProjection)
        return self.projection

    def request_task(
        self,
        task_id: str,
        title: str,
        *,
        instructions: str = "",
        requested_assignee: str = "",
        input_data: bytes = b"",
        deadline_unix_ms: int = 0,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send a TaskRequest envelope. Canonical name across SDKs
        (parity with TypeScript ``requestTask``).
        """
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskRequestPayload(
            task_id=task_id,
            title=title,
            instructions=instructions,
            requested_assignee=requested_assignee,
            input=input_data,
            deadline_unix_ms=deadline_unix_ms,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskRequest",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def request(self, *args: object, **kwargs: object) -> envelope_pb2.Ack:
        """Deprecated alias for :meth:`request_task`."""
        warnings.warn(
            "TaskSession.request() is deprecated; use request_task() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.request_task(*args, **kwargs)  # type: ignore[arg-type]

    def accept_task(
        self,
        task_id: str,
        *,
        assignee: str = "",
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskAcceptPayload(
            task_id=task_id,
            assignee=assignee or self._sender_for(sender, auth=auth),
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskAccept",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def reject_task(
        self,
        task_id: str,
        *,
        assignee: str = "",
        reason: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskRejectPayload(
            task_id=task_id,
            assignee=assignee or self._sender_for(sender, auth=auth),
            reason=reason,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskReject",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def update_task(
        self,
        task_id: str,
        *,
        status: str = "",
        progress: float = 0.0,
        message: str = "",
        partial_output: bytes = b"",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send a TaskUpdate envelope. Canonical name across SDKs
        (parity with TypeScript ``updateTask``).
        """
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskUpdatePayload(
            task_id=task_id,
            status=status,
            progress=progress,
            message=message,
            partial_output=partial_output,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskUpdate",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def update(self, *args: object, **kwargs: object) -> envelope_pb2.Ack:
        """Deprecated alias for :meth:`update_task`."""
        warnings.warn(
            "TaskSession.update() is deprecated; use update_task() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.update_task(*args, **kwargs)  # type: ignore[arg-type]

    def complete_task(
        self,
        task_id: str,
        *,
        assignee: str = "",
        output: bytes = b"",
        summary: str = "",
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send a TaskComplete envelope. Canonical name across SDKs
        (parity with TypeScript ``completeTask``).
        """
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskCompletePayload(
            task_id=task_id,
            assignee=assignee or self._sender_for(sender, auth=auth),
            output=output,
            summary=summary,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskComplete",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def complete(self, *args: object, **kwargs: object) -> envelope_pb2.Ack:
        """Deprecated alias for :meth:`complete_task`."""
        warnings.warn(
            "TaskSession.complete() is deprecated; use complete_task() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.complete_task(*args, **kwargs)  # type: ignore[arg-type]

    def fail_task(
        self,
        task_id: str,
        *,
        assignee: str = "",
        error_code: str = "",
        reason: str = "",
        retryable: bool = False,
        sender: str | None = None,
        auth: AuthConfig | None = None,
    ) -> envelope_pb2.Ack:
        """Send a TaskFail envelope. Canonical name across SDKs
        (parity with TypeScript ``failTask``).
        """
        validate_required_field("task_id", task_id)
        payload = task_pb2.TaskFailPayload(
            task_id=task_id,
            assignee=assignee or self._sender_for(sender, auth=auth),
            error_code=error_code,
            reason=reason,
            retryable=retryable,
        )
        envelope = build_envelope(
            mode=self.MODE,
            message_type="TaskFail",
            session_id=self.session_id,
            sender=self._sender_for(sender, auth=auth),
            payload=serialize_message(payload),
        )
        return self._send_and_track(envelope, auth=auth)

    def fail(self, *args: object, **kwargs: object) -> envelope_pb2.Ack:
        """Deprecated alias for :meth:`fail_task`."""
        warnings.warn(
            "TaskSession.fail() is deprecated; use fail_task() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.fail_task(*args, **kwargs)  # type: ignore[arg-type]


# ── Deprecated aliases (issue #108) ───────────────────────────────────────
#
# ``TaskRequestRecord`` is the pre-rename name, kept as a module-level lazy
# alias (PEP 562) for one minor version, removed at this SDK's next major.
# Same mechanism and reasoning as ``proposal.py``'s own alias dict -- see the
# comment there for the full rationale. This module has no ``__path__`` (a
# plain file, not a package), so ``from macp_sdk.task import
# TaskRequestRecord`` resolves via a single ``getattr`` call -- the warning
# fires exactly once per such import.
_DEPRECATED_ALIASES = {
    "TaskRequestRecord": "TaskRecord",
}


def __getattr__(name: str) -> Any:
    new_name = _DEPRECATED_ALIASES.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {new_name} instead.", DeprecationWarning, stacklevel=2
    )
    return globals()[new_name]
