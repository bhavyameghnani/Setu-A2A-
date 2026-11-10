"""Cancel a task: ask the agent to stop (A2A CancelTask), then mark it canceled.

Shared by `POST /v1/tasks/{id}/cancel` and the A2A facade's `CancelTask`, so both follow the
same rules: the requester or a room manager may cancel; a refusal from the agent leaves the
task alone.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a import registry
from app.a2a.client import A2AError
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.core import secrets
from app.core.auth import Principal
from app.core.lifecycle import TERMINAL_TASK_STATES, TaskState
from app.core.notifications import Event
from app.db import messages as repo
from app.db import notifications, spans
from app.db import tasks as task_repo
from app.db.access import room_access
from app.db.models import Agent, Task
from app.schemas import MessageCreate


class CancelError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def cancel_task(state: Any, session: AsyncSession, principal: Principal, task: Task) -> None:
    """`state` is `app.state` (settings, a2a_client, dispatcher). Commits on success."""
    acc = await room_access(session, principal, task.room_id)
    if acc is None:
        raise CancelError(404, "task not found")
    if principal.participant_id != task.requester_id and not acc.can_manage:
        raise CancelError(403, "only the requester or a room manager can cancel a task")
    if TaskState(task.state) in TERMINAL_TASK_STATES:
        raise CancelError(409, f"task is already {task.state}")

    settings = state.settings
    agent = await session.get(Agent, task.assignee_id)
    if agent and task.a2a_task_id:  # the agent has seen it: tell it to stop
        try:
            ep = registry.endpoint_for(agent, settings.resolved_encryption_key())
            await validate_agent_url(ep.rpc_url, allow_private=settings.allow_private_agent_hosts())
            await state.a2a_client.cancel_task(ep, task.a2a_task_id)
        except (A2AError, UnsafeUrlError, secrets.DecryptionError) as e:
            raise CancelError(502, f"agent did not accept the cancel: {e}") from None
    state.dispatcher.cancel_running(task.id)  # stop mirroring its stream
    await session.refresh(task, with_for_update=True)
    if not task_repo.apply_state(task, TaskState.CANCELED):
        raise CancelError(409, f"task is already {task.state}")
    await spans.end(session, task.span_id, "canceled", {"canceled_by": principal.participant_id})
    await notifications.notify_task(session, task, Event.TASK_CANCELED, "canceled by a user")
    await repo.post_message(
        session,
        task.room_id,
        principal.participant_id,
        MessageCreate(
            kind="task_update",
            body={
                "task_id": str(task.id),
                "state": "canceled",
                "origin": "hub",
                "text": "Canceled by a user.",
            },
            mentions=[task.assignee_id],
            task_id=task.id,
        ),
        trace_id=task.trace_id,
    )
    await session.refresh(task)
