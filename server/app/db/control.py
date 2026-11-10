"""The control plane on the write path: policy, delegation, approvals (spec sections 8.4, 10).

`authorize_send` runs inside `post_message` before anything is stored. `authorize_call` runs in
the dispatcher right before an A2A call. Both use the pure engines in `app.core`; this module
only gathers the facts (departments, rules, delegation chain) and records the outcome in the
audit log.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import delegation as delegation_rules
from app.core.notifications import Event
from app.core.policy import Decision, Effect, PolicyContext, evaluate
from app.db import audit, notifications, spans
from app.db.delegations import as_grants, chain_of
from app.db.messages_util import message_skills
from app.db.models import Approval, Message, Participant, Task
from app.db.policies import load_rules

# Only messages a participant initiates are checked. Replies the hub posts for a task that was
# already authorized (updates, results, approvals, activity) are exempt.
CHECKED_KINDS = frozenset({"text", "task"})


class ControlDenied(Exception):
    """The send was refused. `reason` is safe to show to the sender."""

    def __init__(self, reason: str, rule_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


@dataclass(frozen=True)
class TraceRef:
    """A send's place in its trace: its pre-allocated message span and that span's parent."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None


@dataclass
class SendCheck:
    gates: dict[uuid.UUID, Decision] = field(default_factory=dict)  # assignee -> approval needed
    delegation_id: uuid.UUID | None = None


async def _people(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Participant]:
    rows = await session.execute(select(Participant).where(Participant.id.in_(set(ids))))
    return {p.id: p for p in rows.scalars()}


def _ctx(
    sender: Participant,
    recipient: Participant,
    room_id: uuid.UUID,
    kind: str,
    skills: Sequence[str],
    chain: Sequence[uuid.UUID],
) -> PolicyContext:
    return PolicyContext(
        sender_id=sender.id,
        recipient_id=recipient.id,
        sender_org=sender.org_id,
        recipient_org=recipient.org_id,
        sender_department=sender.department_id,
        recipient_department=recipient.department_id,
        room_id=room_id,
        kind=kind,
        skills=tuple(skills),
        delegation_chain=tuple(chain),
        now=datetime.now(UTC),
    )


async def check_delegation(
    session: AsyncSession,
    requester: Participant,
    delegation_id: uuid.UUID | None,
    skills: Sequence[str],
) -> str | None:
    """Spec 8.4 step 3. Humans are the root of authority; agents must hold a covering grant."""
    if requester.kind == "human":
        return None
    if delegation_id is None:
        return "an agent must act under a delegation (delegation_id)"
    chain = await chain_of(session, delegation_id)
    return delegation_rules.check_chain(
        as_grants(chain),
        requester.id,
        delegation_rules.required_scopes(skills),
        datetime.now(UTC),
    )


