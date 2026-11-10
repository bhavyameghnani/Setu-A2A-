import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import OrgAdminPrincipal, OversightPrincipal, Session
from app.core.policy import ANY, RuleError, parse_rule
from app.db import audit
from app.db.models import Department, Policy
from app.schemas import PolicyCreate, PolicyOut, PolicyUpdate

router = APIRouter(prefix="/v1/policies", tags=["policies"])


async def _validate(session: Session, org_id: uuid.UUID, rule: dict[str, object]) -> None:
    try:
        parsed = parse_rule(rule)
    except RuleError as exc:
        raise HTTPException(422, str(exc)) from None
    named = {
        d
        for d in (parsed.from_department, parsed.to_department, parsed.approver_department)
        if d and d != ANY
    }
    if named:
        found = (
            await session.execute(
                select(Department.id).where(
                    Department.org_id == org_id, Department.id.in_([uuid.UUID(d) for d in named])
                )
            )
        ).scalars()
        if len(set(found)) != len(named):
            raise HTTPException(422, "rule names a department that is not in your organization")


async def _load(session: Session, org_id: uuid.UUID, policy_id: uuid.UUID) -> Policy:
    p = await session.get(Policy, policy_id)
    if p is None or p.org_id != org_id:
        raise HTTPException(404, "policy not found")
    return p


@router.get("", response_model=list[PolicyOut])
async def list_policies(principal: OversightPrincipal, session: Session) -> Sequence[Policy]:
    rows = await session.execute(
        select(Policy).where(Policy.org_id == principal.org_id).order_by(Policy.created_at)
    )
    return rows.scalars().all()


@router.post("", response_model=PolicyOut, status_code=201)
async def create_policy(
    body: PolicyCreate, principal: OrgAdminPrincipal, session: Session
) -> Policy:
    await _validate(session, principal.org_id, body.rule)
    p = Policy(
        org_id=principal.org_id,
        name=body.name,
        rule=body.rule,
        enabled=body.enabled,
        created_by=principal.participant_id,
    )
    session.add(p)
    await session.flush()
    await audit.append(
        session,
        principal.org_id,
        "policy.created",
        principal.participant_id,
        {"policy_id": p.id, "name": p.name, "rule": p.rule, "enabled": p.enabled},
    )
    await session.commit()
    await session.refresh(p)
    return p


@router.put("/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID, body: PolicyUpdate, principal: OrgAdminPrincipal, session: Session
) -> Policy:
    p = await _load(session, principal.org_id, policy_id)
    if body.rule is not None:
        await _validate(session, principal.org_id, body.rule)
        p.rule = body.rule
    if body.name is not None:
        p.name = body.name
    if body.enabled is not None:
        p.enabled = body.enabled
    await audit.append(
        session,
        principal.org_id,
        "policy.updated",
        principal.participant_id,
        {"policy_id": p.id, "name": p.name, "rule": p.rule, "enabled": p.enabled},
    )
    await session.commit()
    await session.refresh(p)
    return p


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: uuid.UUID, principal: OrgAdminPrincipal, session: Session
) -> None:
    p = await _load(session, principal.org_id, policy_id)
    await audit.append(
        session,
        principal.org_id,
        "policy.deleted",
        principal.participant_id,
        {"policy_id": p.id, "name": p.name, "rule": p.rule},
    )
    await session.delete(p)
    await session.commit()
