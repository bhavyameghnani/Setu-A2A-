"""One shared Postgres LISTEN connection that wakes per-room and per-person subscribers."""

import asyncio
import contextlib
import uuid
from collections import defaultdict

import asyncpg
import structlog

from app.db.messages import NOTIFY_CHANNEL
from app.db.notifications import NOTIFY_CHANNEL as USER_CHANNEL

log = structlog.get_logger()


class Notifier:
    def __init__(self, database_url: str) -> None:
        # asyncpg wants a plain postgresql:// DSN, not the SQLAlchemy driver form.
        self._dsn = database_url.replace("+asyncpg", "")
        self._subs: dict[uuid.UUID, set[asyncio.Event]] = defaultdict(set)
        self._users: dict[uuid.UUID, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def subscribe(self, room_id: uuid.UUID) -> asyncio.Event:
        ev = asyncio.Event()
        self._subs[room_id].add(ev)
        return ev

    def unsubscribe(self, room_id: uuid.UUID, ev: asyncio.Event) -> None:
        self._subs[room_id].discard(ev)
        if not self._subs[room_id]:
            del self._subs[room_id]

    def subscribe_user(self, recipient: uuid.UUID) -> asyncio.Event:
        ev = asyncio.Event()
        self._users[recipient].add(ev)
        return ev

    def unsubscribe_user(self, recipient: uuid.UUID, ev: asyncio.Event) -> None:
        self._users[recipient].discard(ev)
        if not self._users[recipient]:
            del self._users[recipient]

    def _on_user_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            for ev in self._users.get(uuid.UUID(payload.split(":", 1)[0]), ()):
                ev.set()
        except ValueError:
            log.warning("bad_notify_payload", payload=payload)

    def _wake_room(self, room_id: uuid.UUID) -> None:
        for ev in self._subs.get(room_id, ()):
            ev.set()

    def _wake_all(self) -> None:
        for evs in (*self._subs.values(), *self._users.values()):
            for ev in evs:
                ev.set()

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            self._wake_room(uuid.UUID(payload.split(":", 1)[0]))
        except ValueError:
            log.warning("bad_notify_payload", payload=payload)

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                conn = await asyncpg.connect(self._dsn)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("listen_connect_failed", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            backoff = 0.5
            closed = asyncio.Event()
            conn.add_termination_listener(lambda _c, ev=closed: ev.set())
            await conn.add_listener(NOTIFY_CHANNEL, self._on_notify)
            await conn.add_listener(USER_CHANNEL, self._on_user_notify)
            # Anything committed while we were disconnected is caught by a fetch-by-seq.
            self._wake_all()
            try:
                await closed.wait()
            finally:
                with contextlib.suppress(Exception):
                    await conn.close(timeout=2)
            log.warning("listen_connection_lost")
