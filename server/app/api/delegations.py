import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import or_, select

from app.api.deps import ReadPrincipal, SendPrincipal, Session
from app.core.auth import Principal
from app.db import audit
from app.db import delegations as repo
from app.db.models import Delegation, Participant
from app.schemas import DelegationCreate, DelegationOut

router = APIRouter(prefix="/v1/delegations", tags=["delegations"])


def _oversight(p: Principal) -> bool:
    return p.is_human and bool({"org_admin", "auditor"} & p.roles)


@router.post("", response_model=DelegationOut, status_code=201)
async def create_delegation(
    body: DelegationCreate, principal: SendPrincipal, session: Session
) -> Delegation:
    """A human grants scopes to an agent; an agent passes part of what it holds to another
    agent (naming `parent_id`). Grants only narrow: scopes and expiry never exceed the parent's."""
    grantor = await session.get(Participant, principal.participant_id)
    to = await session.get(Participant, body.to_participant_id)
    if grantor is None or to is None or to.org_id != principal.org_id:
        raise HTTPException(404, "participant not found")
    try:
        row = await repo.create(
            session,
            grantor=grantor,
            to=to,
            scopes=body.scopes,
            expires_at=body.expires_at,
            parent_id=body.parent_id,
        )
    except repo.DelegationError as exc:
        await session.rollback()
        raise HTTPException(exc.status, str(exc)) from None
    await session.commit()
    await session.refresh(row)
    return row


@router.get("", response_model=list[DelegationOut])
async def list_delegations(principal: ReadPrincipal, session: Session) -> Sequence[Delegation]:
    q = select(Delegation).order_by(Delegation.created_at.desc()).limit(200)
    if not _oversight(principal):
        me = principal.participant_id
        q = q.where(
            or_(
                Delegation.from_participant_id == me,
                Delegation.to_participant_id == me,
                Delegation.root_human_id == me,
            )
        )
    else:
        q = q.join(Participant, Participant.id == Delegation.to_participant_id).where(
            Participant.org_id == principal.org_id
        )
    return (await session.execute(q)).scalars().all()


async def _visible_chain(
    session: Session, principal: Principal, delegation_id: uuid.UUID
) -> list[Delegation]:
    chain = await repo.chain_of(session, delegation_id)
    if chain:
        leaf_to = await session.get(Participant, chain[0].to_participant_id)
        involved = {
            pid
            for d in chain
            for pid in (d.from_participant_id, d.to_participant_id, d.root_human_id)
        }
        in_org = leaf_to is not None and leaf_to.org_id == principal.org_id
        if in_org and (principal.participant_id in involved or _oversight(principal)):
            return chain
    raise HTTPException(404, "delegation not found")


@router.get("/{delegation_id}/chain", response_model=list[DelegationOut])
async def delegation_chain(
    delegation_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> list[Delegation]:
    """Path from this grant back to the root human: leaf first, root last."""
    return await _visible_chain(session, principal, delegation_id)


@router.post("/{delegation_id}/revoke", response_model=DelegationOut)
async def revoke_delegation(
    delegation_id: uuid.UUID, principal: SendPrincipal, session: Session
) -> Delegation:
    """Revoking a grant also ends everything delegated from it (the chain check fails)."""
    chain = await _visible_chain(session, principal, delegation_id)
    d = chain[0]
    if principal.participant_id not in (d.from_participant_id, d.root_human_id) and not (
        principal.is_org_admin
    ):
        raise HTTPException(403, "only the grantor, the root human or an org admin can revoke")
    if d.revoked_at is None:
        d.revoked_at = datetime.now(UTC)
        await audit.append(
            session,
            principal.org_id,
            "delegation.revoked",
            principal.participant_id,
            {"delegation_id": d.id, "to": d.to_participant_id},
        )
    await session.commit()
    await session.refresh(d)
    return d
