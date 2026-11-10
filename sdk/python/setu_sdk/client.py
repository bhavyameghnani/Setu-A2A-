"""SetuClient: REST and WebSocket access to a Setu hub, as one participant (an API key).

Nothing here is clever. It sends what you ask, replays what you missed, acknowledges what you
processed, and reconnects when the socket drops. Routing and policy are the hub's job.
"""

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any, Self

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from setu_sdk.models import TERMINAL_TASK_STATES, Message, SetuError

PAGE = 200


def _id(value: str | uuid.UUID) -> str:
    return str(value)


class SetuClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._me: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ plumbing

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        resp = await self._http.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            json=json_body,
            params=dict(params) if params else None,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            raise SetuError(resp.status_code, str(detail))
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------ who and where

    async def whoami(self) -> dict[str, Any]:
        """This participant (cached): `{"participant": {...}, "roles": [...], "scopes": [...]}`."""
        if self._me is None:
            self._me = await self.request("GET", "/v1/auth/me")
        assert self._me is not None
        return self._me

    async def participant_id(self) -> str:
        return str((await self.whoami())["participant"]["id"])

    async def rooms(self) -> list[dict[str, Any]]:
        """Rooms this participant can see (as a member)."""
        out: list[dict[str, Any]] = await self.request("GET", "/v1/rooms")
        return out

    async def room(self, room_id: str | uuid.UUID) -> dict[str, Any]:
        out: dict[str, Any] = await self.request("GET", f"/v1/rooms/{_id(room_id)}")
        return out

    async def members(self, room_id: str | uuid.UUID) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self.request("GET", f"/v1/rooms/{_id(room_id)}/members")
        return out

    async def agents(self, skill: str | None = None) -> list[dict[str, Any]]:
        """The agent registry, optionally filtered by skill id or tag."""
        out: list[dict[str, Any]] = await self.request(
            "GET", "/v1/agents", params={"skill": skill} if skill else None
        )
        return out

    # ------------------------------------------------------------------ sending

    async def send(
        self,
        room_id: str | uuid.UUID,
        body: Mapping[str, Any],
        *,
        kind: str = "text",
        mentions: Iterable[str | uuid.UUID] = (),
        reply_to: str | uuid.UUID | None = None,
        task_id: str | uuid.UUID | None = None,
        delegation_id: str | uuid.UUID | None = None,
    ) -> Message:
        payload: dict[str, Any] = {
            "kind": kind,
            "body": dict(body),
            "mentions": [_id(m) for m in mentions],
        }
        if reply_to is not None:
            payload["reply_to"] = _id(reply_to)
        if task_id is not None:
            payload["task_id"] = _id(task_id)
        if delegation_id is not None:
            payload["delegation_id"] = _id(delegation_id)
        data = await self.request("POST", f"/v1/rooms/{_id(room_id)}/messages", json_body=payload)
        return Message.from_json(data)

    async def send_task(
        self,
        room_id: str | uuid.UUID,
        to: str | uuid.UUID,
        *,
        text: str | None = None,
        data: Any = None,
        skill: str | None = None,
        delegation_id: str | uuid.UUID | None = None,
        parent_task_id: str | uuid.UUID | None = None,
    ) -> Message:
        """Ask `to` (an agent in the room) to do something. Returns the request message; use
        `task_for(message)` / `subscribe_task` to follow the work it starts."""
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if data is not None:
            body["data"] = data
        if skill is not None:
            body["skill"] = skill
        return await self.send(
            room_id,
            body,
            kind="task",
            mentions=[to],
            task_id=parent_task_id,
            delegation_id=delegation_id,
        )

    # ------------------------------------------------------------------ reading and replay

    async def history(
        self, room_id: str | uuid.UUID, after_seq: int = 0, limit: int = PAGE
    ) -> list[Message]:
        rows = await self.request(
            "GET",
            f"/v1/rooms/{_id(room_id)}/messages",
            params={"after_seq": after_seq, "limit": limit},
        )
        return [Message.from_json(r) for r in rows]

    async def replay(self, room_id: str | uuid.UUID, after_seq: int = 0) -> AsyncIterator[Message]:
        """Every message after `after_seq`, in order, paging until the end."""
        last = after_seq
        while True:
            page = await self.history(room_id, last, PAGE)
            for m in page:
                yield m
                last = m.seq
            if len(page) < PAGE:
                return

    async def cursor(self, room_id: str | uuid.UUID) -> int:
        out = await self.request("GET", f"/v1/rooms/{_id(room_id)}/cursor")
        return int(out["last_seq"])

    async def ack(self, room_id: str | uuid.UUID, seq: int) -> int:
        """Record that everything up to `seq` is processed. Only moves forward."""
        out = await self.request(
            "PUT", f"/v1/rooms/{_id(room_id)}/cursor", json_body={"last_seq": seq}
        )
        return int(out["last_seq"])

    async def inbox(self, room_id: str | uuid.UUID) -> list[Message]:
        """Messages addressed to me that I have not finished (the catch-up path after a crash)."""
        rows = await self.request("GET", f"/v1/rooms/{_id(room_id)}/inbox")
        return [Message.from_json(r) for r in rows]

    async def set_status(
        self, room_id: str | uuid.UUID, message_id: str | uuid.UUID, status: str
    ) -> None:
        """`acked`, `completed` or `failed` for a message addressed to me."""
        await self.request(
            "PUT",
            f"/v1/rooms/{_id(room_id)}/messages/{_id(message_id)}/status",
            json_body={"status": status},
        )

    async def stream(
        self,
        room_id: str | uuid.UUID,
        *,
        after_seq: int | None = None,
        reconnect: bool = True,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
    ) -> AsyncIterator[Message]:
        """Live messages for a room, over WebSocket.

        Without `after_seq` the hub resumes from my stored cursor. After a dropped connection
        this reconnects (exponential backoff with jitter) and continues from the last message
        yielded, so nothing is skipped; delivery is at-least-once, so dedupe by `message.id`.
        """
        url = self.base_url.replace("http", "ws", 1) + f"/v1/ws/rooms/{_id(room_id)}"
        last = after_seq
        delay = backoff_initial
        while True:
            target = url if last is None else f"{url}?after_seq={last}"
            try:
                async with websockets.connect(
                    target, additional_headers=self.headers, open_timeout=10
                ) as ws:
                    delay = backoff_initial
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") == "message":
                            msg = Message.from_json(frame["message"])
                            last = msg.seq
                            yield msg
                        elif frame.get("type") == "error":
                            raise SetuError(400, str(frame.get("detail")))
                if not reconnect:
                    return
            except InvalidStatus as exc:
                raise SetuError(exc.response.status_code, "websocket refused") from exc
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                if (
                    isinstance(exc, ConnectionClosed)
                    and exc.rcvd
                    and exc.rcvd.code in (4401, 4403, 4404)
                ):
                    raise SetuError(exc.rcvd.code, exc.rcvd.reason or "closed by the hub") from exc
                if not reconnect:
                    raise
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, backoff_max)

    # ------------------------------------------------------------------ tasks

    async def get_task(self, task_id: str | uuid.UUID) -> dict[str, Any]:
        out: dict[str, Any] = await self.request("GET", f"/v1/tasks/{_id(task_id)}")
        return out

    async def tasks(self, room_id: str | uuid.UUID) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self.request("GET", f"/v1/rooms/{_id(room_id)}/tasks")
        return out

    async def cancel_task(self, task_id: str | uuid.UUID) -> dict[str, Any]:
        out: dict[str, Any] = await self.request("POST", f"/v1/tasks/{_id(task_id)}/cancel")
        return out

    async def task_for(
        self, message: Message, *, max_wait: float = 5.0, assignee: str | None = None
    ) -> dict[str, Any]:
        """The Setu task a `task` message created (it is created with the message)."""
        deadline = time.monotonic() + max_wait
        while True:
            for t in await self.tasks(message.room_id):
                if t["request_message_id"] == message.id and assignee in (None, t["assignee_id"]):
                    return t
            if time.monotonic() > deadline:
                raise SetuError(
                    404, "no task was created for that message (is the agent a2a_remote?)"
                )
            await asyncio.sleep(0.1)

    async def subscribe_task(
        self,
        task_id: str | uuid.UUID,
        *,
        poll: float = 0.25,
        max_wait: float | None = None,
        stop_when_interrupted: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the task each time its state or artifacts change, until it finishes.

        A task waiting for a human (`input_required`, e.g. a Compliance approval) is not finished:
        by default this keeps waiting through it. Pass `stop_when_interrupted=True` to stop there.
        """
        deadline = None if max_wait is None else time.monotonic() + max_wait
        seen: tuple[str, int] | None = None
        while True:
            task = await self.get_task(task_id)
            key = (task["state"], len(task.get("artifacts") or []))
            if key != seen:
                seen = key
                yield task
            state = task["state"]
            if state in TERMINAL_TASK_STATES or (
                stop_when_interrupted and state in ("input_required", "auth_required")
            ):
                return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"task {task_id} still {state} after {max_wait}s")
            await asyncio.sleep(poll)

    async def wait_task(
        self, task_id: str | uuid.UUID, *, max_wait: float | None = 120.0
    ) -> dict[str, Any]:
        """The task once it is finished (completed, failed, canceled or rejected)."""
        last: dict[str, Any] = {}
        async for update in self.subscribe_task(task_id, max_wait=max_wait):
            last = update
        return last

    # ------------------------------------------------------------------ delegation

    async def delegate(
        self,
        to: str | uuid.UUID,
        scopes: Sequence[str],
        *,
        parent_id: str | uuid.UUID | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Grant `scopes` to an agent. Humans grant from their own authority; an agent passes
        part of a grant it holds by naming it as `parent_id` (scopes and expiry only narrow)."""
        payload: dict[str, Any] = {"to_participant_id": _id(to), "scopes": list(scopes)}
        if parent_id is not None:
            payload["parent_id"] = _id(parent_id)
        if expires_at is not None:
            payload["expires_at"] = expires_at
        out: dict[str, Any] = await self.request("POST", "/v1/delegations", json_body=payload)
        return out

    async def trace(self, trace_id: str) -> dict[str, Any]:
        out: dict[str, Any] = await self.request("GET", f"/v1/traces/{trace_id}")
        return out


__all__ = ["SetuClient", "SetuError"]
