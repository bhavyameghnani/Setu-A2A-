"""WebSocket gateway: replay from a cursor, then stream live.

Connect: /v1/ws/rooms/{room_id}[?after_seq=<n>]. Without `after_seq` the stream resumes from the
caller's stored cursor (0 if none), which is how a restarted agent catches up.
Authenticated with an `Authorization: Bearer` header, an `X-API-Key` header, or (for
browsers, which cannot set headers) `?token=` / `?api_key=`.
Query-string credentials can end up in access logs; prefer headers wherever the client allows.
Close codes: 4401 unauthenticated, 4403 missing scope, 4404 room not visible.
Server -> client: {"type":"message","message":{...}} in seq order, then {"type":"error",...}.
Client -> server: {"type":"send","kind":"text","body":{...},"mentions":[...]}
                  {"type":"ack","seq":N}  advance my cursor to N (processed up to and including N)
                  {"type":"status","message_id":"...","status":"acked|completed|failed"}
Messages carry `addressed`: act only if your id is in it (the only-act-when-addressed guard).
Sent messages come back through the same stream (to every subscriber, sender included).
"""

import asyncio
import contextlib
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import resolve_principal
from app.core.auth import AuthError, Principal
from app.core.lifecycle import DeliveryStatus, InvalidTransitionError
from app.core.replay import resume_seq
from app.db import delivery
from app.db import messages as repo
from app.db.access import room_access
from app.db.control import ControlDenied
from app.schemas import MessageCreate, MessageOut
from app.ws.notify import Notifier

router = APIRouter()
log = structlog.get_logger()
POLL_FALLBACK_SECONDS = 5.0  # safety net if a NOTIFY is ever missed
BATCH = 500


async def _stream(ws: WebSocket, room_id: uuid.UUID, after_seq: int) -> None:
    notifier: Notifier = ws.app.state.notifier
    event = notifier.subscribe(room_id)  # subscribe BEFORE the first fetch: no gap
    last = after_seq
    try:
        while True:
            event.clear()  # clear BEFORE fetching so a NOTIFY during the fetch re-arms us
            while True:
                async with ws.app.state.sessionmaker() as session:
                    rows = await repo.list_messages(session, room_id, last, BATCH)
                for row in rows:
                    out = MessageOut.model_validate(row)
                    await ws.send_json({"type": "message", "message": out.model_dump(mode="json")})
                    last = row.seq
                if len(rows) < BATCH:
                    break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), POLL_FALLBACK_SECONDS)
    finally:
        notifier.unsubscribe(room_id, event)


async def _ack(
    ws: WebSocket, room_id: uuid.UUID, principal: Principal, frame: dict[str, Any]
) -> None:
    seq = frame.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        await ws.send_json({"type": "error", "detail": "ack needs an integer seq"})
        return
    try:
        async with ws.app.state.sessionmaker() as session:
            stored = await delivery.set_cursor(session, room_id, principal.participant_id, seq)
    except ValueError as e:
        await ws.send_json({"type": "error", "detail": str(e)})
        return
    await ws.send_json({"type": "cursor", "last_seq": stored})


async def _status(
    ws: WebSocket, room_id: uuid.UUID, principal: Principal, frame: dict[str, Any]
) -> None:
    if not principal.has_scope("messages:send"):
        await ws.send_json({"type": "error", "detail": "api key lacks scope messages:send"})
        return
    try:
        message_id = uuid.UUID(str(frame.get("message_id")))
        target = DeliveryStatus(str(frame.get("status")))
        if target == DeliveryStatus.PENDING:
            raise ValueError("status must be acked, completed or failed")
        async with ws.app.state.sessionmaker() as session:
            row = await delivery.set_status(
                session, room_id, message_id, principal.participant_id, target
            )
    except (ValueError, delivery.NoSuchStatusError, InvalidTransitionError) as e:
        await ws.send_json({"type": "error", "detail": str(e)})
        return
    await ws.send_json({"type": "status", "message_id": str(message_id), "status": row.status})


_SEND_FIELDS = ("kind", "body", "mentions", "reply_to", "delegation_id")


async def _receive(ws: WebSocket, room_id: uuid.UUID, principal: Principal) -> None:
    while True:
        frame = await ws.receive_json()
        if not isinstance(frame, dict):
            await ws.send_json({"type": "error", "detail": "expected a JSON object"})
            continue
        if frame.get("type") == "ack":
            await _ack(ws, room_id, principal, frame)
            continue
        if frame.get("type") == "status":
            await _status(ws, room_id, principal, frame)
            continue
        if frame.get("type") != "send":
            await ws.send_json({"type": "error", "detail": "unknown frame type"})
            continue
        if not principal.has_scope("messages:send"):
            await ws.send_json({"type": "error", "detail": "api key lacks scope messages:send"})
            continue
        try:
            data = MessageCreate(
                **{
                    k: v
                    for k, v in frame.items()
                    if k in ("kind", "body", "mentions", "reply_to", "delegation_id")
                }
            )
        except ValidationError as e:
            await ws.send_json({"type": "error", "detail": e.errors(include_url=False)[0]["msg"]})
            continue
        try:
            async with ws.app.state.sessionmaker() as session:
                # Re-checked on every send: access can change while the socket stays open.
                acc = await room_access(session, principal, room_id)
                if acc is None or not acc.can_post:
                    await ws.send_json({"type": "error", "detail": "you cannot post to this room"})
                    continue
                posted = await repo.post_message(session, room_id, principal.participant_id, data)
            if posted.kind == "task":
                ws.app.state.dispatcher.wake()
        except repo.InvalidReferenceError as e:
            await ws.send_json({"type": "error", "detail": str(e)})
        except ControlDenied as e:
            await ws.send_json({"type": "error", "detail": e.reason, "denied": True})


@router.websocket("/v1/ws/rooms/{room_id}")
async def room_socket(
    ws: WebSocket,
    room_id: uuid.UUID,
    after_seq: int | None = None,
    token: str | None = None,
    api_key: str | None = None,
) -> None:
    header = ws.headers.get("authorization")
    bearer = header[7:].strip() if header and header.lower().startswith("bearer ") else token
    key = ws.headers.get("x-api-key") or api_key
    try:
        async with ws.app.state.sessionmaker() as session:
            principal = await resolve_principal(session, ws.app.state.jwt_secret, bearer, key)
            acc = await room_access(session, principal, room_id)
            cursor = (
                await delivery.get_cursor(session, room_id, principal.participant_id)
                if acc is not None and acc.can_read
                else None
            )
    except AuthError:
        await ws.close(code=4401, reason="unauthenticated")
        return
    if not principal.has_scope("rooms:read"):
        await ws.close(code=4403, reason="api key lacks scope rooms:read")
        return
    if acc is None or not acc.can_read:
        await ws.close(code=4404, reason="room not found")
        return
    await ws.accept()
    tasks = [
        asyncio.create_task(_stream(ws, room_id, resume_seq(after_seq, cursor))),
        asyncio.create_task(_receive(ws, room_id, principal)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await t
