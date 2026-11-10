"""Setu as an A2A server (spec 8.1, role 2): every registered agent is reachable at
`/a2a/{handle}` with a proxied Agent Card that points at Setu, never at the agent.

A call arrives as plain A2A (1.0 or 0.3), is authenticated with the caller's Setu API key, and
becomes a `task` message in the room named by `contextId`. From there it is the same path as a
call made from the room UI: policy, delegation, approvals, audit, spans and the dispatcher.
The executor below streams the task's progress back as A2A events.
"""

import asyncio
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

import structlog
from a2a.helpers.proto_helpers import new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import InvalidParamsError, TaskNotCancelableError
from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import cancel, facade_store, versions
from app.a2a.mapping import scrub
from app.a2a.types import Part, text_of
from app.core.auth import Principal
from app.core.lifecycle import TERMINAL_TASK_STATES, TaskState
from app.core.tracing import TraceContext
from app.db import messages as repo
from app.db.access import room_access
from app.db.control import ControlDenied
from app.db.models import Agent, Message, Participant, RoomMember, Task
from app.schemas import MessageCreate

log = structlog.get_logger()

API_KEY_HEADER = "X-API-Key"
SCHEME = "setu_api_key"
POLL_SECONDS = 0.05
TERMINAL_WAIT_SECONDS = 3.0
# Per-request facts for the long-lived executor: who is calling now, and the trace they sent.
# (Set by the HTTP endpoint before the SDK runs; the SDK's tasks inherit them.)
current_principal: ContextVar[Principal | None] = ContextVar("facade_principal", default=None)
current_trace: ContextVar[TraceContext | None] = ContextVar("facade_trace", default=None)


# ------------------------------------------------------------------ cards


def _skills(agent: Agent) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in agent.skills or []:
        if isinstance(s, dict) and s.get("id"):
            out.append(
                {
                    "id": str(s["id"]),
                    "name": str(s.get("name") or s["id"]),
                    "description": str(s.get("description") or ""),
                    "tags": [str(t) for t in s.get("tags") or []],
                }
            )
    return out


def _common(p: Participant, agent: Agent) -> dict[str, Any]:
    stored = agent.a2a_card_json or {}
    modes_in = stored.get("defaultInputModes")
    modes_out = stored.get("defaultOutputModes")
    return {
        "name": p.display_name,
        "description": str(stored.get("description") or ""),
        "version": str(stored.get("version") or "1.0.0"),
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": modes_in if isinstance(modes_in, list) else ["text/plain"],
        "defaultOutputModes": modes_out if isinstance(modes_out, list) else ["text/plain"],
        "skills": _skills(agent),
    }  # an allow-list: provider, documentation and icon URLs from the origin card are dropped


