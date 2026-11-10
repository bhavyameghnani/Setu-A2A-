"""Who may see, post to, and manage a room. One place, used by REST and WebSocket alike."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.db.models import Room, RoomDepartmentAccess, RoomMember


@dataclass(frozen=True)
class RoomAccess:
    room: Room
    can_read: bool
    can_post: bool
    can_manage: bool

    @property
    def visible(self) -> bool:
        # Managers see the room's existence even without read access (to fix access mistakes).
        return self.can_read or self.can_manage


async def room_access(
    session: AsyncSession, principal: Principal, room_id: uuid.UUID
) -> RoomAccess | None:
    """None means "does not exist for you": callers answer 404, never 403, to avoid leaking."""
    room = await session.get(Room, room_id)
    if room is None or room.org_id != principal.org_id:
        return None
    member = await session.get(RoomMember, (room_id, principal.participant_id))
    dept_access: str | None = None
    if principal.is_human:  # department access applies to humans only; agents must be members
        dept_access = (
            await session.execute(
                select(RoomDepartmentAccess.access).where(
                    RoomDepartmentAccess.room_id == room_id,
                    RoomDepartmentAccess.department_id == principal.department_id,
                )
            )
        ).scalar_one_or_none()
    can_read = member is not None or dept_access is not None
    if member is not None:
        can_post = member.role != "observer"
    else:
        can_post = dept_access == "participate"
    can_manage = (member is not None and member.role == "owner") or principal.can_administer(
        room.owning_department_id
    )
    return RoomAccess(room, can_read, can_post, can_manage)


async def visible_rooms(
    session: AsyncSession, principal: Principal, limit: int = 200
) -> Sequence[Room]:
    is_member = exists().where(
        RoomMember.room_id == Room.id, RoomMember.participant_id == principal.participant_id
    )
    conds = [is_member]
    if principal.is_human:
        conds.append(
            exists().where(
                RoomDepartmentAccess.room_id == Room.id,
                RoomDepartmentAccess.department_id == principal.department_id,
            )
        )
    rows = await session.execute(
        select(Room)
        .where(Room.org_id == principal.org_id, or_(*conds))
        .order_by(Room.created_at.desc())
        .limit(limit)
    )
    return rows.scalars().all()