def _audit_payload(
    d: Decision, sender: Participant, recipient: Participant, room_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    return {
        "sender": sender.id,
        "recipient": recipient.id,
        "sender_department": sender.department_id,
        "recipient_department": recipient.department_id,
        "room_id": room_id,
        "reason": d.reason,
        "rule_id": d.rule_id,
        **extra,
    }


async def authorize_send(
    session: AsyncSession,
    room_id: uuid.UUID,
    sender_id: uuid.UUID,
    kind: str,
    body: dict[str, Any],
    addressed: Sequence[uuid.UUID],
    delegation_id: uuid.UUID | None,
    trace: TraceRef,
) -> SendCheck:
    """Raises ControlDenied (after committing the audit event) or returns what to gate.

    Writes `delegation` and `policy` spans under the message span. On a denial it also writes
    the (denied) message span, since no message will exist to write it."""
    check = SendCheck()
    if kind not in CHECKED_KINDS or not addressed:
        return check
    people = await _people(session, [sender_id, *addressed])
    sender = people[sender_id]
    skills = message_skills(body)

    async def span(kind_: str, name: str, status: str, attrs: dict[str, Any]) -> None:
        await spans.start(
            session,
            org_id=sender.org_id,
            trace_id=trace.trace_id,
            kind=kind_,
            name=name,
            parent_span_id=trace.span_id,
            room_id=room_id,
            actor_id=sender.id,
            status=status,
            ended=True,
            attributes=attrs,
        )

    async def denied_root(reason: str) -> None:
        await spans.start(
            session,
            org_id=sender.org_id,
            trace_id=trace.trace_id,
            span_id=trace.span_id,
            parent_span_id=trace.parent_span_id,
            kind="message",
            name=f"{kind} denied",
            room_id=room_id,
            actor_id=sender.id,
            status="denied",
            ended=True,
            attributes={"kind": kind, "skills": skills, "reason": reason},
        )

    if kind == "task":
        reason = await check_delegation(session, sender, delegation_id, skills)
        if sender.kind == "agent":
            grants = await chain_of(session, delegation_id) if delegation_id else []
            await span(
                "delegation",
                "delegation check",
                "ok" if reason is None else "denied",
                {
                    "delegation_id": delegation_id,
                    "chain": [d.id for d in grants],  # leaf first
                    "root_human": grants[-1].root_human_id if grants else None,
                    "scopes": grants[0].scopes if grants else [],
                    "needed": delegation_rules.required_scopes(skills),
                    "reason": reason,
                },
            )
        if reason is not None:
            await denied_root(reason)
            await notifications.notify_policy_denied(
                session,
                sender,
                people[addressed[0]],
                room_id=room_id,
                skills=skills,
                reason=reason,
                trace_id=trace.trace_id,
            )
            await audit.append(
                session,
                sender.org_id,
                "delegation.denied",
                sender.id,
                {"room_id": room_id, "delegation_id": delegation_id, "reason": reason},
            )
            await session.commit()
            raise ControlDenied(reason)
        if sender.kind == "agent":
            check.delegation_id = delegation_id
    chain = [check.delegation_id] if check.delegation_id else []
    rules = await load_rules(session, sender.org_id)
    for rid in addressed:
        recipient = people[rid]
        d = evaluate(_ctx(sender, recipient, room_id, kind, skills, chain), rules)
        if kind == "task" or d.effect == Effect.DENY:
            await span(
                "policy",
                f"policy {d.effect.value}",
                "denied" if d.effect == Effect.DENY else "ok",
                {
                    "decision": d.effect.value,
                    "reason": d.reason,
                    "rule_id": d.rule_id,
                    "from_department": sender.department_id,
                    "to_department": recipient.department_id,
                    "recipient": recipient.id,
                },
            )
        if d.effect == Effect.DENY:
            await denied_root(d.reason)
            await notifications.notify_policy_denied(
                session,
                sender,
                recipient,
                room_id=room_id,
                skills=skills,
                reason=d.reason,
                trace_id=trace.trace_id,
            )
            await audit.append(
                session,
                sender.org_id,
                "policy.deny",
                sender.id,
                _audit_payload(d, sender, recipient, room_id, kind=kind, skills=skills),
            )
            await session.commit()
            raise ControlDenied(d.reason, d.rule_id)
        if kind != "task":
            continue
        event = "policy.require_approval" if d.effect == Effect.REQUIRE_APPROVAL else "policy.allow"
        await audit.append(
            session,
            sender.org_id,
            event,
            sender.id,
            _audit_payload(d, sender, recipient, room_id, skills=skills),
        )
        if d.effect == Effect.REQUIRE_APPROVAL:
            check.gates[rid] = d
    return check


# ------------------------------------------------------------------ approvals


async def _approval_span(
    session: AsyncSession, appr: Approval, task: Task, org_id: uuid.UUID, parent: str | None
) -> None:
    """Open the `approval` span; it is closed when a human decides."""
    if not task.trace_id:
        return
    span = await spans.start(
        session,
        org_id=org_id,
        trace_id=task.trace_id,
        kind="approval",
        name=f"{appr.kind} approval",
        parent_span_id=parent,
        room_id=task.room_id,
        actor_id=task.requester_id,
        task_id=task.id,
        status="pending",
        attributes={
            "approval_id": appr.id,
            "approver_department": appr.approver_department_id,
            "approver_role": appr.approver_role,
            "policy_id": appr.policy_id,
        },
    )
    appr.span_id = span.id


def _summary(requester: Participant, assignee: Participant, body: Mapping[str, Any]) -> str:
    skills = message_skills(dict(body))
    what = ", ".join(skills) if skills else "a task"
    text = body.get("text")
    tail = f": {text[:200]}" if isinstance(text, str) and text else ""
    return f"{requester.display_name} asks {assignee.display_name} for {what}{tail}"


async def open_policy_approval(
    session: AsyncSession, task: Task, decision: Decision, request_body: Mapping[str, Any]
) -> Approval:
    """Hold `task` until a human of the approver department decides. The caller commits."""
    assert decision.approver_department is not None and decision.approver_role is not None
    people = await _people(session, [task.requester_id, task.assignee_id])
    requester, assignee = people[task.requester_id], people[task.assignee_id]
    task.state = "input_required"
    task.claimed_at = None
    appr = Approval(
        task_id=task.id,
        kind="policy",
        policy_id=decision.rule_id,
        approver_department_id=decision.approver_department,
        approver_role=decision.approver_role,
        summary=_summary(requester, assignee, request_body),
    )
    session.add(appr)
    await session.flush()
    request = (
        await session.get(Message, task.request_message_id) if task.request_message_id else None
    )
    await _approval_span(
        session, appr, task, requester.org_id, request.span_id if request else None
    )
    await notifications.notify_approval(session, appr, task, Event.APPROVAL_REQUESTED)
    await audit.append(
        session,
        requester.org_id,
        "approval.requested",
        requester.id,
        {
            "approval_id": appr.id,
            "task_id": task.id,
            "kind": "policy",
            "policy_id": decision.rule_id,
            "approver_department": decision.approver_department,
            "approver_role": decision.approver_role,
        },
    )
    return appr


async def open_agent_approval(session: AsyncSession, task: Task, prompt: str) -> Approval | None:
    """The agent itself asked for input. The requester answers (a human), or a department admin
    of the requester's department when the requester is an agent. The caller commits."""
    existing = (
        await session.execute(
            select(Approval.id).where(
                Approval.task_id == task.id, Approval.kind == "agent", Approval.state == "pending"
            )
        )
    ).first()
    if existing:
        return None
    requester = (await _people(session, [task.requester_id]))[task.requester_id]
    human = requester.kind == "human"
    appr = Approval(
        task_id=task.id,
        kind="agent",
        approver_department_id=requester.department_id,
        approver_role="dept_member" if human else "dept_admin",
        approver_id=requester.id if human else None,
        summary=prompt[:500],
    )
    session.add(appr)
    await session.flush()
    await _approval_span(session, appr, task, requester.org_id, task.span_id)
    await audit.append(
        session,
        requester.org_id,
        "approval.requested",
        task.assignee_id,
        {"approval_id": appr.id, "task_id": task.id, "kind": "agent"},
    )
    return appr


def approval_request_body(appr: Approval, task_id: uuid.UUID) -> dict[str, Any]:
    return {
        "approval_id": str(appr.id),
        "task_id": str(task_id),
        "origin": "hub",
        "prompt": f"Approval required: {appr.summary}",
        "approver_department_id": str(appr.approver_department_id),
        "approver_role": appr.approver_role,
    }


# ------------------------------------------------------------------ at call time


@dataclass(frozen=True)
class CallVerdict:
    effect: Effect
    reason: str
    decision: Decision | None = None


async def authorize_call(session: AsyncSession, task: Task, request: Message) -> CallVerdict:
    """Re-checked right before the A2A call: rules and delegations can change after the send."""
    people = await _people(session, [task.requester_id, task.assignee_id])
    requester, assignee = people[task.requester_id], people[task.assignee_id]
    skills = message_skills(request.body)
    reason = await check_delegation(session, requester, task.delegation_chain_id, skills)
    if reason is not None:
        return CallVerdict(Effect.DENY, reason)
    chain = [task.delegation_chain_id] if task.delegation_chain_id else []
    rules = await load_rules(session, requester.org_id)
    d = evaluate(_ctx(requester, assignee, task.room_id, "task", skills, chain), rules)
    if d.effect == Effect.REQUIRE_APPROVAL:
        approved = (
            await session.execute(
                select(Approval.id).where(
                    Approval.task_id == task.id,
                    Approval.kind == "policy",
                    Approval.state == "approved",
                    Approval.policy_id == d.rule_id,
                )
            )
        ).first()
        if approved:
            return CallVerdict(Effect.ALLOW, "approved by a human", d)
    return CallVerdict(d.effect, d.reason, d)
