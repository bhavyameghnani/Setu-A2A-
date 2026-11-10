"""Delivery state for the calling participant: cursor, message status, inbox, subscriptions,
and room snapshots. Everything here is about *me* in a room I can read."""

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import ReadPrincipal, SendPrincipal, Session
from app.core.auth import Principal
from app.core.lifecycle import DeliveryStatus, InvalidTransitionError
from app.db import delivery
from app.db.access import RoomAccess, room_access
from app.db.models import Message, MessageStatus, RoomMember, RoomSnapshot, Subscription
from app.schemas import (
    CursorOut,
    CursorSet,
    MessageOut,
    SnapshotOut,
    StatusOut,
    StatusSet,
    SubscriptionCreate,
    SubscriptionOut,
)

router = APIRouter(prefix="/v1/rooms/{room_id}", tags=["delivery"])


async def _readable(session: Session, principal: Principal, room_id: uuid.UUID) -> RoomAccess:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.can_read:
        raise HTTPException(404, "room not found")
    return acc


@router.get("/cursor", response_model=CursorOut)
async def get_cursor(room_id: uuid.UUID, principal: ReadPrincipal, session: Session) -> CursorOut:
    await _readable(session, principal, room_id)
    return CursorOut(
        room_id=room_id,
        last_seq=await delivery.get_cursor(session, room_id, principal.participant_id),
    )


@router.put("/cursor", response_model=CursorOut)
async def put_cursor(
    room_id: uuid.UUID, body: CursorSet, principal: ReadPrincipal, session: Session
) -> CursorOut:
    await _readable(session, principal, room_id)
    try:
        seq = await delivery.set_cursor(session, room_id, principal.participant_id, body.last_seq)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return CursorOut(room_id=room_id, last_seq=seq)


@router.get("/inbox", response_model=list[MessageOut])
async def get_inbox(
    room_id: uuid.UUID,
    principal: ReadPrincipal,
    session: Session,
    limit: int = Query(100, ge=1, le=500),
) -> Sequence[Message]:
    """What I was asked to do and have not finished: the catch-up list after a restart."""
    await _readable(session, principal, room_id)
    return await delivery.inbox(session, room_id, principal.participant_id, limit)


@router.put("/messages/{message_id}/status", response_model=StatusOut)
async def put_status(
    room_id: uuid.UUID,
    message_id: uuid.UUID,
    body: StatusSet,
    principal: SendPrincipal,
    session: Session,
) -> MessageStatus:
    await _readable(session, principal, room_id)
    try:
        return await delivery.set_status(
            session, room_id, message_id, principal.participant_id, DeliveryStatus(body.status)
        )
    except delivery.NoSuchStatusError as e:
        raise HTTPException(404, str(e)) from None
    except InvalidTransitionError as e:
        raise HTTPException(409, str(e)) from None


@router.put("/subscriptions", response_model=SubscriptionOut)
async def subscribe(
    room_id: uuid.UUID, body: SubscriptionCreate, principal: SendPrincipal, session: Session
) -> SubscriptionOut:
    await _readable(session, principal, room_id)
    member = await session.get(RoomMember, (room_id, principal.participant_id))
    if member is None or member.role == "observer":
        raise HTTPException(403, "only non-observer members can subscribe")
    sub = await delivery.add_subscription(
        session, room_id, principal.participant_id, body.kind or "", body.skill or ""
    )
    return _sub_out(sub)


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def my_subscriptions(
    room_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> list[SubscriptionOut]:
    await _readable(session, principal, room_id)
    subs = await delivery.list_subscriptions(session, room_id, principal.participant_id)
    return [_sub_out(s) for s in subs]


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def unsubscribe(
    room_id: uuid.UUID, subscription_id: uuid.UUID, principal: SendPrincipal, session: Session
) -> None:
    await _readable(session, principal, room_id)
    sub = await session.get(Subscription, subscription_id)
    if sub is None or sub.room_id != room_id or sub.participant_id != principal.participant_id:
        raise HTTPException(404, "subscription not found")
    await session.delete(sub)
    await session.commit()


@router.get("/snapshots", response_model=list[SnapshotOut])
async def snapshots(
    room_id: uuid.UUID,
    principal: ReadPrincipal,
    session: Session,
    limit: int = Query(10, ge=1, le=100),
) -> Sequence[RoomSnapshot]:
    """Newest first. A late joiner reads the latest digest, then replays after its up_to_seq."""
    await _readable(session, principal, room_id)
    rows = await session.execute(
        select(RoomSnapshot)
        .where(RoomSnapshot.room_id == room_id)
        .order_by(RoomSnapshot.up_to_seq.desc())
        .limit(limit)
    )
    return rows.scalars().all()


def _sub_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id, room_id=sub.room_id, kind=sub.kind or None, skill=sub.skill or None
    )
