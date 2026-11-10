"""Append to and verify the hash-chained audit log (spec section 10).

`append` runs inside the caller's transaction, so an event and the change it describes commit
together. A per-org advisory lock keeps `seq` and `prev_hash` consistent under concurrency.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit as chain
from app.db.models import AuditLog


async def append(
    session: AsyncSession,
    org_id: uuid.UUID,
    event_type: str,
    actor_id: uuid.UUID | None,
    payload: Mapping[str, Any],
) -> AuditLog:
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{org_id}"}
    )
    last = (
        await session.execute(
            select(AuditLog.seq, AuditLog.hash)
            .where(AuditLog.org_id == org_id)
            .order_by(AuditLog.seq.desc())
            .limit(1)
        )
    ).first()
    seq, prev = (last[0] + 1, last[1]) if last else (1, chain.GENESIS_HASH)
    created = datetime.now(UTC)
    clean = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in payload.items()}
    row = AuditLog(
        org_id=org_id,
        seq=seq,
        event_type=event_type,
        actor_id=actor_id,
        payload=clean,
        prev_hash=prev,
        hash=chain.compute_hash(prev, org_id, seq, event_type, actor_id, clean, created),
        created_at=created,
    )
    session.add(row)
    await session.flush()
    return row


async def list_events(
    session: AsyncSession, org_id: uuid.UUID, after_seq: int = 0, limit: int = 100
) -> Sequence[AuditLog]:
    rows = await session.execute(
        select(AuditLog)
        .where(AuditLog.org_id == org_id, AuditLog.seq > after_seq)
        .order_by(AuditLog.seq)
        .limit(limit)
    )
    return rows.scalars().all()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_broken_seq: int | None


async def verify(session: AsyncSession, org_id: uuid.UUID) -> VerifyResult:
    rows = (
        await session.execute(
            select(AuditLog).where(AuditLog.org_id == org_id).order_by(AuditLog.seq)
        )
    ).scalars()
    records = [
        chain.AuditRecord(
            r.seq, r.org_id, r.event_type, r.actor_id, r.payload, r.created_at, r.prev_hash, r.hash
        )
        for r in rows
    ]
    broken = chain.verify_chain(records)
    return VerifyResult(broken is None, len(records), broken)
