"""Agent registry: register by Agent Card URL, discover by skill, refresh, tune."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from app.a2a import registry
from app.a2a.client import auth_headers
from app.api.deps import AdminPrincipal, CurrentPrincipal, Session
from app.core import secrets
from app.core.auth import Principal
from app.db.models import Agent, Department, Message, Participant, RoomMember, Task
from app.schemas import (
    AgentAdminOut,
    AgentOut,
    AgentPreviewOut,
    AgentPreviewRequest,
    AgentRegister,
    AgentStatsOut,
    AgentUpdate,
    SkillOut,
)

router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _public(p: Participant, a: Agent) -> AgentOut:
    return AgentOut(
        participant_id=p.id,
        handle=p.handle,
        display_name=p.display_name,
        department_id=a.owner_department_id,
        transport=a.transport,
        skills=[SkillOut(**s) for s in a.skills],
        protocol_versions=list(a.a2a_protocol_versions),
        a2a_version=a.a2a_version,
        streaming=a.streaming,
        enabled=a.enabled,
        card_fetched_at=a.card_fetched_at,
    )


def _admin(
    p: Participant, a: Agent, warnings: list[str] | None = None, changed: bool | None = None
) -> AgentAdminOut:
    return AgentAdminOut(
        **_public(p, a).model_dump(),
        card_url=a.a2a_card_url,
        rpc_url=a.a2a_rpc_url,
        timeout_seconds=a.timeout_seconds,
        max_retries=a.max_retries,
        has_credentials=a.auth_config is not None,
        warnings=warnings or [],
        changed=changed,
    )


def _view(principal: Principal, p: Participant, a: Agent) -> AgentOut:
    return _admin(p, a) if principal.can_administer(a.owner_department_id) else _public(p, a)


async def _load(
    session: Session, principal: Principal, pid: uuid.UUID
) -> tuple[Participant, Agent]:
    p = await session.get(Participant, pid)
    a = await session.get(Agent, pid)
    if p is None or a is None or p.org_id != principal.org_id:
        raise HTTPException(404, "agent not found")
    return p, a


def _require_admin(principal: Principal, agent: Agent) -> None:
    if not principal.can_administer(agent.owner_department_id):
        raise HTTPException(403, "not an admin of the agent's department")


@router.post("/register", response_model=AgentAdminOut, status_code=201)
async def register(
    body: AgentRegister, principal: AdminPrincipal, session: Session, request: Request
) -> AgentAdminOut:
    dept = await session.get(Department, body.department_id)
    if dept is None or dept.org_id != principal.org_id:
        raise HTTPException(404, "department not found")
    if not principal.can_administer(dept.id):
        raise HTTPException(403, "not an admin of that department")
    settings = request.app.state.settings
    try:
        reg = await registry.register_agent(
            session,
            request.app.state.a2a_client,
            org_id=principal.org_id,
            department=dept,
            card_url=body.card_url,
            auth=body.auth.to_config() if body.auth else None,
            handle=body.handle,
            display_name=body.display_name,
            timeout_seconds=body.timeout_seconds,
            max_retries=body.max_retries,
            allow_private=settings.allow_private_agent_hosts(),
            encryption_key=settings.resolved_encryption_key(),
        )
    except registry.RegistrationError as e:
        raise HTTPException(e.status_code, str(e)) from None
    return _admin(reg.participant, reg.agent, reg.warnings)


@router.get("", response_model=list[AgentAdminOut | AgentOut])
async def list_agents(
    principal: CurrentPrincipal,
    session: Session,
    skill: str | None = None,
    q: str | None = None,
    department_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[AgentOut]:
    """Discovery. `skill` matches a skill id or tag exactly; `q` matches names, ids and tags."""
    stmt = (
        select(Participant, Agent)
        .join(Agent, Agent.participant_id == Participant.id)
        .where(Participant.org_id == principal.org_id)
        .order_by(Participant.handle)
        .limit(limit)
    )
    if department_id is not None:
        stmt = stmt.where(Agent.owner_department_id == department_id)
    out: list[AgentOut] = []
    for p, a in (await session.execute(stmt)).all():
        skills = a.skills
        if skill and not any(skill == s["id"] or skill in s.get("tags", []) for s in skills):
            continue
        if q:
            hay = " ".join(
                [
                    p.handle,
                    p.display_name,
                    *[f"{s['id']} {s['name']} {' '.join(s.get('tags', []))}" for s in skills],
                ]
            )
            if q.lower() not in hay.lower():
                continue
        out.append(_view(principal, p, a))
    return out


@router.post("/preview", response_model=AgentPreviewOut)
async def preview(
    body: AgentPreviewRequest, principal: AdminPrincipal, request: Request
) -> AgentPreviewOut:
    """ "Test connection": fetch and validate a card exactly as registering would, but save
    nothing. Same URL guard, same errors."""
    settings = request.app.state.settings
    auth = body.auth.to_config() if body.auth else None
    try:
        card, info = await registry.fetch_card(
            request.app.state.a2a_client,
            body.card_url,
            auth_headers(auth),
            allow_private=settings.allow_private_agent_hosts(),
        )
    except registry.RegistrationError as e:
        raise HTTPException(e.status_code, str(e)) from None
    warnings: list[str] = []
    if info.security and not auth:
        names = ", ".join(s.name for s in info.security)
        warnings.append(
            f"the card declares security schemes ({names}) but no credentials were given"
        )
    return AgentPreviewOut(
        name=info.name,
        description=info.description,
        a2a_version=info.version.value,
        protocol_versions=list(info.protocol_versions),
        streaming=info.streaming,
        skills=[SkillOut(**s) for s in registry.skills_json(info)],
        security_schemes=[s.name for s in info.security],
        rpc_url=info.rpc_url,
        suggested_handle=registry.slugify(info.name),
        warnings=warnings,
    )


@router.get("/stats", response_model=list[AgentStatsOut])
async def stats(principal: CurrentPrincipal, session: Session) -> list[AgentStatsOut]:
    """Activity counts per agent. Shown to org admins, auditors and the admins of the agent's
    own department; for everyone else the list is empty (counts are not for other teams)."""
    rows = (
        await session.execute(
            select(Agent.participant_id, Agent.owner_department_id)
            .join(Participant, Participant.id == Agent.participant_id)
            .where(Participant.org_id == principal.org_id)
        )
    ).all()
    oversight = principal.is_human and bool({"org_admin", "auditor"} & principal.roles)
    visible = [pid for pid, dept in rows if oversight or principal.can_administer(dept)]
    if not visible:
        return []
    msgs = {
        sender: n
        for sender, n in (
            await session.execute(
                select(Message.sender_id, func.count())
                .where(Message.sender_id.in_(visible))
                .group_by(Message.sender_id)
            )
        ).all()
    }
    tasks = {
        assignee: (total, failed)
        for assignee, total, failed in (
            await session.execute(
                select(Task.assignee_id, func.count(), func.count().filter(Task.state == "failed"))
                .where(Task.assignee_id.in_(visible))
                .group_by(Task.assignee_id)
            )
        ).all()
    }
    rooms = {
        member: n
        for member, n in (
            await session.execute(
                select(RoomMember.participant_id, func.count())
                .where(RoomMember.participant_id.in_(visible))
                .group_by(RoomMember.participant_id)
            )
        ).all()
    }
    return [
        AgentStatsOut(
            participant_id=pid,
            messages=msgs.get(pid, 0),
            tasks=tasks.get(pid, (0, 0))[0],
            failed=tasks.get(pid, (0, 0))[1],
            rooms=rooms.get(pid, 0),
        )
        for pid in visible
    ]


@router.get("/{participant_id}", response_model=AgentAdminOut | AgentOut)
async def get_agent(
    participant_id: uuid.UUID, principal: CurrentPrincipal, session: Session
) -> AgentOut:
    p, a = await _load(session, principal, participant_id)
    return _view(principal, p, a)


@router.post("/{participant_id}/refresh", response_model=AgentAdminOut)
async def refresh(
    participant_id: uuid.UUID, principal: AdminPrincipal, session: Session, request: Request
) -> AgentAdminOut:
    p, a = await _load(session, principal, participant_id)
    _require_admin(principal, a)
    settings = request.app.state.settings
    try:
        changed = await registry.refresh_agent(
            session,
            request.app.state.a2a_client,
            a,
            allow_private=settings.allow_private_agent_hosts(),
            encryption_key=settings.resolved_encryption_key(),
        )
    except registry.RegistrationError as e:
        raise HTTPException(e.status_code, str(e)) from None
    return _admin(p, a, changed=changed)


@router.patch("/{participant_id}", response_model=AgentAdminOut)
async def update_agent(
    participant_id: uuid.UUID,
    body: AgentUpdate,
    principal: AdminPrincipal,
    session: Session,
    request: Request,
) -> AgentAdminOut:
    p, a = await _load(session, principal, participant_id)
    _require_admin(principal, a)
    if body.enabled is not None:
        a.enabled = body.enabled
    if body.timeout_seconds is not None:
        a.timeout_seconds = body.timeout_seconds
    if body.max_retries is not None:
        a.max_retries = body.max_retries
    if body.clear_auth:
        a.auth_config = None
    if body.auth is not None:
        key = request.app.state.settings.resolved_encryption_key()
        a.auth_config = secrets.encrypt_json(key, body.auth.to_config())
    await session.commit()
    await session.refresh(a)
    return _admin(p, a)
