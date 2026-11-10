"""Creating notifications for the humans of every department involved in an event.

Everything here runs in the caller's transaction, so a notification exists exactly when the
event it reports was committed. The push to open WebSockets is a Postgres NOTIFY carrying ids
only (`<recipient>:<notification>`), delivered on commit.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import notifications as tmpl
from app.core.notifications import Event, Facts
from app.db.messages_util import message_skills
from app.db.models import (
    Approval,
    Department,
    Human,
    Message,
    Notification,
    Participant,
    Task,
    TaskArtifact,
)

NOTIFY_CHANNEL = "setu_notifications"


@dataclass(frozen=True)
class Audience:
    """Humans of one department; `role` narrows it to people who hold that role."""

    department_id: uuid.UUID
    role: str | None = None


async def _humans(
    session: AsyncSession, audience: Audience, exclude: Iterable[uuid.UUID]
) -> list[Participant]:
    rows = await session.execute(
        select(Participant, Human.roles)
        .join(Human, Human.participant_id == Participant.id)
        .where(
            Participant.department_id == audience.department_id,
            Participant.kind == "human",
            Participant.status == "active",
        )
        .order_by(Participant.id)
    )
    skip = set(exclude)
    out: list[Participant] = []
    for p, roles in rows.all():
        if p.id in skip:
            continue
        if audience.role in (None, "dept_member") or audience.role in roles:
            out.append(p)
    return out


async def emit(
    session: AsyncSession,
    event: Event,
    facts: Facts,
    audiences: Sequence[Audience],
    *,
    room_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
    exclude: Iterable[uuid.UUID] = (),
) -> list[Notification]:
    """One notification per human in the audiences (a person is notified once per event, even
    if two audiences name their department). The caller commits."""
    summary = tmpl.summary(event, facts)
    payload = {"event": event.value, **{k: _plain(v) for k, v in (detail or {}).items()}}
    seen: set[uuid.UUID] = set()
    made: list[Notification] = []
    for aud in audiences:
        for person in await _humans(session, aud, exclude):
            if person.id in seen:
                continue
            seen.add(person.id)
            made.append(
                Notification(
                    recipient_id=person.id,
                    department_id=person.department_id,
                    room_id=room_id,
                    task_id=task_id,
                    trace_id=trace_id,
                    event_type=event.value,
                    summary=summary,
                    detail=payload,
                )
            )
    session.add_all(made)
    await session.flush()
    for n in made:
        await session.execute(
            text("select pg_notify(:ch, :payload)"),
            {"ch": NOTIFY_CHANNEL, "payload": f"{n.recipient_id}:{n.id}"},
        )
    return made


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


# ------------------------------------------------------------------ task & approval events


async def _names(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Participant]:
    rows = await session.execute(select(Participant).where(Participant.id.in_(set(ids))))
    return {p.id: p for p in rows.scalars()}


async def _what(session: AsyncSession, task: Task) -> str:
    request = (
        await session.get(Message, task.request_message_id) if task.request_message_id else None
    )
    if request is None:
        return "a task"
    skills = message_skills(request.body)
    if skills:
        return ", ".join(skills)
    snippet = request.body.get("text")
    return f"“{snippet[:60]}”" if isinstance(snippet, str) and snippet else "a task"


async def notify_task(
    session: AsyncSession,
    task: Task,
    event: Event,
    detail: str = "",
    *,
    approval: Approval | None = None,
) -> list[Notification]:
    """A task event, for the requester's and the assignee's departments."""
    people = await _names(session, [task.requester_id, task.assignee_id])
    requester, assignee = people[task.requester_id], people[task.assignee_id]
    has_result = (
        event == Event.TASK_COMPLETED
        and (
            await session.execute(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.task_id == task.id)
            )
        ).scalar_one()
        > 0
    )
    facts = Facts(
        requester=requester.display_name,
        assignee=assignee.display_name,
        what=await _what(session, task),
        detail=detail,
        has_result=has_result,
    )
    audiences = [Audience(requester.department_id), Audience(assignee.department_id)]
    # A task that needed sign-off also tells the department that gave (or refused) it.
    gate = (
        await session.execute(
            select(Approval.approver_department_id)
            .where(Approval.task_id == task.id, Approval.kind == "policy")
            .limit(1)
        )
    ).scalar_one_or_none()
    if gate is not None:
        audiences.append(Audience(gate))
    return await emit(
        session,
        event,
        facts,
        audiences,
        room_id=task.room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        detail={"state": task.state, **({"approval_id": approval.id} if approval else {})},
    )


async def notify_approval(
    session: AsyncSession,
    appr: Approval,
    task: Task,
    event: Event,
    *,
    actor: Participant | None = None,
) -> list[Notification]:
    """Approval requested / granted / denied. A request reaches the approver department's
    role holders (who can act on it) plus the requester's and assignee's departments; a
    decision reaches the same departments except the person who decided."""
    people = await _names(session, [task.requester_id, task.assignee_id])
    requester, assignee = people[task.requester_id], people[task.assignee_id]
    dept = await session.get(Department, appr.approver_department_id)
    facts = Facts(
        requester=requester.display_name,
        assignee=assignee.display_name,
        actor=actor.display_name if actor else "",
        department=dept.name if dept else "",
        detail=appr.summary,
    )
    approver = Audience(appr.approver_department_id, appr.approver_role)
    audiences = [approver, Audience(requester.department_id), Audience(assignee.department_id)]
    if event != Event.APPROVAL_REQUESTED:
        audiences[0] = Audience(appr.approver_department_id)
    return await emit(
        session,
        event,
        facts,
        audiences,
        room_id=task.room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        detail={"approval_id": appr.id, "approval_kind": appr.kind},
        exclude=[actor.id] if actor else (),
    )


async def notify_policy_denied(
    session: AsyncSession,
    sender: Participant,
    recipient: Participant,
    *,
    room_id: uuid.UUID,
    skills: Sequence[str],
    reason: str,
    trace_id: str | None,
) -> list[Notification]:
    facts = Facts(
        requester=sender.display_name,
        assignee=recipient.display_name,
        what=", ".join(skills) or "a task",
        detail=reason,
    )
    return await emit(
        session,
        Event.POLICY_DENIED,
        facts,
        [Audience(sender.department_id), Audience(recipient.department_id)],
        room_id=room_id,
        trace_id=trace_id,
        detail={"sender_id": sender.id, "recipient_id": recipient.id},
    )


async def notify_agent(
    session: AsyncSession, agent: Participant, event: Event, detail: str = ""
) -> list[Notification]:
    """An agent was registered, or its card changed: tell the owning department."""
    dept = await session.get(Department, agent.department_id)
    facts = Facts(assignee=agent.display_name, department=dept.name if dept else "", detail=detail)
    return await emit(
        session,
        event,
        facts,
        [Audience(agent.department_id)],
        detail={"agent_id": agent.id},
    )
