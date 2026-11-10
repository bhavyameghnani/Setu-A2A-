"""Recording spans (spec section 11). Spans are written in the caller's transaction, so a span
exists exactly when the thing it describes was committed."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tracing import new_span_id
from app.db.models import Span


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json(v) for v in value]
    return value


async def start(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    trace_id: str,
    kind: str,
    name: str,
    parent_span_id: str | None = None,
    span_id: str | None = None,
    room_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    status: str = "ok",
    ended: bool = False,
    attributes: Mapping[str, Any] | None = None,
) -> Span:
    """Open a span (`ended=True` records an instantaneous one such as a policy decision)."""
    now = datetime.now(UTC)
    span = Span(
        id=span_id or new_span_id(),
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        org_id=org_id,
        room_id=room_id,
        kind=kind,
        name=name,
        actor_id=actor_id,
        task_id=task_id,
        started_at=now,
        ended_at=now if ended else None,
        status=status,
        attributes=_json(dict(attributes or {})),
    )
    session.add(span)
    await session.flush()
    return span


async def end(
    session: AsyncSession,
    span_id: str | None,
    status: str,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    """Close a span. Closing twice or an unknown id is a no-op (cancel and failure can race)."""
    if not span_id:
        return
    span = await session.get(Span, span_id, with_for_update=True)
    if span is None or span.ended_at is not None:
        return
    span.ended_at = datetime.now(UTC)
    span.status = status
    millis = round((span.ended_at - span.started_at).total_seconds() * 1000)
    span.attributes = {**span.attributes, "duration_ms": millis, **_json(dict(attributes or {}))}
    await session.flush()


async def for_trace(session: AsyncSession, trace_id: str) -> list[Span]:
    rows = await session.execute(
        select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at, Span.id)
    )
    return list(rows.scalars())
