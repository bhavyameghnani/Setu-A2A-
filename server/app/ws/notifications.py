"""Live inbox: /v1/ws/notifications[?after_id=<notification id>].

Authenticated like the room socket (Authorization header, or ?token= for browsers). Humans only.
Server -> client:
    {"type":"unread","unread":N}                          once, on connect
    {"type":"notification","notification":{...},"unread":N}   each new notification, oldest first
Without `after_id` only notifications created after connecting are pushed (the inbox API is
the way to read history); with it, everything newer than that id is replayed first. The push is
a Postgres NOTIFY with ids only; the row is read from the database, so nothing is ever lost
to a missed wake-up (a periodic poll is the safety net).
Close codes: 4401 unauthenticated, 4403 not a person / missing scope.
"""

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.api.deps import resolve_principal
from app.api.notifications import actions_for, unread_count
from app.core.auth import AuthError, Principal
from app.db.models import Notification
from app.schemas import NotificationOut

router = APIRouter()
POLL_FALLBACK_SECONDS = 5.0


async def _latest(ws: WebSocket, recipient: uuid.UUID) -> uuid.UUID | None:
    async with ws.app.state.sessionmaker() as s:
        found = (
            await s.execute(
                select(Notification.id)
                .where(Notification.recipient_id == recipient)
                .order_by(Notification.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return found if isinstance(found, uuid.UUID) else None


async def _push(ws: WebSocket, principal: Principal, after: uuid.UUID | None) -> None:
    notifier = ws.app.state.notifier
    event = notifier.subscribe_user(principal.participant_id)  # before the first read: no gap
    last = after
    try:
        while True:
            event.clear()
            async with ws.app.state.sessionmaker() as s:
                q = select(Notification).where(
                    Notification.recipient_id == principal.participant_id
                )
                if last is not None:
                    q = q.where(Notification.id > last)
                rows = list((await s.execute(q.order_by(Notification.id).limit(200))).scalars())
                for n in rows:
                    out = NotificationOut.model_validate(n)
                    out.actions = await actions_for(s, principal, n)
                    await ws.send_json(
                        {
                            "type": "notification",
                            "notification": out.model_dump(mode="json"),
                            "unread": await unread_count(s, principal.participant_id),
                        }
                    )
                    last = n.id
            if len(rows) == 200:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), POLL_FALLBACK_SECONDS)
    finally:
        notifier.unsubscribe_user(principal.participant_id, event)


@router.websocket("/v1/ws/notifications")
async def notification_socket(
    ws: WebSocket, after_id: uuid.UUID | None = None, token: str | None = None
) -> None:
    header = ws.headers.get("authorization")
    bearer = header[7:].strip() if header and header.lower().startswith("bearer ") else token
    try:
        async with ws.app.state.sessionmaker() as session:
            principal = await resolve_principal(
                session, ws.app.state.jwt_secret, bearer, ws.headers.get("x-api-key")
            )
            unread = await unread_count(session, principal.participant_id)
    except AuthError:
        await ws.close(code=4401, reason="unauthenticated")
        return
    if not principal.is_human or not principal.has_scope("rooms:read"):
        await ws.close(code=4403, reason="notifications are for people")
        return
    start = after_id if after_id is not None else await _latest(ws, principal.participant_id)
    await ws.accept()
    await ws.send_json({"type": "unread", "unread": unread})
    pusher = asyncio.create_task(_push(ws, principal, start))
    watcher = asyncio.create_task(_drain(ws))
    try:
        await asyncio.wait({pusher, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (pusher, watcher):
            t.cancel()
        for t in (pusher, watcher):
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t


async def _drain(ws: WebSocket) -> None:
    """Notice the client going away (we expect no frames from it)."""
    while True:
        await ws.receive_text()
