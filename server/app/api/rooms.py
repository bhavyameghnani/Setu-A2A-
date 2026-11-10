import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.deps import ReadPrincipal, Session, WritePrincipal
from app.db import audit
from app.db.access import RoomAccess, room_access, visible_rooms
from app.db.models import Department, Participant, Room, RoomDepartmentAccess, RoomMember
from app.schemas import AccessGrant, AccessOut, MemberAdd, MemberOut, RoomCreate, RoomOut

router = APIRouter(prefix="/v1", tags=["rooms"])


async def _visible_room(
    session: Session, principal: ReadPrincipal, room_id: uuid.UUID
) -> RoomAccess:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.visible:
        raise HTTPException(404, "room not found")
    return acc


async def _managed_room(
    session: Session, principal: WritePrincipal, room_id: uuid.UUID
) -> RoomAccess:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.visible:
        raise HTTPException(404, "room not found")
    if not acc.can_manage:
        raise HTTPException(403, "room owner or department admin required")
    return acc


@router.post("/rooms", response_model=RoomOut, status_code=201)
async def create_room(body: RoomCreate, principal: WritePrincipal, session: Session) -> Room:
    dept_id = body.owning_department_id or principal.department_id
    dept = await session.get(Department, dept_id)
    if dept is None or dept.org_id != principal.org_id:
        raise HTTPException(404, "department not found")
    if not principal.can_administer(dept.id):
        # Plain members may only create rooms owned by their own department.
        if dept.id != principal.department_id:
            raise HTTPException(403, "cannot create rooms for another department")
    room = Room(
        org_id=dept.org_id,
        name=body.name,
        topic=body.topic,
        visibility=body.visibility,
        owning_department_id=dept.id,
        created_by=principal.participant_id,
    )
    session.add(room)
    await session.flush()
    session.add(RoomMember(room_id=room.id, participant_id=principal.participant_id, role="owner"))
    if body.visibility in ("department", "cross_department"):
        session.add(
            RoomDepartmentAccess(room_id=room.id, department_id=dept.id, access="participate")
        )
    await session.commit()
    await session.refresh(room)
    return room


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(principal: ReadPrincipal, session: Session) -> Sequence[Room]:
    return await visible_rooms(session, principal)


@router.get("/rooms/{room_id}", response_model=RoomOut)
async def get_room(room_id: uuid.UUID, principal: ReadPrincipal, session: Session) -> Room:
    return (await _visible_room(session, principal, room_id)).room


@router.get("/rooms/{room_id}/members", response_model=list[MemberOut])
async def list_members(
    room_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> Sequence[RoomMember]:
    acc = await _visible_room(session, principal, room_id)
    if not acc.can_read:
        raise HTTPException(403, "no read access to this room")
    rows = await session.execute(select(RoomMember).where(RoomMember.room_id == room_id))
    return rows.scalars().all()


@router.post("/rooms/{room_id}/members", response_model=MemberOut, status_code=201)
async def add_member(
    room_id: uuid.UUID, body: MemberAdd, principal: WritePrincipal, session: Session
) -> RoomMember:
    await _managed_room(session, principal, room_id)
    participant = await session.get(Participant, body.participant_id)
    if participant is None or participant.org_id != principal.org_id:  # cross-org denied
        raise HTTPException(404, "participant not found")
    member = RoomMember(room_id=room_id, participant_id=participant.id, role=body.role)
    session.add(member)
    try:
        await session.flush()
        await audit.append(
            session,
            principal.org_id,
            "room.member_added",
            principal.participant_id,
            {
                "room_id": room_id,
                "participant_id": participant.id,
                "role": body.role,
                "self_added": participant.id == principal.participant_id,
            },
        )
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "already a member") from None
    await session.refresh(member)
    return member


@router.put("/rooms/{room_id}/access", response_model=AccessOut)
async def grant_department_access(
    room_id: uuid.UUID, body: AccessGrant, principal: WritePrincipal, session: Session
) -> RoomDepartmentAccess:
    acc = await _managed_room(session, principal, room_id)
    room = acc.room
    dept = await session.get(Department, body.department_id)
    if dept is None or dept.org_id != room.org_id:
        raise HTTPException(404, "department not found")
    if room.visibility == "private":
        raise HTTPException(422, "private rooms are member-only; change visibility first")
    if room.visibility == "department" and dept.id != room.owning_department_id:
        raise HTTPException(422, "department rooms are limited to the owning department")
    stmt = (
        pg_insert(RoomDepartmentAccess)
        .values(room_id=room_id, department_id=dept.id, access=body.access)
        .on_conflict_do_update(
            index_elements=["room_id", "department_id"], set_={"access": body.access}
        )
    )
    await session.execute(stmt)
    await audit.append(
        session,
        principal.org_id,
        "room.access_granted",
        principal.participant_id,
        {"room_id": room_id, "department_id": dept.id, "access": body.access},
    )
    await session.commit()
    row = await session.get(RoomDepartmentAccess, (room_id, dept.id), populate_existing=True)
    assert row is not None
    return row


@router.delete("/rooms/{room_id}/access/{department_id}", status_code=204)
async def revoke_department_access(
    room_id: uuid.UUID, department_id: uuid.UUID, principal: WritePrincipal, session: Session
) -> None:
    await _managed_room(session, principal, room_id)
    row = await session.get(RoomDepartmentAccess, (room_id, department_id))
    if row is None:
        raise HTTPException(404, "no such access grant")
    await session.delete(row)
    await audit.append(
        session,
        principal.org_id,
        "room.access_revoked",
        principal.participant_id,
        {"room_id": room_id, "department_id": department_id},
    )
    await session.commit()
