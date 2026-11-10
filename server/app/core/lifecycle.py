"""Status machines.

Delivery status: what an *addressed* participant has done with a message.

    pending -> acked -> completed | failed        (pending may also jump straight to a terminal)

Re-sending the current status is a no-op, so retries are safe (delivery is at-least-once).
Task state: the A2A task states, mirrored in `tasks.state` (see `next_task_state`).
"""

from enum import StrEnum


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    ACKED = "acked"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_DELIVERY = frozenset({DeliveryStatus.COMPLETED, DeliveryStatus.FAILED})

_ALLOWED: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset(
        {DeliveryStatus.ACKED, DeliveryStatus.COMPLETED, DeliveryStatus.FAILED}
    ),
    DeliveryStatus.ACKED: frozenset({DeliveryStatus.COMPLETED, DeliveryStatus.FAILED}),
    DeliveryStatus.COMPLETED: frozenset(),
    DeliveryStatus.FAILED: frozenset(),
}


class InvalidTransitionError(Exception):
    pass


def next_delivery_status(current: DeliveryStatus, target: DeliveryStatus) -> DeliveryStatus:
    if target == current:
        return current
    if target not in _ALLOWED[current]:
        raise InvalidTransitionError(f"cannot go from {current.value} to {target.value}")
    return target


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL_TASK_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
)
INTERRUPTED_TASK_STATES = frozenset({TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED})

_ACTIVE = frozenset(
    {
        TaskState.WORKING,
        TaskState.INPUT_REQUIRED,
        TaskState.AUTH_REQUIRED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.REJECTED,
    }
)
_TASK_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: _ACTIVE,
    TaskState.WORKING: _ACTIVE - {TaskState.WORKING},
    TaskState.INPUT_REQUIRED: _ACTIVE - {TaskState.INPUT_REQUIRED},
    TaskState.AUTH_REQUIRED: _ACTIVE - {TaskState.AUTH_REQUIRED},
    **{s: frozenset[TaskState]() for s in TERMINAL_TASK_STATES},
}


def release_gated(current: TaskState) -> TaskState:
    """A task held by a `require_approval` policy is released by a human approval.

    The one way back to `submitted`, reserved for the hub: the general machine never allows it.
    """
    if current != TaskState.INPUT_REQUIRED:
        raise InvalidTransitionError(f"only an input_required task can be released, not {current}")
    return TaskState.SUBMITTED


def next_task_state(current: TaskState, target: TaskState) -> TaskState:
    """Validate a task state change. Same state is a no-op; terminal states are final, and
    nothing goes back to `submitted`."""
    if target == current:
        return current
    if target not in _TASK_ALLOWED[current]:
        raise InvalidTransitionError(f"task cannot go from {current.value} to {target.value}")
    return target
