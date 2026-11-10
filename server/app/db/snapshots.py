"""Room snapshots: a background job writes a digest of each run of new messages.

Never in the message path. The default summarizer is deterministic (core.replay); an LLM
summarizer could be swapped in here later without touching routing.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.replay import SnapshotItem, summarize_segment
from app.db.models import Message, Participant, Room, RoomSnapshot

log = structlog.get_logger()
Summarizer = Callable[[Sequence[SnapshotItem]], str]


async def snapshot_room(
    session: AsyncSession, room: Room, min_messages: int, summarize: Summarizer = summarize_segment
) -> RoomSnapshot | None:
    """Snapshot messages after the previous snapshot, if there are at least `min_messages`."""
    prev = (
        await session.execute(
            select(func.coalesce(func.max(RoomSnapshot.up_to_seq), 0)).where(
                RoomSnapshot.room_id == room.id
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(Message.seq, Message.kind, Participant.handle)
            .join(Participant, Participant.id == Message.sender_id)
            .where(Message.room_id == room.id, Message.seq > prev)
            .order_by(Message.seq)
        )
    ).all()
    if len(rows) < max(min_messages, 1):
        return None
    items = [SnapshotItem(seq, kind, handle) for seq, kind, handle in rows]
    up_to = items[-1].seq
    # (room_id, up_to_seq) is the key, so two workers racing produce one row, not two.
    await session.execute(
        pg_insert(RoomSnapshot)
        .values(room_id=room.id, up_to_seq=up_to, summary_text=summarize(items))
        .on_conflict_do_nothing()
    )
    await session.commit()
    return await session.get(RoomSnapshot, (room.id, up_to))


async def run_snapshots_once(
    sessionmaker: async_sessionmaker[AsyncSession], min_messages: int
) -> int:
    created = 0
    async with sessionmaker() as session:
        room_ids: list[uuid.UUID] = list(
            (await session.execute(select(Room.id).where(Room.last_seq >= min_messages))).scalars()
        )
    for room_id in room_ids:
        async with sessionmaker() as session:
            room = await session.get(Room, room_id)
            if room is not None and await snapshot_room(session, room, min_messages):
                created += 1
    return created


async def snapshot_loop(
    sessionmaker: async_sessionmaker[AsyncSession], interval: float, min_messages: int
) -> None:
    while True:
        await asyncio.sleep(interval)  # sleep first: nothing to do at boot
        try:
            n = await run_snapshots_once(sessionmaker, min_messages)
            if n:
                log.info("snapshots_created", count=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad run must not kill the loop
            log.warning("snapshot_run_failed", error=str(exc))


async def stop_task(task: asyncio.Task[None] | None) -> None:
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
