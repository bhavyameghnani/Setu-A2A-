import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.a2a import registry
from app.a2a.client import A2AError
from app.a2a.mapping import status_body
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.api.deps import Session, WritePrincipal
from app.core import secrets
from app.core.auth import Principal
from app.core.lifecycle import InvalidTransitionError, TaskState, release_gated
from app.core.notifications import Event
from app.db import audit, notifications, spans
from app.db import messages as repo
from app.db import tasks as task_repo
from app.db.models import Agent, Approval, Participant, Room, Task
from app.schemas import ApprovalDecision, ApprovalOut, MessageCreate

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


def _holds_role(p: Principal, department_id: uuid.UUID, role: str) -> bool:
    if not p.is_human:
        return False
    if p.is_org_admin:
        return True
    if p.department_id != department_id:
        return False
    if role == "dept_member":
        return bool({"dept_member", "dept_admin"} & p.roles)
    return role in p.roles


def _may_decide(p: Principal, a: Approval, task: Task) -> bool:
    if not p.is_human:
        return False
    if a.kind == "agent":  # the person who asked, or an admin of their department
        return p.participant_id == a.approver_id or p.can_administer(a.approver_department_id)
    if p.participant_id == task.requester_id:  # nobody approves their own request
        return False
    return _holds_role(p, a.approver_department_id, a.approver_role)


def _may_see(p: Principal, a: Approval, task: Task) -> bool:
    if not p.is_human:
        return False
    return (
        _may_decide(p, a, task)
        or "auditor" in p.roles
        or p.participant_id in (task.requester_id, a.approver_id)
    )


async def _out(session: Session, rows: Sequence[Approval]) -> list[ApprovalOut]:
    tasks = {
        t.id: t
        for t in (
            await session.execute(select(Task).where(Task.id.in_({a.task_id for a in rows})))
        ).scalars()
    }
    out = []
    for a in rows:
        o = ApprovalOut.model_validate(a)
        o.room_id = tasks[a.task_id].room_id
        out.append(o)
    return out


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    principal: WritePrincipal,
    session: Session,
    state: Annotated[str, Query(pattern="^(pending|approved|denied|all)$")] = "pending",
) -> list[ApprovalOut]:
    """Approvals this person may decide (or, for the requester and auditors, may follow)."""
    q = (
        select(Approval, Task)
        .join(Task, Task.id == Approval.task_id)
        .join(Room, Room.id == Task.room_id)
        .where(Room.org_id == principal.org_id)
        .order_by(Approval.created_at.desc())
        .limit(200)
    )
    if state != "all":
        q = q.where(Approval.state == state)
    rows = [a for a, t in (await session.execute(q)).all() if _may_see(principal, a, t)]
    return await _out(session, rows)


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(
    approval_id: uuid.UUID, principal: WritePrincipal, session: Session
) -> ApprovalOut:
    a, _ = await _load(session, principal, approval_id, decide=False)
    return (await _out(session, [a]))[0]


async def _load(
    session: Session, principal: Principal, approval_id: uuid.UUID, *, decide: bool
) -> tuple[Approval, Task]:
    a = await session.get(Approval, approval_id, with_for_update=decide)
    task = await session.get(Task, a.task_id) if a else None
    room = await session.get(Room, task.room_id) if task else None
    if a is None or task is None or room is None or room.org_id != principal.org_id:
        raise HTTPException(404, "approval not found")
    if not _may_see(principal, a, task):
        raise HTTPException(404, "approval not found")
    if decide and not _may_decide(principal, a, task):
        raise HTTPException(403, "you are not allowed to decide this approval")
    return a, task


@router.post("/{approval_id}/decide", response_model=ApprovalOut)
async def decide(
    approval_id: uuid.UUID,
    body: ApprovalDecision,
    principal: WritePrincipal,
    session: Session,
    request: Request,
) -> ApprovalOut:
    """Approve or deny. A policy approval releases (or rejects) the held task; an answer to
    an agent's question is sent back to the agent on the same A2A task."""
    a, _ = await _load(session, principal, approval_id, decide=True)
    task = await session.get(Task, a.task_id, with_for_update=True, populate_existing=True)
    assert task is not None
    if a.state != "pending":
        raise HTTPException(409, f"already {a.state}")
    if TaskState(task.state) != TaskState.INPUT_REQUIRED:
        raise HTTPException(409, f"the task is {task.state}, not waiting for a decision")

    if a.kind == "agent" and not body.approve:
        await _cancel_at_agent(request, session, task)
    a.state = "approved" if body.approve else "denied"
    a.decided_by = principal.participant_id
    a.decided_at = datetime.now(UTC)
    a.note = body.note
    if a.kind == "policy" and body.approve:
        task.state = release_gated(TaskState(task.state)).value  # back to the dispatcher's queue
        task.updated_at = a.decided_at
    elif a.kind == "agent" and body.approve:
        task_repo.apply_state(task, TaskState.WORKING)
    else:
        target = TaskState.REJECTED if a.kind == "policy" else TaskState.CANCELED
        task_repo.apply_state(task, target, error=None if a.kind == "agent" else "denied")
    await spans.end(
        session,
        a.span_id,
        "approved" if body.approve else "denied",
        {"decided_by": principal.participant_id, "note": body.note},
    )
    await audit.append(
        session,
        principal.org_id,
        "approval.granted" if body.approve else "approval.denied",
        principal.participant_id,
        {"approval_id": a.id, "task_id": task.id, "kind": a.kind, "note": body.note},
    )
    me = await session.get(Participant, principal.participant_id)
    await notifications.notify_approval(
        session,
        a,
        task,
        Event.APPROVAL_GRANTED if body.approve else Event.APPROVAL_DENIED,
        actor=me,
    )
    await repo.post_message(  # commits the decision together with its message
        session,
        task.room_id,
        principal.participant_id,
        MessageCreate(
            kind="approval_response",
            body={
                "approval_id": str(a.id),
                "task_id": str(task.id),
                "approved": body.approve,
                "note": body.note,
            },
            mentions=[task.requester_id],
        ),
        trace_id=task.trace_id,
    )
    if not body.approve:
        verb = "rejected" if a.kind == "policy" else "canceled"
        await repo.post_message(
            session,
            task.room_id,
            principal.participant_id,
            MessageCreate(
                kind="task_update",
                body=status_body(
                    task.id,
                    verb,
                    f"Denied by an approver.{' ' + body.note if body.note else ''}",
                    origin="hub",
                ),
                mentions=[task.assignee_id],
                task_id=task.id,
            ),
            trace_id=task.trace_id,
        )
    request.app.state.dispatcher.wake()
    await session.refresh(a)
    return (await _out(session, [a]))[0]


async def _cancel_at_agent(request: Request, session: Session, task: Task) -> None:
    """Best effort: tell the agent we gave up. A refusal does not block the denial."""
    agent = await session.get(Agent, task.assignee_id)
    if agent is None or not task.a2a_task_id:
        return
    settings = request.app.state.settings
    try:
        ep = registry.endpoint_for(agent, settings.resolved_encryption_key())
        await validate_agent_url(ep.rpc_url, allow_private=settings.allow_private_agent_hosts())
        await request.app.state.a2a_client.cancel_task(ep, task.a2a_task_id)
    except (A2AError, UnsafeUrlError, secrets.DecryptionError, InvalidTransitionError):
        pass
