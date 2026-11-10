"""Runs submitted tasks against their A2A agents and mirrors the results into the room.

Flow per task (spec 8.4, steps 4-7): claim -> call the agent -> turn each streamed event into a
`task_update` / `result` / `approval_request` message and a state change -> on any failure end
in a visible `failed` state. A failed call is never silent (spec 8.5).

Policy and delegation checks slot in before the call in Phase 5.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import mapping, registry
from app.a2a.client import A2AClient, A2AError, AgentEndpoint
from app.a2a.mapping import scrub
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.a2a.types import (
    ArtifactEvent,
    Event,
    MessageEvent,
    Part,
    StatusEvent,
    TaskSnapshot,
    text_of,
)
from app.config import Settings
from app.core import notifications as tmpl
from app.core import secrets
from app.core.lifecycle import TERMINAL_TASK_STATES, TaskState
from app.core.policy import Effect
from app.core.signing import signer_from_settings
from app.core.tracing import format_traceparent, new_span_id
from app.db import audit, control, notifications, spans
from app.db import messages as repo
from app.db.models import Agent, Approval, Message, Participant, Task, TaskArtifact
from app.db.tasks import apply_state
from app.schemas import MessageCreate, MessageKind

log = structlog.get_logger()


@dataclass
class _Run:
    """Per-run memory: which artifacts already produced a `result` message."""

    task_id: uuid.UUID
    posted: set[str] = field(default_factory=set)
    got_event: bool = False


class Dispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: A2AClient,
        settings: Settings,
    ) -> None:
        self._sm = sessionmaker
        self._client = client
        self._settings = settings
        self._key = settings.resolved_encryption_key()
        self._signer = signer_from_settings(settings)
        self._allow_private = settings.allow_private_agent_hosts()
        self._wake_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for t in list(self._running.values()):
            t.cancel()
        for t in list(self._running.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def wake(self) -> None:
        """Call after committing a `task` message so dispatch does not wait for the poll."""
        self._wake_event.set()

    def cancel_running(self, task_id: uuid.UUID) -> bool:
        running = self._running.get(task_id)
        if running is None:
            return False
        running.cancel()
        return True

    # ------------------------------------------------------------ main loop

    async def _loop(self) -> None:
        try:
            await self._recover()
        except Exception as exc:
            log.warning("dispatch_recovery_failed", error=str(exc))
        while True:
            self._wake_event.clear()  # clear first: a wake during the claim re-arms us
            try:
                for task_id in await self._claim():
                    self._running[task_id] = asyncio.create_task(self._guarded(task_id))
                for appr_id, task_id in await self._claim_resumes():
                    if task_id not in self._running:
                        self._running[task_id] = asyncio.create_task(
                            self._guarded(task_id, appr_id)
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dispatch_claim_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), 1.0)

    async def _recover(self) -> None:
        """Single-instance assumption: a task claimed before a restart has no live runner, so
        end it visibly instead of leaving it `working` forever. Unclaimed ones are just run."""
        async with self._sm() as s:
            ids = list(
                (
                    await s.execute(
                        select(Task.id).where(
                            Task.claimed_at.is_not(None),
                            Task.state.in_(("submitted", "working")),
                        )
                    )
                ).scalars()
            )
        for task_id in ids:
            await self._fail(task_id, "the hub restarted while this task was running")

    async def _claim(self) -> list[uuid.UUID]:
        free = self._settings.a2a_max_concurrent_tasks - len(self._running)
        if free <= 0:
            return []
        async with self._sm() as s:
            ids = list(
                (
                    await s.execute(
                        select(Task.id)
                        .where(Task.state == "submitted", Task.claimed_at.is_(None))
                        .order_by(Task.created_at)
                        .limit(free)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if ids:
                await s.execute(
                    update(Task).where(Task.id.in_(ids)).values(claimed_at=datetime.now(UTC))
                )
            await s.commit()
        return ids

    async def _claim_resumes(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Answered `input_required` questions: the answer goes back on the same A2A task."""
        free = self._settings.a2a_max_concurrent_tasks - len(self._running)
        if free <= 0:
            return []
        async with self._sm() as s:
            rows = list(
                (
                    await s.execute(
                        select(Approval)
                        .where(
                            Approval.kind == "agent",
                            Approval.state == "approved",
                            Approval.resumed_at.is_(None),
                        )
                        .order_by(Approval.decided_at)
                        .limit(free)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for a in rows:
                a.resumed_at = datetime.now(UTC)
            out = [(a.id, a.task_id) for a in rows]
            await s.commit()
        return out

    async def _guarded(self, task_id: uuid.UUID, resume: uuid.UUID | None = None) -> None:
        cancelled = False
        try:
            await self._run_task(task_id, resume)
        except asyncio.CancelledError:
            cancelled = True  # user cancel or shutdown; the canceller sets state and ends the span
        except Exception as exc:
            log.exception("task_run_crashed", task=str(task_id))
            await self._fail(
                task_id, f"internal error while running the task: {exc.__class__.__name__}"
            )
        finally:
            self._running.pop(task_id, None)
            if not cancelled:
                await self._end_call(task_id)
            self.wake()  # a slot freed up

    async def _end_call(self, task_id: uuid.UUID) -> None:
        """Close the `a2a_call` span with how the call ended (the task state at that moment)."""
        try:
            async with self._sm() as s:
                task = await s.get(Task, task_id)
                if task is not None and task.span_id:
                    await spans.end(s, task.span_id, task.state, {"error": task.error})
                    await s.commit()
        except Exception as exc:
            log.warning("end_call_span_failed", task=str(task_id), error=str(exc))

    # ------------------------------------------------------------ one task

    async def _run_task(self, task_id: uuid.UUID, resume: uuid.UUID | None = None) -> None:
        a2a_task_id: str | None = None
        message_id = mapping.message_id_for_task(task_id)
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            expected = TaskState.WORKING if resume else TaskState.SUBMITTED
            if task is None or task.state != expected.value:
                return
            agent = await s.get(Agent, task.assignee_id)
            request = (
                await s.get(Message, task.request_message_id) if task.request_message_id else None
            )
            if agent is None or request is None or not agent.enabled:
                await self._fail_in(s, task, "agent is unavailable or disabled")
                return
            if resume:
                appr = await s.get(Approval, resume)
                if appr is None:
                    return
                answer = appr.note or "approved"
                parts: list[Part] = [{"text": answer}, {"data": {"approved": True}}]
                a2a_task_id = task.a2a_task_id
                message_id = str(uuid.uuid5(uuid.UUID(message_id), str(resume)))
            else:
                # Policy and delegation are re-checked on every call (spec 10).
                verdict = await control.authorize_call(s, task, request)
                if verdict.effect == Effect.DENY:
                    await self._reject_in(s, task, verdict.reason, verdict.decision)
                    return
                if verdict.effect == Effect.REQUIRE_APPROVAL:
                    assert verdict.decision is not None
                    appr = await control.open_policy_approval(
                        s, task, verdict.decision, request.body
                    )
                    await repo.post_message(
                        s,
                        task.room_id,
                        task.requester_id,
                        MessageCreate(
                            kind="approval_request",
                            body=control.approval_request_body(appr, task.id),
                        ),
                        trace_id=task.trace_id,
                    )
                    return
                parts = mapping.parts_from_body(request.body)
            room_id, trace_id = task.room_id, task.trace_id or ""
            try:
                ep = registry.endpoint_for(agent, self._key)
            except (A2AError, secrets.DecryptionError) as exc:
                await self._fail_in(s, task, f"cannot prepare the call: {exc}")
                return
            max_retries = agent.max_retries
            call_span = new_span_id()
            callee = await s.get(Participant, task.assignee_id)
            if trace_id and callee is not None:
                await spans.start(
                    s,
                    org_id=callee.org_id,
                    trace_id=trace_id,
                    span_id=call_span,
                    parent_span_id=request.span_id,
                    kind="a2a_call",
                    name=f"call {callee.display_name}",
                    room_id=room_id,
                    actor_id=task.assignee_id,
                    task_id=task.id,
                    status="pending",
                    attributes={
                        "agent": callee.id,
                        "department": callee.department_id,
                        "a2a_version": agent.a2a_version,
                        "resumed": resume is not None,
                    },
                )
            task.span_id = call_span
            if resume is None:
                await notifications.notify_task(s, task, tmpl.Event.TASK_STARTED)
            await s.commit()

        run = _Run(task_id)
        metadata = {
            "setu": {"room_id": str(room_id), "task_id": str(task_id), "trace_id": trace_id}
        }
        refreshed = False
        attempt = 0
        while True:
            try:
                await validate_agent_url(ep.rpc_url, allow_private=self._allow_private)
                async for event in self._client.stream_message(
                    ep,
                    message_id=message_id,  # same id on every retry: the agent can dedupe
                    context_id=mapping.context_id_for_room(room_id),
                    parts=parts,
                    metadata=metadata,
                    traceparent=format_traceparent(trace_id, call_span) if trace_id else None,
                    task_id=a2a_task_id,
                ):
                    run.got_event = True
                    await self._apply(run, event)
                break
            except UnsafeUrlError as exc:
                await self._fail(task_id, f"blocked unsafe agent address: {exc}")
                return
            except A2AError as exc:
                if exc.version_mismatch and not refreshed and not run.got_event:
                    refreshed = True
                    new_ep = await self._refresh_endpoint(task_id)
                    if new_ep is not None:
                        ep = new_ep
                        continue
                if run.got_event or not exc.retryable or attempt >= max_retries:
                    await self._fail(task_id, str(exc))
                    return
                attempt += 1
                await asyncio.sleep(self._settings.a2a_retry_backoff_seconds * 2 ** (attempt - 1))
        await self._finish(run)

    async def _refresh_endpoint(self, task_id: uuid.UUID) -> AgentEndpoint | None:
        """A card/version mismatch: re-read the card once and retry with what it says now."""
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            agent = await s.get(Agent, task.assignee_id) if task else None
            if agent is None:
                return None
            try:
                await registry.refresh_agent(
                    s,
                    self._client,
                    agent,
                    allow_private=self._allow_private,
                    encryption_key=self._key,
                )
                return registry.endpoint_for(agent, self._key)
            except (registry.RegistrationError, A2AError, secrets.DecryptionError) as exc:
                log.warning("refresh_on_mismatch_failed", error=str(exc))
                return None

    async def _finish(self, run: _Run) -> None:
        """The stream ended. Anything but a terminal or interrupted state is a failure."""
        async with self._sm() as s:
            task = await s.get(Task, run.task_id, with_for_update=True)
            if task is None:
                return
            state = TaskState(task.state)
            if state in TERMINAL_TASK_STATES or state in (
                TaskState.INPUT_REQUIRED,
                TaskState.AUTH_REQUIRED,
            ):
                return
            await self._fail_in(s, task, "the agent ended the stream before finishing the task")

    # ------------------------------------------------------------ events -> room

    async def _apply(self, run: _Run, event: Event) -> None:
        async with self._sm() as s:
            task = await s.get(Task, run.task_id, with_for_update=True)
            if task is None or TaskState(task.state) in TERMINAL_TASK_STATES:
                return  # e.g. canceled locally while events were in flight
            if isinstance(event, TaskSnapshot):
                task.a2a_task_id = event.task_id
                task.a2a_context_id = event.context_id
                for art in event.artifacts:
                    await self._store_artifact(s, task, art)
                await self._move(s, run, task, TaskState(event.state), event.text)
            elif isinstance(event, StatusEvent):
                if event.task_id and not task.a2a_task_id:
                    task.a2a_task_id = event.task_id
                await self._move(s, run, task, TaskState(event.state), event.text)
            elif isinstance(event, ArtifactEvent):
                await self._store_artifact(s, task, event)
                if event.last_chunk:
                    await self._post_result(s, run, task, event.artifact_id)
                else:
                    await s.commit()
            elif isinstance(event, MessageEvent):
                reply = ArtifactEvent(None, event.context_id, "reply", "reply", event.parts, True)
                await self._store_artifact(s, task, reply)
                await self._post_result(s, run, task, "reply")
                apply_state(task, TaskState.COMPLETED)
                await notifications.notify_task(s, task, tmpl.Event.TASK_COMPLETED)
                await self._post(
                    s,
                    task,
                    "task_update",
                    mapping.status_body(task.id, "completed", text_of(event.parts) or None),
                )

    async def _move(
        self, s: AsyncSession, run: _Run, task: Task, target: TaskState, text: str | None
    ) -> None:
        if target == TaskState.SUBMITTED:  # the agent acknowledging receipt; nothing to show yet
            await s.commit()
            return
        if not apply_state(task, target):
            log.warning("ignored_illegal_transition", task=str(task.id), to=target.value)
            await s.commit()
            return
        if target in TERMINAL_TASK_STATES:  # flush chunked artifacts before the final message
            for art_id in await self._unposted_artifacts(s, run, task):
                await self._post_result(s, run, task, art_id)
        if target == TaskState.INPUT_REQUIRED:
            prompt = text or "The agent needs more input."
            appr = await control.open_agent_approval(s, task, prompt)
            await notifications.notify_task(
                s, task, tmpl.Event.TASK_INPUT_REQUIRED, prompt, approval=appr
            )
            await self._post(
                s,
                task,
                "approval_request",
                {
                    "task_id": str(task.id),
                    "prompt": prompt,
                    **({"approval_id": str(appr.id)} if appr else {}),
                },
            )
        else:
            event = tmpl.event_for_state(target.value)
            if event is not None:
                await notifications.notify_task(s, task, event, text or "")
            await self._post(
                s,
                task,
                "task_update",
                mapping.status_body(task.id, target.value, text),
            )

    async def _store_artifact(self, s: AsyncSession, task: Task, art: ArtifactEvent) -> None:
        row = (
            await s.execute(
                select(TaskArtifact).where(
                    TaskArtifact.task_id == task.id, TaskArtifact.a2a_artifact_id == art.artifact_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(
                TaskArtifact(
                    task_id=task.id,
                    a2a_artifact_id=art.artifact_id,
                    name=art.name,
                    parts=list(art.parts),
                )
            )
        elif art.append:
            row.parts = [*row.parts, *art.parts]
        else:
            row.parts = list(art.parts)
            row.name = art.name or row.name
        await s.flush()

    async def _unposted_artifacts(self, s: AsyncSession, run: _Run, task: Task) -> list[str]:
        ids = (
            await s.execute(
                select(TaskArtifact.a2a_artifact_id).where(TaskArtifact.task_id == task.id)
            )
        ).scalars()
        return [a for a in ids if a not in run.posted]

    async def _post_result(self, s: AsyncSession, run: _Run, task: Task, artifact_id: str) -> None:
        if artifact_id in run.posted:
            await s.commit()
            return
        row = (
            await s.execute(
                select(TaskArtifact).where(
                    TaskArtifact.task_id == task.id, TaskArtifact.a2a_artifact_id == artifact_id
                )
            )
        ).scalar_one()
        run.posted.add(artifact_id)
        art = ArtifactEvent(None, None, artifact_id, row.name, tuple(row.parts), True)
        body = mapping.artifact_body(task.id, art, list(row.parts))
        # Setu attests to what the agent returned (core/signing.py, decision 125).
        body["signature"] = self._signer.sign_artifact(
            task_id=str(task.id),
            room_id=str(task.room_id),
            artifact_id=artifact_id,
            agent_id=str(task.assignee_id),
            trace_id=task.trace_id,
            parts=list(row.parts),
        )
        await self._post(s, task, "result", body)

    async def _post(
        self, s: AsyncSession, task: Task, kind: MessageKind, body: dict[str, object]
    ) -> None:
        """Post as the assignee agent, addressed to the requester. Commits the session, so the
        task change and its message land together."""
        await repo.post_message(
            s,
            task.room_id,
            task.assignee_id,
            MessageCreate(kind=kind, body=body, mentions=[task.requester_id], task_id=task.id),
            trace_id=task.trace_id,
        )

    # ------------------------------------------------------------ failure

    async def _reject_in(
        self, s: AsyncSession, task: Task, reason: str, decision: object | None
    ) -> None:
        """Policy or delegation now says no: end the task `rejected`, visibly and audited."""
        reason = scrub(reason)[:500]
        if not apply_state(task, TaskState.REJECTED, error=reason):
            await s.commit()
            return
        requester = await s.get(Participant, task.requester_id)
        request = await s.get(Message, task.request_message_id) if task.request_message_id else None
        if requester is not None and task.trace_id:
            await spans.start(
                s,
                org_id=requester.org_id,
                trace_id=task.trace_id,
                kind="policy",
                name="policy deny (at call)",
                parent_span_id=request.span_id if request else None,
                room_id=task.room_id,
                actor_id=task.requester_id,
                task_id=task.id,
                status="denied",
                ended=True,
                attributes={"decision": "deny", "reason": reason, "at": "dispatch"},
            )
        if requester is not None:
            await audit.append(
                s,
                requester.org_id,
                "policy.deny",
                task.requester_id,
                {"task_id": task.id, "reason": reason, "at": "dispatch"},
            )
        await notifications.notify_task(s, task, tmpl.Event.TASK_REJECTED, reason)
        await self._post(
            s,
            task,
            "task_update",
            mapping.status_body(task.id, "rejected", None, origin="hub", error=reason),
        )

    async def _fail(self, task_id: uuid.UUID, reason: str) -> None:
        async with self._sm() as s:
            task = await s.get(Task, task_id, with_for_update=True)
            if task is not None:
                await self._fail_in(s, task, reason)

    async def _fail_in(self, s: AsyncSession, task: Task, reason: str) -> None:
        reason = scrub(reason)[:500]
        if not apply_state(task, TaskState.FAILED, error=reason):
            await s.commit()
            return
        await notifications.notify_task(s, task, tmpl.Event.TASK_FAILED, reason)
        await self._post(
            s,
            task,
            "task_update",
            mapping.status_body(task.id, "failed", None, origin="hub", error=reason),
        )
