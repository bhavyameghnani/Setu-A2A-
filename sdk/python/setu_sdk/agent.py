"""Agent: a long-running participant that acts only when it is addressed.

Subclass it and implement `on_message`. For every room the agent belongs to, `run()`:

* streams messages (replaying from the hub's stored cursor, so a restarted agent catches up);
* calls `on_message` only for messages that name this agent in `addressed` (the guard from the
  routing rules); everything else goes to `on_context` and is otherwise ignored;
* marks each addressed message `acked`, then `completed` (or `failed` if your code raised);
* advances the cursor after each message, so a crash resumes at the first unprocessed one;
* dedupes by message id (delivery is at-least-once) and skips replayed messages it already
  finished before a crash;
* reconnects with exponential backoff.

The hub never calls an SDK agent; it only addresses it. To answer, use `reply` or the client.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

from setu_sdk.client import SetuClient
from setu_sdk.models import Message, SetuError

log = logging.getLogger("setu_sdk.agent")


class Agent:
    def __init__(
        self,
        client: SetuClient,
        *,
        rooms: Iterable[str | uuid.UUID] | None = None,
        discover_every: float = 3.0,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        dedupe_size: int = 2048,
    ) -> None:
        """`rooms=None` means every room this agent is a member of (re-checked periodically, so
        rooms it is added to later are joined without a restart)."""
        self.client = client
        self._fixed = None if rooms is None else [str(r) for r in rooms]
        self._discover_every = discover_every
        self._backoff = (backoff_initial, backoff_max)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedupe_size = dedupe_size
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self.me: str = ""

    # ------------------------------------------------------------------ override these

    async def on_message(self, msg: Message) -> None:
        """Called once per message addressed to this agent. Raise to mark it `failed`.

        Not every addressed message is a request: Setu also addresses you with the replies to
        tasks you sent (`task_update`, `result`) and with approval messages. Check `msg.kind`
        (requests are `text` and `task`) before acting."""
        raise NotImplementedError

    async def on_context(self, msg: Message) -> None:  # noqa: B027  (optional hook)
        """Called for messages in the room that are not for this agent. Do not act on them."""

    # ------------------------------------------------------------------ helpers for subclasses

    async def reply(
        self,
        msg: Message,
        body: Mapping[str, Any],
        *,
        kind: str = "text",
        mention_sender: bool = True,
    ) -> Message:
        """Post into the same room, in the same task thread, addressed to whoever asked."""
        return await self.client.send(
            msg.room_id,
            body,
            kind=kind,
            mentions=[msg.sender_id] if mention_sender else [],
            reply_to=msg.id,
            task_id=msg.task_id,
        )

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Run until `stop()` is called (or the task is cancelled)."""
        self.me = await self.client.participant_id()
        try:
            while not self._stop.is_set():
                for room_id in await self._room_ids():
                    task = self._loops.get(room_id)
                    if task is None or task.done():
                        self._loops[room_id] = asyncio.create_task(self._room_loop(room_id))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self._discover_every)
        finally:
            await self._cancel_loops()

    async def stop(self) -> None:
        self._stop.set()

    async def _cancel_loops(self) -> None:
        loops = list(self._loops.values())
        for t in loops:
            t.cancel()
        for t in loops:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._loops.clear()

    async def _room_ids(self) -> list[str]:
        if self._fixed is not None:
            return self._fixed
        try:
            return [r["id"] for r in await self.client.rooms()]
        except (SetuError, OSError) as exc:
            log.warning("could not list rooms: %s", exc)
            return list(self._loops)

    # ------------------------------------------------------------------ one room

    async def _room_loop(self, room_id: str) -> None:
        delay, cap = self._backoff
        while not self._stop.is_set():
            try:
                horizon = int((await self.client.room(room_id))["last_seq"])
                unfinished = {m.id for m in await self.client.inbox(room_id)}
                # after_seq=None: the hub resumes from my stored cursor.
                async for msg in self.client.stream(room_id, reconnect=False):
                    await self._handle(msg, horizon, unfinished)
                    delay = self._backoff[0]
            except asyncio.CancelledError:
                raise
            except SetuError as exc:
                if exc.status in (401, 403, 404):  # not allowed (any more): stop trying this room
                    log.warning("leaving room %s: %s", room_id, exc)
                    return
                log.warning("room %s: %s", room_id, exc)
            except Exception as exc:
                log.warning("room %s stream ended: %r", room_id, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, cap)

    async def _handle(self, msg: Message, horizon: int, unfinished: set[str]) -> None:
        if msg.id in self._seen:
            await self._advance(msg)
            return
        addressed = self.me in msg.addressed and msg.sender_id != self.me
        # A message from before this connection that I am not still owed: I finished it already.
        done_before = msg.seq <= horizon and msg.id not in unfinished
        if not addressed:
            if msg.sender_id != self.me:
                await self._guarded(self.on_context, msg)
        elif not done_before:
            await self._run_message(msg)
        self._remember(msg.id)
        await self._advance(msg)

    async def _run_message(self, msg: Message) -> None:
        await self._status(msg, "acked")
        try:
            await self.on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_message failed for %s", msg.id)
            await self._status(msg, "failed")
        else:
            await self._status(msg, "completed")

    async def _guarded(self, fn: Any, msg: Message) -> None:
        try:
            await fn(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed for %s", getattr(fn, "__name__", "hook"), msg.id)

    async def _status(self, msg: Message, status: str) -> None:
        try:
            await self.client.set_status(msg.room_id, msg.id, status)
        except SetuError as exc:
            if exc.status not in (404, 409):  # already terminal / not addressed: nothing to do
                raise

    async def _advance(self, msg: Message) -> None:
        try:
            await self.client.ack(msg.room_id, msg.seq)
        except SetuError as exc:
            if exc.status != 422:  # 422: a cursor never moves backwards; harmless on replay
                raise

    def _remember(self, message_id: str) -> None:
        self._seen[message_id] = None
        while len(self._seen) > self._dedupe_size:
            self._seen.popitem(last=False)