def card_v1(p: Participant, agent: Agent, rpc_url: str) -> dict[str, Any]:
    return {
        **_common(p, agent),
        "supportedInterfaces": [
            {"url": rpc_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": rpc_url, "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
        ],
        "securitySchemes": {
            SCHEME: {"apiKeySecurityScheme": {"location": "header", "name": API_KEY_HEADER}}
        },
        "securityRequirements": [{"schemes": {SCHEME: {"list": []}}}],
    }


def card_v03(p: Participant, agent: Agent, rpc_url: str) -> dict[str, Any]:
    return {
        **_common(p, agent),
        "protocolVersion": "0.3.0",
        "url": rpc_url,
        "preferredTransport": "JSONRPC",
        "securitySchemes": {SCHEME: {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}},
        "security": [{SCHEME: []}],
    }


def card_pb(card: Mapping[str, Any]) -> pb.AgentCard:
    out: pb.AgentCard = ParseDict(dict(card), pb.AgentCard())
    return out


# ------------------------------------------------------------------ executor


def _setu_meta(message: pb.Message) -> dict[str, Any]:
    meta = MessageToDict(message.metadata) if message.HasField("metadata") else {}
    inner = meta.get("setu")
    return inner if isinstance(inner, dict) else {}


def _parts_in(message: pb.Message) -> list[Part]:
    raw = [MessageToDict(p) for p in message.parts]
    return list(versions.parts_from_wire(raw))


def _parts_out(parts: list[Any]) -> list[pb.Part]:
    return [ParseDict(dict(p), pb.Part()) for p in parts if isinstance(p, dict)]


class FacadeExecutor(AgentExecutor):
    """Bridges A2A calls into Setu. One instance per (caller, callee) pair, kept for the life of
    the process so a later `CancelTask` reaches the task a streaming call started. The caller
    and trace of the request being served come from the context variables above."""

    def __init__(self, state: Any, callee: uuid.UUID) -> None:
        self.state = state
        self.sm: async_sessionmaker[AsyncSession] = state.sessionmaker
        self.callee = callee

    @property
    def principal(self) -> Principal:
        p = current_principal.get()
        assert p is not None, "the endpoint authenticates before the SDK runs"
        return p

    # -------------------------------------------------------------- execute

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is not None:
            raise InvalidParamsError(
                "continue an existing task by answering its approval in Setu, not by re-sending"
            )
        room_id = self._room(context)
        principal, trace = self.principal, current_trace.get()
        message = context.message
        assert message is not None
        meta = _setu_meta(message)
        parts = _parts_in(message)
        body: dict[str, Any] = {"parts": parts}
        if text_of(parts):
            body["text"] = text_of(parts)
        if isinstance(meta.get("skill"), str):
            body["skill"] = meta["skill"]
        delegation = self._uuid(meta.get("delegation_id"), "metadata.setu.delegation_id")

        async with self.sm() as s:
            await self._check_room(s, principal, room_id)
            try:
                posted = await repo.post_message(
                    s,
                    room_id,
                    principal.participant_id,
                    MessageCreate(
                        kind="task", body=body, mentions=[self.callee], delegation_id=delegation
                    ),
                    incoming=trace,
                )
            except ControlDenied as denied:
                await self._rejected(event_queue, context, room_id, denied.reason)
                return
            except repo.InvalidReferenceError as exc:
                raise InvalidParamsError(str(exc)) from None
            task = (
                await s.execute(
                    select(Task).where(
                        Task.request_message_id == posted.id, Task.assignee_id == self.callee
                    )
                )
            ).scalar_one_or_none()
            if task is None:
                raise InvalidParamsError("that agent cannot take tasks right now")
            task_id, trace_id = task.id, task.trace_id
        self.state.dispatcher.wake()

        # The SDK fixes the wire task id before we run; remember which Setu task it stands for.
        wire_id = context.task_id or str(task_id)
        await facade_store.link(
            self.sm, wire_id, principal.participant_id, self.callee, task_id, str(room_id)
        )
        initial = pb.Task(
            id=wire_id,
            context_id=str(room_id),
            status=pb.TaskStatus(state=pb.TASK_STATE_SUBMITTED),
        )
        if trace_id:
            initial.metadata.update({"setu": {"trace_id": trace_id}})
        await event_queue.enqueue_event(initial)
        await self._follow(TaskUpdater(event_queue, wire_id, str(room_id)), task_id, wire_id)

    def _room(self, context: RequestContext) -> uuid.UUID:
        try:
            return uuid.UUID(context.context_id or "")
        except ValueError:
            raise InvalidParamsError(
                "contextId must be the id of a Setu room the caller is a member of"
            ) from None

    @staticmethod
    def _uuid(value: Any, what: str) -> uuid.UUID | None:
        if value is None:
            return None
        try:
            return uuid.UUID(str(value))
        except ValueError:
            raise InvalidParamsError(f"{what} must be a delegation id") from None

    async def _check_room(self, s: AsyncSession, principal: Principal, room_id: uuid.UUID) -> None:
        acc = await room_access(s, principal, room_id)
        if acc is None or not acc.can_read:  # same answer as "no such room": no existence leak
            raise InvalidParamsError("no such room for this caller")
        if not acc.can_post:
            raise InvalidParamsError("the caller cannot post to that room")
        member = await s.get(RoomMember, (room_id, self.callee))
        if member is None or member.role == "observer":
            raise InvalidParamsError("that agent is not a participant in the room")

    async def _rejected(
        self, queue: EventQueue, context: RequestContext, room_id: uuid.UUID, reason: str
    ) -> None:
        """A policy/delegation denial: nothing was stored, so there is no Setu task. Answer
        with a task in the standard `rejected` state and the reason."""
        task_id = context.task_id or str(uuid.uuid4())
        await queue.enqueue_event(
            pb.Task(
                id=task_id,
                context_id=str(room_id),
                status=pb.TaskStatus(state=pb.TASK_STATE_SUBMITTED),
            )
        )
        u = TaskUpdater(queue, task_id, str(room_id))
        await u.reject(u.new_agent_message([new_text_part(scrub(reason))]))

    # -------------------------------------------------------------- follow a task

    async def _follow(self, u: TaskUpdater, task_id: uuid.UUID, wire_id: str) -> None:
        sent_state = TaskState.SUBMITTED
        last_seq = 0
        cancelled = self.state.facade_cancelled.setdefault(wire_id, asyncio.Event())
        try:
            while True:
                if cancelled.is_set():
                    return  # cancel() answered the caller
                async with self.sm() as s:
                    task = await s.get(Task, task_id)
                    if task is None:
                        return
                    state = TaskState(task.state)
                    last_seq = await self._emit_results(s, u, task_id, last_seq)
                    final_text = await self._status_text(s, task_id, state)
                if state != sent_state:
                    if state == TaskState.WORKING:
                        await u.start_work()
                        sent_state = state
                    elif state in TERMINAL_TASK_STATES or state in (
                        TaskState.INPUT_REQUIRED,
                        TaskState.AUTH_REQUIRED,
                    ):
                        # Let the closing task_update (and any results flushed with it) commit.
                        final_text = await self._settle(task_id, state)
                        async with self.sm() as s:
                            await self._emit_results(s, u, task_id, last_seq)
                        await self._finish(u, state, final_text)
                        return
                    else:
                        sent_state = state
                await asyncio.sleep(POLL_SECONDS)
        finally:
            self.state.facade_cancelled.pop(wire_id, None)

    async def _emit_results(
        self, s: AsyncSession, u: TaskUpdater, task_id: uuid.UUID, after_seq: int
    ) -> int:
        rows = await s.execute(
            select(Message)
            .where(Message.task_id == task_id, Message.kind == "result", Message.seq > after_seq)
            .order_by(Message.seq)
        )
        for m in rows.scalars():
            after_seq = max(after_seq, m.seq)
            parts = _parts_out(list(m.body.get("parts") or []))
            if parts:
                await u.add_artifact(
                    parts,
                    artifact_id=str(m.body.get("artifact_id") or m.id),
                    name=m.body.get("name"),
                    metadata=(
                        {"setu": {"signature": m.body["signature"]}}
                        if m.body.get("signature")
                        else None
                    ),
                    last_chunk=True,
                )
        return after_seq

    async def _status_text(self, s: AsyncSession, task_id: uuid.UUID, state: TaskState) -> str:
        row = (
            await s.execute(
                select(Message)
                .where(
                    Message.task_id == task_id,
                    Message.kind.in_(("task_update", "approval_request")),
                )
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return ""
        b = row.body
        return str(b.get("error") or b.get("text") or b.get("prompt") or "")

    async def _settle(self, task_id: uuid.UUID, state: TaskState) -> str:
        """The state flips before its closing message commits when several are posted in a
        row; wait briefly for the message that carries the final text."""
        deadline = asyncio.get_running_loop().time() + TERMINAL_WAIT_SECONDS
        while True:
            async with self.sm() as s:
                row = (
                    await s.execute(
                        select(Message)
                        .where(
                            Message.task_id == task_id,
                            Message.kind.in_(("task_update", "approval_request")),
                        )
                        .order_by(Message.seq.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if row is not None:
                b = row.body
                closing = row.kind == "approval_request" or b.get("state") == state.value
                if closing:
                    return str(b.get("error") or b.get("text") or b.get("prompt") or "")
            if asyncio.get_running_loop().time() > deadline:
                return ""
            await asyncio.sleep(POLL_SECONDS)

    async def _finish(self, u: TaskUpdater, state: TaskState, text: str) -> None:
        msg = u.new_agent_message([new_text_part(scrub(text))]) if text else None
        if state == TaskState.COMPLETED:
            await u.complete(msg)
        elif state == TaskState.FAILED:
            await u.failed(msg)
        elif state == TaskState.CANCELED:
            await u.cancel(msg)
        elif state == TaskState.REJECTED:
            await u.reject(msg)
        elif state == TaskState.INPUT_REQUIRED:
            await u.requires_input(msg)
        else:
            await u.requires_auth(msg)

    # -------------------------------------------------------------- cancel

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        wire_id = context.task_id or ""
        principal = self.principal
        setu_task_id = await facade_store.setu_task_of(self.sm, wire_id, principal.participant_id)
        if setu_task_id is None:
            raise TaskNotCancelableError("unknown task")
        async with self.sm() as s:
            task = await s.get(Task, setu_task_id)
            if task is None:
                raise TaskNotCancelableError("unknown task")
            event = self.state.facade_cancelled.setdefault(wire_id, asyncio.Event())
            event.set()  # the streaming loop stops; this call answers the caller
            try:
                await cancel.cancel_task(self.state, s, principal, task)
            except cancel.CancelError as exc:
                event.clear()
                raise TaskNotCancelableError(scrub(str(exc))) from None
        await TaskUpdater(event_queue, wire_id, context.context_id or "").cancel()
