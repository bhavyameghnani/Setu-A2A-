"""Task rows: creation (inside the message transaction), reads, and state changes."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lifecycle import (
    TERMINAL_TASK_STATES,
    InvalidTransitionError,
    TaskState,
    next_task_state,
)
from app.core.policy import Decision
from app.core.tracing import new_trace_id
from app.db.control import open_policy_approval
from app.db.models import Agent, Approval, Message, Task, TaskArtifact


async def create_tasks_for_message(
    session: AsyncSession,
    msg: Message,
    addressed: Sequence[uuid.UUID],
    parent: Task | None,
    gates: Mapping[uuid.UUID, Decision] | None = None,
) -> list[tuple[Task, Approval]]:
    """A `task` message creates one A2A task per addressed, enabled `a2a_remote` agent.

    Runs in the same transaction as the message, so a committed task message always has its
    task rows; the dispatcher picks them up afterwards. Tasks whose policy says
    `require_approval` are held (`input_required`) with a pending approval; those are returned
    so the caller can announce them in the room.
    """
    if not addressed:
        return []
    rows = await session.execute(
        select(Agent.participant_id).where(
            Agent.participant_id.in_(addressed),
            Agent.transport == "a2a_remote",
            Agent.enabled.is_(True),
        )
    )
    assignees = sorted(rows.scalars().all(), key=str)
    trace_id = msg.trace_id or (parent.trace_id if parent else None) or new_trace_id()
    tasks = [
        Task(
            room_id=msg.room_id,
            request_message_id=msg.id,
            requester_id=msg.sender_id,
            assignee_id=assignee,
            parent_task_id=parent.id if parent else None,
            state=TaskState.SUBMITTED.value,
            trace_id=trace_id,
            delegation_chain_id=msg.delegation_chain_id,
        )
        for assignee in assignees
    ]
    session.add_all(tasks)
    await session.flush()
    if len(tasks) == 1:
        msg.task_id = tasks[0].id  # the request message belongs to its task's thread
    if tasks and not msg.trace_id:
        msg.trace_id = trace_id
    held: list[tuple[Task, Approval]] = []
    for t in tasks:
        if gates and t.assignee_id in gates:
            held.append((t, await open_policy_approval(session, t, gates[t.assignee_id], msg.body)))
    return held


def apply_state(task: Task, target: TaskState, *, error: str | None = None) -> bool:
    """Move a task along the state machine. Returns False (and changes nothing) if the change
    is illegal, e.g. an agent reporting `working` after `completed`."""
    try:
        new = next_task_state(TaskState(task.state), target)
    except InvalidTransitionError:
        return False
    if new.value == task.state:
        return True
    task.state = new.value
    task.updated_at = datetime.now(UTC)
    if new in TERMINAL_TASK_STATES:
        task.completed_at = task.updated_at
    if error:
        task.error = error
    return True


async def list_room_tasks(
    session: AsyncSession, room_id: uuid.UUID, limit: int = 100
) -> Sequence[Task]:
    rows = await session.execute(
        select(Task).where(Task.room_id == room_id).order_by(Task.created_at.desc()).limit(limit)
    )
    return rows.scalars().all()


async def artifacts_for(
    session: AsyncSession, task_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[TaskArtifact]]:
    out: dict[uuid.UUID, list[TaskArtifact]] = {t: [] for t in task_ids}
    if not task_ids:
        return out
    rows = await session.execute(
        select(TaskArtifact)
        .where(TaskArtifact.task_id.in_(task_ids))
        .order_by(TaskArtifact.created_at)
    )
    for a in rows.scalars():
        out[a.task_id].append(a)
    return out
