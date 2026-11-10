import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Request

from app.a2a import cancel
from app.api.deps import ReadPrincipal, Session, WritePrincipal
from app.db import tasks as task_repo
from app.db.access import room_access
from app.db.models import Task
from app.schemas import ArtifactOut, TaskOut

router = APIRouter(prefix="/v1", tags=["tasks"])


async def _out(session: Session, tasks: Sequence[Task]) -> list[TaskOut]:
    arts = await task_repo.artifacts_for(session, [t.id for t in tasks])
    out: list[TaskOut] = []
    for t in tasks:
        o = TaskOut.model_validate(t)
        o.artifacts = [ArtifactOut.model_validate(a) for a in arts[t.id]]
        out.append(o)
    return out


@router.get("/rooms/{room_id}/tasks", response_model=list[TaskOut])
async def list_tasks(
    room_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> list[TaskOut]:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.can_read:
        raise HTTPException(404, "room not found")
    return await _out(session, await task_repo.list_room_tasks(session, room_id))


async def _readable_task(session: Session, principal: ReadPrincipal, task_id: uuid.UUID) -> Task:
    task = await session.get(Task, task_id)
    acc = await room_access(session, principal, task.room_id) if task else None
    if task is None or acc is None or not acc.can_read:
        raise HTTPException(404, "task not found")
    return task


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, principal: ReadPrincipal, session: Session) -> TaskOut:
    return (await _out(session, [await _readable_task(session, principal, task_id)]))[0]


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: uuid.UUID, principal: WritePrincipal, session: Session, request: Request
) -> TaskOut:
    """Ask the agent to stop (A2A CancelTask), then mark the task canceled. Allowed for the
    requester and for room managers."""
    task = await _readable_task(session, principal, task_id)
    try:
        await cancel.cancel_task(request.app.state, session, principal, task)
    except cancel.CancelError as e:
        raise HTTPException(e.status, str(e)) from None
    return (await _out(session, [task]))[0]
