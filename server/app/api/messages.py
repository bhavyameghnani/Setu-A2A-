import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import ReadPrincipal, SendPrincipal, Session
from app.db import messages as repo
from app.db.access import room_access
from app.db.control import ControlDenied
from app.db.models import Message
from app.schemas import MessageCreate, MessageOut

router = APIRouter(prefix="/v1", tags=["messages"])


@router.post("/rooms/{room_id}/messages", response_model=MessageOut, status_code=201)
async def post_message(
    room_id: uuid.UUID,
    body: MessageCreate,
    principal: SendPrincipal,
    session: Session,
    request: Request,
) -> Message:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.can_read:
        raise HTTPException(404, "room not found")
    if not acc.can_post:
        raise HTTPException(403, "you can read this room but not post to it")
    try:
        msg = await repo.post_message(session, room_id, principal.participant_id, body)
    except repo.InvalidReferenceError as e:
        raise HTTPException(422, str(e)) from None
    except ControlDenied as e:
        raise HTTPException(403, e.reason) from None
    if msg.kind == "task":
        request.app.state.dispatcher.wake()
    return msg


@router.get("/rooms/{room_id}/messages", response_model=list[MessageOut])
async def list_messages(
    room_id: uuid.UUID,
    principal: ReadPrincipal,
    session: Session,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Sequence[Message]:
    acc = await room_access(session, principal, room_id)
    if acc is None or not acc.can_read:
        raise HTTPException(404, "room not found")
    return await repo.list_messages(session, room_id, after_seq, limit)
