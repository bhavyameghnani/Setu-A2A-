import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminPrincipal, CurrentPrincipal, Session
from app.core import auth
from app.core.auth import Principal
from app.db.models import Agent, ApiKey, Department, Human, Participant
from app.schemas import (
    AgentCreate,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    HumanCreate,
    ParticipantOut,
)

router = APIRouter(prefix="/v1", tags=["participants"])

# Roles a department admin may hand out, and only inside their own department.
_DEPT_ADMIN_GRANTABLE = {"dept_member", "dept_admin"}


async def _department_for_admin(
    session: Session, principal: Principal, department_id: uuid.UUID
) -> Department:
    dept = await session.get(Department, department_id)
    if dept is None or dept.org_id != principal.org_id:
        raise HTTPException(404, "department not found")
    if not principal.can_administer(dept.id):
        raise HTTPException(403, "not an admin of that department")
    return dept


@router.post("/humans", response_model=ParticipantOut, status_code=201)
async def create_human(
    body: HumanCreate, principal: AdminPrincipal, session: Session
) -> Participant:
    dept = await _department_for_admin(session, principal, body.department_id)
    roles = set(body.roles) or {"dept_member"}
    if not principal.is_org_admin and not roles <= _DEPT_ADMIN_GRANTABLE:
        raise HTTPException(403, "only an org_admin can grant org_admin or auditor")
    p = Participant(
        org_id=dept.org_id,
        department_id=dept.id,
        kind="human",
        display_name=body.display_name,
        handle=body.handle,
    )
    session.add(p)
    try:
        await session.flush()
        session.add(
            Human(
                participant_id=p.id,
                email=body.email.strip().lower(),
                password_hash=auth.hash_secret(body.password),
                roles=sorted(roles),
            )
        )
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "handle or email already exists") from None
    await session.refresh(p)
    return p


@router.post("/participants", response_model=ParticipantOut, status_code=201)
async def create_agent(
    body: AgentCreate, principal: AdminPrincipal, session: Session
) -> Participant:
    dept = await _department_for_admin(session, principal, body.department_id)
    p = Participant(
        org_id=dept.org_id,
        department_id=dept.id,
        kind="agent",
        display_name=body.display_name,
        handle=body.handle,
    )
    session.add(p)
    try:
        await session.flush()
        session.add(Agent(participant_id=p.id, owner_department_id=dept.id, transport="sdk"))
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "handle already exists in org") from None
    await session.refresh(p)
    return p


@router.get("/participants", response_model=list[ParticipantOut])
async def list_participants(
    principal: CurrentPrincipal, session: Session, department_id: uuid.UUID | None = None
) -> Sequence[Participant]:
    q = select(Participant).where(Participant.org_id == principal.org_id)
    if department_id is not None:
        q = q.where(Participant.department_id == department_id)
    return (await session.execute(q.order_by(Participant.handle))).scalars().all()


async def _target_for_admin(session: Session, principal: Principal, pid: uuid.UUID) -> Participant:
    target = await session.get(Participant, pid)
    if target is None or target.org_id != principal.org_id:
        raise HTTPException(404, "participant not found")
    if not principal.can_administer(target.department_id):
        raise HTTPException(403, "not an admin of that participant's department")
    return target


@router.post(
    "/participants/{participant_id}/api-keys", response_model=ApiKeyCreated, status_code=201
)
async def create_api_key(
    participant_id: uuid.UUID, body: ApiKeyCreate, principal: AdminPrincipal, session: Session
) -> ApiKeyCreated:
    target = await _target_for_admin(session, principal, participant_id)
    new = auth.generate_api_key()
    row = ApiKey(
        participant_id=target.id,
        key_prefix=new.prefix,
        key_hash=new.key_hash,
        scopes=sorted(set(body.scopes)),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), key=new.key)


@router.get("/participants/{participant_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    participant_id: uuid.UUID, principal: AdminPrincipal, session: Session
) -> Sequence[ApiKey]:
    target = await _target_for_admin(session, principal, participant_id)
    rows = await session.execute(select(ApiKey).where(ApiKey.participant_id == target.id))
    return rows.scalars().all()


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: uuid.UUID, principal: AdminPrincipal, session: Session) -> None:
    from datetime import UTC, datetime

    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "api key not found")
    await _target_for_admin(session, principal, key.participant_id)
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.commit()
