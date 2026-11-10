"""Persistent storage for the A2A facade (decision 124).

The a2a-sdk keeps tasks and push-notification configs in memory. Here they live in Postgres, so
a caller's `GetTask`, `ListTasks`, `CancelTask` and webhooks survive a Setu restart. Every row
belongs to the caller who created it: an agent can never read or cancel another agent's tasks by
guessing an id.
"""

import uuid
from datetime import UTC
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import PushNotificationConfigStore, TaskStore
from a2a.server.tasks.push_notification_config_store import normalize_push_notification_config
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import secrets
from app.core.lifecycle import TERMINAL_TASK_STATES, TaskState
from app.db.models import FacadePushConfig, FacadeTask, Task

_PB_STATE = {
    TaskState.SUBMITTED: pb.TASK_STATE_SUBMITTED,
    TaskState.WORKING: pb.TASK_STATE_WORKING,
    TaskState.INPUT_REQUIRED: pb.TASK_STATE_INPUT_REQUIRED,
    TaskState.AUTH_REQUIRED: pb.TASK_STATE_AUTH_REQUIRED,
    TaskState.COMPLETED: pb.TASK_STATE_COMPLETED,
    TaskState.FAILED: pb.TASK_STATE_FAILED,
    TaskState.CANCELED: pb.TASK_STATE_CANCELED,
    TaskState.REJECTED: pb.TASK_STATE_REJECTED,
}
_PB_TERMINAL = {_PB_STATE[s] for s in TERMINAL_TASK_STATES}


class NotYourTask(Exception):
    """A task id already belongs to another caller."""


async def link(
    sm: async_sessionmaker[AsyncSession],
    wire_id: str,
    caller_id: uuid.UUID,
    callee_id: uuid.UUID,
    setu_task_id: uuid.UUID,
    context_id: str,
) -> None:
    """Remember which Setu task an A2A task id stands for (before the SDK saves its snapshot)."""
    async with sm() as s:
        stmt = insert(FacadeTask).values(
            wire_task_id=wire_id,
            caller_id=caller_id,
            callee_id=callee_id,
            setu_task_id=setu_task_id,
            context_id=context_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[FacadeTask.wire_task_id],
            set_={"setu_task_id": setu_task_id, "updated_at": func.now()},
            where=FacadeTask.caller_id == caller_id,
        )
        await s.execute(stmt)
        await s.commit()


async def setu_task_of(
    sm: async_sessionmaker[AsyncSession], wire_id: str, caller_id: uuid.UUID
) -> uuid.UUID | None:
    """The Setu task behind a caller's A2A task id, or None if it is unknown or not theirs."""
    async with sm() as s:
        row = (
            await s.execute(
                select(FacadeTask.setu_task_id).where(
                    FacadeTask.wire_task_id == wire_id, FacadeTask.caller_id == caller_id
                )
            )
        ).first()
    return row[0] if row else None


class PgTaskStore(TaskStore):
    """The SDK task store, backed by `facade_tasks`. One per (caller, callee) handler."""

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        caller_id: uuid.UUID,
        callee_id: uuid.UUID,
    ) -> None:
        self.sm, self.caller_id, self.callee_id = sm, caller_id, callee_id

    def _mine(self) -> tuple[Any, ...]:
        return (FacadeTask.caller_id == self.caller_id, FacadeTask.callee_id == self.callee_id)

    async def save(self, task: pb.Task, context: ServerCallContext) -> None:
        snapshot = MessageToDict(task)
        values = {
            "wire_task_id": task.id,
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "context_id": task.context_id,
            "state": pb.TaskState.Name(task.status.state),
            "task": snapshot,
        }
        async with self.sm() as s:
            upsert = insert(FacadeTask).values(**values)
            stmt = upsert.on_conflict_do_update(
                index_elements=[FacadeTask.wire_task_id],
                set_={
                    "context_id": task.context_id,
                    "state": values["state"],
                    "task": snapshot,
                    "updated_at": func.now(),
                },
                where=FacadeTask.caller_id == self.caller_id,
            ).returning(FacadeTask.wire_task_id)
            if (await s.execute(stmt)).first() is None:
                raise NotYourTask(task.id)
            await s.commit()

    async def get(self, task_id: str, context: ServerCallContext) -> pb.Task | None:
        async with self.sm() as s:
            row = (
                await s.execute(
                    select(FacadeTask).where(FacadeTask.wire_task_id == task_id, *self._mine())
                )
            ).scalar_one_or_none()
            if row is None or not row.task:
                return None
            return await self._to_pb(s, row)

    async def _to_pb(self, s: AsyncSession, row: FacadeTask) -> pb.Task:
        task: pb.Task = ParseDict(row.task, pb.Task())
        # A restart can leave a snapshot saying "working" for a task Setu has since finished or
        # failed (decision 63): report Setu's state, the source of truth.
        if task.status.state not in _PB_TERMINAL and row.setu_task_id is not None:
            setu = await s.get(Task, row.setu_task_id)
            if setu is not None and TaskState(setu.state) in TERMINAL_TASK_STATES:
                task.status.state = _PB_STATE[TaskState(setu.state)]
        return task

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        async with self.sm() as s:
            await s.execute(
                delete(FacadePushConfig).where(
                    FacadePushConfig.wire_task_id == task_id,
                    FacadePushConfig.caller_id == self.caller_id,
                )
            )
            await s.execute(
                delete(FacadeTask).where(FacadeTask.wire_task_id == task_id, *self._mine())
            )
            await s.commit()

    async def list(
        self, params: pb.ListTasksRequest, context: ServerCallContext
    ) -> pb.ListTasksResponse:
        where: list[Any] = [*self._mine(), FacadeTask.task != {}]
        if params.context_id:
            where.append(FacadeTask.context_id == params.context_id)
        if params.status != pb.TASK_STATE_UNSPECIFIED:
            where.append(FacadeTask.state == pb.TaskState.Name(params.status))
        if params.HasField("status_timestamp_after"):
            where.append(
                FacadeTask.updated_at > params.status_timestamp_after.ToDatetime(tzinfo=UTC)
            )
        size = min(params.page_size or 50, 100)
        offset = int(params.page_token) if params.page_token.isdigit() else 0
        async with self.sm() as s:
            total = (
                await s.execute(select(func.count()).select_from(FacadeTask).where(*where))
            ).scalar_one()
            rows = (
                (
                    await s.execute(
                        select(FacadeTask)
                        .where(*where)
                        .order_by(FacadeTask.updated_at.desc(), FacadeTask.wire_task_id)
                        .offset(offset)
                        .limit(size)
                    )
                )
                .scalars()
                .all()
            )
            tasks = [await self._to_pb(s, r) for r in rows]
        for t in tasks:
            if not params.include_artifacts:
                del t.artifacts[:]
            if params.history_length:
                del t.history[: max(0, len(t.history) - params.history_length)]
        more = offset + len(tasks) < total
        return pb.ListTasksResponse(
            tasks=tasks,
            next_page_token=str(offset + len(tasks)) if more else "",
            page_size=size,
            total_size=total,
        )


