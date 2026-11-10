"""Delegation grants: creation under the narrowing rules, and walking the chain to the root."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import delegation as rules
from app.db import audit
from app.db.models import Delegation, Participant


class DelegationError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


async def chain_of(session: AsyncSession, leaf_id: uuid.UUID) -> list[Delegation]:
    """The grant and its ancestors, leaf first, root last. Empty if the leaf does not exist."""
    out: list[Delegation] = []
    current: uuid.UUID | None = leaf_id
    while current is not None and len(out) < 32:  # depth cap guards against bad data
        row = await session.get(Delegation, current)
        if row is None:
            break
        out.append(row)
        current = row.parent_id
    return out


def as_grants(chain: Sequence[Delegation]) -> list[rules.Grant]:
    return [
        rules.Grant(d.id, d.to_participant_id, tuple(d.scopes), d.expires_at, d.revoked_at)
        for d in chain
    ]


async def create(
    session: AsyncSession,
    *,
    grantor: Participant,
    to: Participant,
    scopes: Sequence[str],
    expires_at: datetime | None,
    parent_id: uuid.UUID | None,
) -> Delegation:
    """A human (no parent) or an agent holding `parent_id` grants `scopes` to `to`.

    The caller commits. Raises DelegationError when the grant would exceed the parent.
    """
    try:
        wanted = rules.validate_scopes(scopes)
    except rules.ScopeError as exc:
        raise DelegationError(str(exc)) from None
    now = datetime.now(UTC)
    if expires_at is not None and expires_at <= now:
        raise DelegationError("expires_at must be in the future")
    if to.org_id != grantor.org_id or to.kind != "agent":
        raise DelegationError("scopes can only be delegated to an agent in your organization", 404)
    if parent_id is None:
        if grantor.kind != "human":
            raise DelegationError("an agent must name the delegation it is passing on", 403)
        root_human = grantor.id
    else:
        chain = await chain_of(session, parent_id)
        reason = rules.check_chain(as_grants(chain), grantor.id, [], now) if chain else "not found"
        if reason is not None:
            raise DelegationError(f"cannot sub-delegate: {reason}", 403)
        parent = chain[0]
        if not rules.is_subset(wanted, parent.scopes):
            raise DelegationError("scopes exceed what the parent delegation holds", 403)
        if not rules.expiry_ok(expires_at, parent.expires_at):
            raise DelegationError("the delegation may not outlive its parent", 403)
        root_human = parent.root_human_id
    row = Delegation(
        root_human_id=root_human,
        from_participant_id=grantor.id,
        to_participant_id=to.id,
        scopes=list(wanted),
        parent_id=parent_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await audit.append(
        session,
        grantor.org_id,
        "delegation.created",
        grantor.id,
        {
            "delegation_id": row.id,
            "to": to.id,
            "scopes": list(wanted),
            "parent_id": parent_id,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    return row
