"""Per-participant delivery state: cursors, message status, subscriptions, inbox."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lifecycle import DeliveryStatus, next_delivery_status
from app.core.replay import advance_cursor
from app.db.models import Cursor, Message, MessageStatus, Room, Subscription


async def get_cursor(session: AsyncSession, room_id: uuid.UUID, participant_id: uuid.UUID) -> int:
    row = await session.get(Cursor, (room_id, participant_id))
    return row.last_seq if row else 0


async def set_cursor(
    session: AsyncSession, room_id: uuid.UUID, participant_id: uuid.UUID, proposed: int
) -> int:
    """Advance (never rewind) the cursor. Raises ValueError if beyond the room's last seq."""
    room = await session.get(Room, room_id)
    assert room is not None
    # Upsert first so the row exists, then lock it: concurrent acks serialize on it.
    await session.execute(
        pg_insert(Cursor)
        .values(room_id=room_id, participant_id=participant_id, last_seq=0)
        .on_conflict_do_nothing()
    )
    row = (
        await session.execute(
            select(Cursor)
            .where(Cursor.room_id == room_id, Cursor.participant_id == participant_id)
            .with_for_update()
        )
    ).scalar_one()
    row.last_seq = advance_cursor(row.last_seq, proposed, room.last_seq)
    await session.commit()
    return row.last_seq


class NoSuchStatusError(Exception):
    """The message never asked this participant to act."""


async def set_status(
    session: AsyncSession,
    room_id: uuid.UUID,
    message_id: uuid.UUID,
    participant_id: uuid.UUID,
    target: DeliveryStatus,
) -> MessageStatus:
    row = (
        await session.execute(
            select(MessageStatus)
            .where(
                MessageStatus.message_id == message_id,
                MessageStatus.participant_id == participant_id,
                MessageStatus.room_id == room_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NoSuchStatusError("this message did not address you")
    row.status = next_delivery_status(DeliveryStatus(row.status), target).value
    await session.commit()
    await session.refresh(row)
    return row


async def inbox(
    session: AsyncSession, room_id: uuid.UUID, participant_id: uuid.UUID, limit: int = 100
) -> Sequence[Message]:
    """Messages that addressed this participant and are not finished, oldest first."""
    rows = await session.execute(
        select(Message)
        .join(MessageStatus, MessageStatus.message_id == Message.id)
        .where(
            Message.room_id == room_id,
            MessageStatus.participant_id == participant_id,
            MessageStatus.status.in_(("pending", "acked")),
        )
        .order_by(Message.seq)
        .limit(limit)
    )
    return rows.scalars().all()


async def add_subscription(
    session: AsyncSession, room_id: uuid.UUID, participant_id: uuid.UUID, kind: str, skill: str
) -> Subscription:
    await session.execute(
        pg_insert(Subscription)
        .values(
            id=uuid.uuid4(), room_id=room_id, participant_id=participant_id, kind=kind, skill=skill
        )
        .on_conflict_do_nothing()
    )
    await session.commit()
    return (
        await session.execute(
            select(Subscription).where(
                Subscription.room_id == room_id,
                Subscription.participant_id == participant_id,
                Subscription.kind == kind,
                Subscription.skill == skill,
            )
        )
    ).scalar_one()


async def list_subscriptions(
    session: AsyncSession, room_id: uuid.UUID, participant_id: uuid.UUID
) -> Sequence[Subscription]:
    rows = await session.execute(
        select(Subscription).where(
            Subscription.room_id == room_id, Subscription.participant_id == participant_id
        )
    )
    return rows.scalars().all()
