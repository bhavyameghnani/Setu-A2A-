"""Notification templates (spec section 12): pure functions, no I/O, no LLM.

Every sentence a person reads in their inbox is produced here from facts, so the same event
always reads the same way. An LLM-written summary may later be attached by a *background job*;
it never sits in this path.
"""

from dataclasses import dataclass
from enum import StrEnum

MAX_DETAIL = 200


class Event(StrEnum):
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELED = "task.canceled"
    TASK_REJECTED = "task.rejected"
    TASK_INPUT_REQUIRED = "task.input_required"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    POLICY_DENIED = "policy.denied"
    AGENT_REGISTERED = "agent.registered"
    AGENT_CARD_CHANGED = "agent.card_changed"


_BY_STATE = {
    "completed": Event.TASK_COMPLETED,
    "failed": Event.TASK_FAILED,
    "canceled": Event.TASK_CANCELED,
    "rejected": Event.TASK_REJECTED,
    "input_required": Event.TASK_INPUT_REQUIRED,
}


def event_for_state(state: str) -> Event | None:
    """The notification a task state change produces (None for states nobody needs told)."""
    return _BY_STATE.get(state)


@dataclass(frozen=True)
class Facts:
    """Everything a template may mention. Unused fields are simply ignored."""

    requester: str = ""  # display names, not ids
    assignee: str = ""
    actor: str = ""
    what: str = "a task"  # e.g. "risk.score"
    detail: str = ""  # error, reason, prompt or note
    has_result: bool = False
    department: str = ""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def _tail(sep: str, text: str) -> str:
    return f"{sep}{_clip(text)}" if text.strip() else ""


def summary(event: Event, f: Facts) -> str:
    match event:
        case Event.TASK_STARTED:
            return f"{f.assignee} started {f.what} for {f.requester}."
        case Event.TASK_COMPLETED:
            return f"{f.assignee} finished {f.what} for {f.requester}." + (
                " Result attached." if f.has_result else ""
            )
        case Event.TASK_FAILED:
            return f"{f.assignee} failed {f.what} for {f.requester}" + _tail(": ", f.detail) + "."
        case Event.TASK_CANCELED:
            return f"{f.what} for {f.requester} was canceled" + _tail(": ", f.detail) + "."
        case Event.TASK_REJECTED:
            return (
                f"{f.what} requested by {f.requester} from {f.assignee} was rejected"
                + _tail(": ", f.detail)
                + "."
            )
        case Event.TASK_INPUT_REQUIRED:
            return (
                f"{f.assignee} needs input on {f.what} for {f.requester}"
                + _tail(": ", f.detail)
                + "."
            )
        case Event.APPROVAL_REQUESTED:
            return f"Approval needed from {f.department}: {_clip(f.detail)}"
        case Event.APPROVAL_GRANTED:
            return f"{f.actor} approved: {_clip(f.detail)}"
        case Event.APPROVAL_DENIED:
            return f"{f.actor} denied: {_clip(f.detail)}"
        case Event.POLICY_DENIED:
            return (
                f"{f.requester} was not allowed to ask {f.assignee} for {f.what}"
                + _tail(": ", f.detail)
                + "."
            )
        case Event.AGENT_REGISTERED:
            return f"{f.assignee} was registered for {f.department}."
        case Event.AGENT_CARD_CHANGED:
            return f"The Agent Card of {f.assignee} changed" + _tail(": ", f.detail) + "."
