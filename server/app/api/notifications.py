"""The inbox (spec section 12): list with unread count, mark read, and act on a notification.

Everything is scoped to the recipient. Seeing a notification does not grant access to its
room: visibility rules stay as they are (decision 35), and `actions` says what the person can
actually do. A pending approval can be decided from the inbox even without room access.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select, update

from app.api.approvals import _may_decide
from app.api.deps import ReadPrincipal, SendPrincipal, Session, WritePrincipal
from app.core.auth import Principal
from app.core.lifecycle import TERMINAL_TASK_STATES, TaskState
from app.db import messages as repo
from app.db.access import room_access
from app.db.control import ControlDenied
from app.db.models import Approval, Notification, Task
from app.schemas import (
    MessageCreate,
    MessageOut,
    NotificationActions,
    NotificationList,
    NotificationOut,
    NotificationReply,
)

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


def _humans_only(principal: Principal) -> None:
    if not principal.is_human:
        raise HTTPException(403, "notifications are for people")


async def unread_count(session: Session, recipient: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_id == recipient, Notification.read_at.is_(None))
        )
    ).scalar_one()


async def actions_for(
    session: Session, principal: Principal, n: Notification
) -> NotificationActions:
    out = NotificationActions()
    if n.room_id is not None:
        acc = await room_access(session, principal, n.room_id)
        out.open_room = acc is not None and acc.can_read
        out.reply = acc is not None and acc.can_post
    task = await session.get(Task, n.task_id) if n.task_id else None
    if task is not None:
        acc = await room_access(session, principal, task.room_id)
        live = TaskState(task.state) not in TERMINAL_TASK_STATES
        out.cancel_task = live and (
            principal.participant_id == task.requester_id or bool(acc and acc.can_manage)
        )
        raw = n.detail.get("approval_id")
        if raw:
            appr = await session.get(Approval, uuid.UUID(str(raw)))
            if appr and appr.state == "pending" and _may_decide(principal, appr, task):
                out.approval_id = appr.id
    return out


async def _view(session: Session, principal: Principal, n: Notification) -> NotificationOut:
    out = NotificationOut.model_validate(n)
    out.actions = await actions_for(session, principal, n)
    return out


@router.get("", response_model=NotificationList)
async def inbox(
    principal: ReadPrincipal,
    session: Session,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: uuid.UUID | None = None,
) -> NotificationList:
    """Newest first. `unread` counts everything unread, not just this page."""
    _humans_only(principal)
    q = select(Notification).where(Notification.recipient_id == principal.participant_id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    if before is not None:
        q = q.where(Notification.id < before)
    rows = list(
        (await session.execute(q.order_by(Notification.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return NotificationList(
        unread=await unread_count(session, principal.participant_id),
        items=[await _view(session, principal, n) for n in rows],
        next_before=rows[-1].id if more and rows else None,
    )


@router.get("/unread-count")
async def unread(principal: ReadPrincipal, session: Session) -> dict[str, int]:
    _humans_only(principal)
    return {"unread": await unread_count(session, principal.participant_id)}


async def _mine(session: Session, principal: Principal, nid: uuid.UUID) -> Notification:
    n = await session.get(Notification, nid)
    if n is None or n.recipient_id != principal.participant_id:
        raise HTTPException(404, "notification not found")
    return n


@router.post("/read-all")
async def read_all(principal: WritePrincipal, session: Session) -> dict[str, int]:
    _humans_only(principal)
    res = await session.execute(
        update(Notification)
        .where(
            Notification.recipient_id == principal.participant_id, Notification.read_at.is_(None)
        )
        .values(read_at=func.now())
    )
    await session.commit()
    return {"marked": res.rowcount or 0}  # type: ignore[attr-defined]


@router.get("/{notification_id}", response_model=NotificationOut)
async def get_notification(
    notification_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> NotificationOut:
    _humans_only(principal)
    return await _view(session, principal, await _mine(session, principal, notification_id))


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, principal: WritePrincipal, session: Session
) -> NotificationOut:
    _humans_only(principal)
    n = await _mine(session, principal, notification_id)
    if n.read_at is None:
        n.read_at = func.now()
        await session.commit()
        await session.refresh(n)
    return await _view(session, principal, n)


@router.post("/{notification_id}/reply", response_model=MessageOut, status_code=201)
async def reply(
    notification_id: uuid.UUID,
    body: NotificationReply,
    principal: SendPrincipal,
    session: Session,
    request: Request,
) -> object:
    """Answer from the inbox: posts a human message in the notification's room. If the
    notification belongs to a task, the message joins that task's thread, so its agent is
    addressed (routing, decision 60) and can act on it."""
    _humans_only(principal)
    n = await _mine(session, principal, notification_id)
    if n.room_id is None:
        raise HTTPException(409, "this notification has no room to reply in")
    acc = await room_access(session, principal, n.room_id)
    if acc is None or not acc.can_read:
        raise HTTPException(404, "room not found")  # access can change after the notification
    if not acc.can_post:
        raise HTTPException(403, "you can read this room but not post to it")
    try:
        msg = await repo.post_message(
            session,
            n.room_id,
            principal.participant_id,
            MessageCreate(kind="text", body={"text": body.text}, task_id=n.task_id),
        )
    except repo.InvalidReferenceError as e:
        raise HTTPException(422, str(e)) from None
    except ControlDenied as e:
        raise HTTPException(403, e.reason) from None
    if n.read_at is None:
        n.read_at = func.now()
        await session.commit()
    return msg
