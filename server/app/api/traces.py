import re
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import exists, or_, select

from app.api.deps import ReadPrincipal, Session
from app.db import spans as repo
from app.db.models import Room, RoomDepartmentAccess, RoomMember, Span
from app.schemas import SpanOut, TraceOut

router = APIRouter(prefix="/v1", tags=["traces"])

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


@router.get("/traces/{trace_id}", response_model=TraceOut)
async def get_trace(trace_id: str, principal: ReadPrincipal, session: Session) -> TraceOut:
    """The trace as a tree: who asked whom, the policy decision, the delegation, durations.

    A person sees only the spans of rooms their department (or membership) can read; an
    auditor sees the whole trace. A trace with nothing visible is a 404, never a 403.
    """
    if not _TRACE_ID.match(trace_id):
        raise HTTPException(404, "trace not found")
    rows = [s for s in await repo.for_trace(session, trace_id) if s.org_id == principal.org_id]
    if not rows:
        raise HTTPException(404, "trace not found")
    auditor = principal.is_human and "auditor" in principal.roles
    readable = set() if auditor else await _readable(session, principal, rows)
    visible = rows if auditor else [s for s in rows if s.room_id in readable]
    if not visible:
        raise HTTPException(404, "trace not found")
    return TraceOut(
        trace_id=trace_id,
        span_count=len(visible),
        hidden_spans=len(rows) - len(visible),
        roots=_tree(visible),
    )


async def _readable(session: Session, principal: ReadPrincipal, rows: list[Span]) -> set[uuid.UUID]:
    room_ids = {s.room_id for s in rows if s.room_id is not None}
    conds = [
        exists().where(
            RoomMember.room_id == Room.id, RoomMember.participant_id == principal.participant_id
        )
    ]
    if principal.is_human:  # department access applies to humans only (db/access.py)
        conds.append(
            exists().where(
                RoomDepartmentAccess.room_id == Room.id,
                RoomDepartmentAccess.department_id == principal.department_id,
            )
        )
    found = await session.execute(
        select(Room.id).where(Room.id.in_(room_ids), Room.org_id == principal.org_id, or_(*conds))
    )
    return set(found.scalars())


def _tree(spans: list[Span]) -> list[SpanOut]:
    nodes: dict[str, SpanOut] = {}
    for s in spans:
        node = SpanOut.model_validate(s)
        if s.ended_at is not None:
            node.duration_ms = round((s.ended_at - s.started_at).total_seconds() * 1000)
        nodes[s.id] = node
    roots: list[SpanOut] = []
    for s in spans:  # spans arrive ordered by start time, so children keep that order
        parent = nodes.get(s.parent_span_id) if s.parent_span_id else None
        (parent.children if parent else roots).append(nodes[s.id])
    return roots