class PgPushConfigStore(PushNotificationConfigStore):
    """Webhook configs for facade tasks. The token and credentials are encrypted at rest; the
    caller is whoever is bound to the current request (`current_owner`)."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], key: str, current_owner: Any) -> None:
        self.sm, self.key, self._owner = sm, key, current_owner

    def _owner_id(self) -> uuid.UUID:
        principal = self._owner.get()
        assert principal is not None, "push configs are only managed inside an authenticated call"
        owner: uuid.UUID = principal.participant_id
        return owner

    def _pack(self, c: pb.TaskPushNotificationConfig) -> dict[str, Any]:
        secret: dict[str, Any] = {}
        if c.token:
            secret["token"] = c.token
        if c.HasField("authentication"):
            secret["authentication"] = MessageToDict(c.authentication)
        return secrets.encrypt_json(self.key, secret)

    def _unpack(self, row: FacadePushConfig) -> pb.TaskPushNotificationConfig:
        data = secrets.decrypt_json(self.key, row.secret)
        c = pb.TaskPushNotificationConfig(id=row.config_id, task_id=row.wire_task_id, url=row.url)
        c.token = str(data.get("token", ""))
        if "authentication" in data:
            ParseDict(data["authentication"], c.authentication)
        return c

    async def set_info(
        self,
        task_id: str,
        notification_config: pb.TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> pb.TaskPushNotificationConfig:
        stored = normalize_push_notification_config(task_id, notification_config)
        owner = self._owner_id()
        async with self.sm() as s:
            stmt = insert(FacadePushConfig).values(
                wire_task_id=task_id,
                config_id=stored.id,
                caller_id=owner,
                url=stored.url,
                secret=self._pack(stored),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[FacadePushConfig.wire_task_id, FacadePushConfig.config_id],
                set_={"url": stored.url, "secret": self._pack(stored)},
                where=FacadePushConfig.caller_id == owner,
            )
            await s.execute(stmt)
            await s.commit()
        out = pb.TaskPushNotificationConfig()
        out.CopyFrom(stored)
        return out

    async def get_info(
        self, task_id: str, context: ServerCallContext
    ) -> list[pb.TaskPushNotificationConfig]:
        async with self.sm() as s:
            rows = (
                await s.execute(
                    select(FacadePushConfig).where(
                        FacadePushConfig.wire_task_id == task_id,
                        FacadePushConfig.caller_id == self._owner_id(),
                    )
                )
            ).scalars()
            return [self._unpack(r) for r in rows]

    async def get_info_for_dispatch(self, task_id: str) -> list[pb.TaskPushNotificationConfig]:
        async with self.sm() as s:
            rows = (
                await s.execute(
                    select(FacadePushConfig).where(FacadePushConfig.wire_task_id == task_id)
                )
            ).scalars()
            return [self._unpack(r) for r in rows]

    async def delete_info(
        self, task_id: str, context: ServerCallContext, config_id: str | None = None
    ) -> None:
        cond = [
            FacadePushConfig.wire_task_id == task_id,
            FacadePushConfig.caller_id == self._owner_id(),
        ]
        if config_id is not None:
            cond.append(FacadePushConfig.config_id == config_id)
        async with self.sm() as s:
            await s.execute(delete(FacadePushConfig).where(*cond))
            await s.commit()
