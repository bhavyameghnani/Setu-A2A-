"""Read-only activity numbers for the Analytics page, computed from the tasks table."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import aliased

from app.api.deps import CurrentPrincipal, Session
from app.db.models import Participant, Task
from app.schemas import AgentActivity, AnalyticsSummary, DayCount

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])

_FINISHED_SECONDS = func.extract("epoch", Task.completed_at - Task.created_at)


@router.get("/summary", response_model=AnalyticsSummary)
async def summary(
    principal: CurrentPrincipal,
    session: Session,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> AnalyticsSummary:
    """Org admins and auditors see every task in the org; everyone else sees the tasks their own
    department asked for or ran (a task shows up for both departments it connects)."""
    since = datetime.now(UTC) - timedelta(days=days)
    requester, assignee = aliased(Participant), aliased(Participant)
    base = (
        select(Task)
        .join(requester, requester.id == Task.requester_id)
        .join(assignee, assignee.id == Task.assignee_id)
        .where(requester.org_id == principal.org_id, Task.created_at >= since)
    )
    oversight = principal.is_human and bool({"org_admin", "auditor"} & principal.roles)
    if not oversight:
        base = base.where(
            (requester.department_id == principal.department_id)
            | (assignee.department_id == principal.department_id)
        )
    ids = base.with_only_columns(Task.id).subquery()

    states = dict(
        (
            await session.execute(
                select(Task.state, func.count())
                .where(Task.id.in_(select(ids.c.id)))
                .group_by(Task.state)
            )
        ).all()
    )
    avg = (
        await session.execute(
            select(func.avg(_FINISHED_SECONDS)).where(
                Task.id.in_(select(ids.c.id)),
                Task.state == "completed",
                Task.completed_at.is_not(None),
            )
        )
    ).scalar_one()

    day_col = cast(func.timezone("UTC", Task.created_at), Date)
    by_day: dict[date, dict[str, int]] = {}
    for d, state, n in (
        await session.execute(
            select(day_col, Task.state, func.count())
            .where(Task.id.in_(select(ids.c.id)))
            .group_by(day_col, Task.state)
        )
    ).all():
        slot = by_day.setdefault(d, {"created": 0, "completed": 0, "failed": 0})
        slot["created"] += n
        if state in ("completed", "failed"):
            slot[state] += n
    today = datetime.now(UTC).date()
    per_day = [
        DayCount(
            day=(d := today - timedelta(days=i)).isoformat(),
            **by_day.get(d, {"created": 0, "completed": 0, "failed": 0}),
        )
        for i in range(days - 1, -1, -1)
    ]

    agents = (
        await session.execute(
            select(
                Task.assignee_id,
                func.count(),
                func.count().filter(Task.state == "completed"),
                func.count().filter(Task.state == "failed"),
                func.avg(_FINISHED_SECONDS).filter(
                    Task.state == "completed", Task.completed_at.is_not(None)
                ),
            )
            .where(Task.id.in_(select(ids.c.id)))
            .group_by(Task.assignee_id)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    completed, failed = states.get("completed", 0), states.get("failed", 0)
    return AnalyticsSummary(
        days=days,
        scope="org" if oversight else "department",
        total=sum(states.values()),
        active=sum(
            states.get(s, 0) for s in ("submitted", "working", "input_required", "auth_required")
        ),
        completed=completed,
        failed=failed,
        canceled=states.get("canceled", 0),
        rejected=states.get("rejected", 0),
        success_rate=completed / (completed + failed) if completed + failed else None,
        avg_seconds=float(avg) if avg is not None else None,
        per_day=per_day,
        by_agent=[
            AgentActivity(
                participant_id=a,
                tasks=n,
                completed=c,
                failed=f,
                avg_seconds=float(s) if s is not None else None,
            )
            for a, n, c, f, s in agents
        ],
    )
