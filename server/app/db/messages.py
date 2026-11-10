"""Message persistence. Postgres is the source of truth; NOTIFY only announces (room, seq).

Authorization is the caller's job (see app.db.access); this module only stores and reads.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.routing import Member, RoutingInput, addressed_participants
from app.core.routing import Subscription as RoutedSubscription
from app.core.tracing import TraceContext, new_span_id, new_trace_id
from app.db import spans
from app.db.control import TraceRef, approval_request_body, authorize_send
from app.db.messages_util import message_skills
from app.db.models import (
    Approval,
    Message,
    MessageStatus,
    Participant,
    Room,
    RoomMember,
    Subscription,
    Task,
)
from app.db.tasks import create_tasks_for_message
from app.schemas import MessageCreate

NOTIFY_CHANNEL = "setu_messages"


class InvalidReferenceError(Exception):
    pass


async def post_message(
    session: AsyncSession,
    room_id: uuid.UUID,
    sender_id: uuid.UUID,
    data: MessageCreate,
    *,
    trace_id: str | None = None,
    incoming: TraceContext | None = None,
) -> Message:
    """Store a message, decide who it addresses, and (for `task` messages) create task rows.

    `data.task_id` references an existing task in this room: the message joins that task's
    thread, and the task's assignee is addressed. For a `task` message it names the parent task.

    `incoming` is a caller's W3C trace context (from an external A2A caller): the message joins
    that trace as a child of the caller's span. A `task` message otherwise starts a new trace,
    or continues its parent task's.
    """
    if data.reply_to is not None:
        parent = await session.execute(
            select(Message.id).where(Message.id == data.reply_to, Message.room_id == room_id)
        )
        if parent.scalar_one_or_none() is None:
            raise InvalidReferenceError("reply_to must be a message in the same room")

    referenced: Task | None = None
    if data.task_id is not None:
        referenced = await session.get(Task, data.task_id)
        if referenced is None or referenced.room_id != room_id:
            raise InvalidReferenceError("task_id must be a task in the same room")

    members = [
        Member(r.participant_id, r.role)
        for r in (
            await session.execute(select(RoomMember).where(RoomMember.room_id == room_id))
        ).scalars()
    ]
    subs = [
        RoutedSubscription(r.participant_id, r.kind, r.skill)
        for r in (
            await session.execute(select(Subscription).where(Subscription.room_id == room_id))
        ).scalars()
    ]
    # Deterministic and decided once, at send time, so later changes never rewrite history.
    addressed = addressed_participants(
        RoutingInput(
            sender_id=sender_id,
            kind=data.kind,
            mentions=data.mentions,
            skills=message_skills(data.body),
            assignee_id=referenced.assignee_id if referenced else None,
        ),
        members,
        subs,
    )

    # Tracing: task messages are the root (or a child) of a trace.
    trace = None
    if data.kind == "task" or incoming is not None:
        parent_span: str | None = None
        if incoming is not None:
            trace_id, parent_span = incoming.trace_id, incoming.span_id
        elif referenced is not None:
            trace_id = trace_id or referenced.trace_id
            parent_span = referenced.span_id
        trace = TraceRef(trace_id or new_trace_id(), new_span_id(), parent_span)
    # Policy and delegation (spec 10): raises ControlDenied before anything is stored.
    check = await authorize_send(
        session,
        room_id,
        sender_id,
        data.kind,
        data.body,
        addressed,
        data.delegation_id,
        trace or TraceRef(trace_id or new_trace_id(), new_span_id()),
    )

    # The UPDATE takes the room row lock until commit, so concurrent senders get consecutive
    # seq values and commit in seq order (a reader never sees seq N+1 before seq N).
    seq = (
        await session.execute(
            update(Room)
            .where(Room.id == room_id)
            .values(last_seq=Room.last_seq + 1)
            .returning(Room.last_seq)
        )
    ).scalar_one()
    msg = Message(
        room_id=room_id,
        seq=seq,
        sender_id=sender_id,
        kind=data.kind,
        body=data.body,
        mentions=data.mentions,
        reply_to=data.reply_to,
        addressed=list(addressed),
        task_id=referenced.id if referenced else None,
        trace_id=trace.trace_id
        if trace
        else trace_id or (referenced.trace_id if referenced else None),
        span_id=trace.span_id if trace else None,
        delegation_chain_id=check.delegation_id
        or (referenced.delegation_chain_id if referenced else None),
    )
    session.add(msg)
    await session.flush()
    session.add_all(
        MessageStatus(message_id=msg.id, participant_id=pid, room_id=room_id, status="pending")
        for pid in addressed
    )
    if trace is not None:
        sender = await session.get(Participant, sender_id)
        assert sender is not None
        await spans.start(
            session,
            org_id=sender.org_id,
            trace_id=trace.trace_id,
            span_id=trace.span_id,
            parent_span_id=trace.parent_span_id,
            kind="message",
            name=f"{data.kind} message",
            room_id=room_id,
            actor_id=sender_id,
            ended=True,
            attributes={
                "kind": data.kind,
                "seq": seq,
                "skills": message_skills(data.body),
                "addressed": list(addressed),
                "sender_department": sender.department_id,
                "delegation_id": check.delegation_id,
            },
        )
    gated: list[tuple[Task, Approval]] = []
    if data.kind == "task":
        gated = await create_tasks_for_message(session, msg, addressed, referenced, check.gates)
    # Delivered to listeners only on commit. Ids only: payload limit is ~8 KB.
    await session.execute(
        text("select pg_notify(:ch, :payload)"),
        {"ch": NOTIFY_CHANNEL, "payload": f"{room_id}:{seq}"},
    )
    await session.commit()
    await session.refresh(msg)
    for task, appr in gated:  # visible in the room; the approval itself is already stored
        await post_message(
            session,
            room_id,
            sender_id,
            MessageCreate(kind="approval_request", body=approval_request_body(appr, task.id)),
            trace_id=task.trace_id,
        )
    return msg


async def list_messages(
    session: AsyncSession, room_id: uuid.UUID, after_seq: int = 0, limit: int = 100
) -> Sequence[Message]:
    rows = await session.execute(
        select(Message)
        .where(Message.room_id == room_id, Message.seq > after_seq)
        .order_by(Message.seq)
        .limit(limit)
    )
    return rows.scalars().all()
