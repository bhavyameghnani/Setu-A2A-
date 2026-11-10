"""Loading an org's policy rules for the pure engine."""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy import PolicyRule, RuleError, parse_rule
from app.db.models import Policy

log = structlog.get_logger()


async def load_rules(session: AsyncSession, org_id: uuid.UUID) -> list[PolicyRule]:
    rows = await session.execute(
        select(Policy)
        .where(Policy.org_id == org_id, Policy.enabled.is_(True))
        .order_by(Policy.created_at, Policy.id)
    )
    out: list[PolicyRule] = []
    for p in rows.scalars():
        try:
            out.append(PolicyRule(p.id, parse_rule(p.rule)))
        except RuleError as exc:  # stored before validation tightened; skip, never crash a send
            log.warning("invalid_policy_skipped", policy=str(p.id), error=str(exc))
    return out
