import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth
from app.core.auth import AuthError, Principal
from app.db.models import ApiKey, Human, Participant

_LAST_USED_RESOLUTION = timedelta(minutes=1)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def _load_principal(
    session: AsyncSession, participant_id: uuid.UUID, scopes: frozenset[str] | None
) -> Principal:
    p = await session.get(Participant, participant_id)
    if p is None or p.status != "active":
        raise AuthError("unknown or inactive participant")
    human = await session.get(Human, participant_id)
    return Principal(
        participant_id=p.id,
        org_id=p.org_id,
        department_id=p.department_id,
        kind=p.kind,
        roles=frozenset(human.roles if human else ()),
        scopes=scopes,
    )


async def _principal_from_api_key(session: AsyncSession, key: str) -> Principal:
    prefix = auth.parse_api_key(key)
    row = None
    if prefix is not None:
        row = (
            await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        ).scalar_one_or_none()
    if row is None:
        auth.verify_unknown_user()  # keep timing similar for unknown prefixes
        raise AuthError("invalid api key")
    if not auth.verify_secret(row.key_hash, key) or row.revoked_at is not None:
        raise AuthError("invalid api key")
    now = datetime.now(UTC)
    if row.last_used_at is None or now - row.last_used_at > _LAST_USED_RESOLUTION:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
        await session.commit()
    return await _load_principal(session, row.participant_id, frozenset(row.scopes))


async def resolve_principal(
    session: AsyncSession, jwt_secret: str, bearer: str | None, api_key: str | None
) -> Principal:
    """Shared by REST and WebSocket. Accepts a JWT or an API key (as bearer or X-API-Key)."""
    if api_key is None and bearer is not None and auth.looks_like_api_key(bearer):
        api_key, bearer = bearer, None
    if api_key is not None:
        return await _principal_from_api_key(session, api_key)
    if bearer is not None:
        pid = auth.decode_access_token(jwt_secret, bearer)
        return await _load_principal(session, pid, None)
    raise AuthError("missing credentials")


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def current_principal(
    request: Request,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    try:
        return await resolve_principal(
            session, request.app.state.jwt_secret, _bearer(authorization), x_api_key
        )
    except AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_scope(scope: str) -> Callable[[Principal], Awaitable[Principal]]:
    async def dep(principal: CurrentPrincipal) -> Principal:
        if not principal.has_scope(scope):
            raise HTTPException(403, f"api key lacks scope {scope}")
        return principal

    return dep


ReadPrincipal = Annotated[Principal, Depends(require_scope("rooms:read"))]
WritePrincipal = Annotated[Principal, Depends(require_scope("rooms:write"))]
SendPrincipal = Annotated[Principal, Depends(require_scope("messages:send"))]


def require_admin(principal: CurrentPrincipal) -> Principal:
    if not principal.is_human or not ({"org_admin", "dept_admin"} & principal.roles):
        raise HTTPException(403, "admin role required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def require_oversight(principal: CurrentPrincipal) -> Principal:
    """Audit log and policies: org admins and auditors (humans only)."""
    if not principal.is_human or not ({"org_admin", "auditor"} & principal.roles):
        raise HTTPException(403, "org_admin or auditor role required")
    return principal


OversightPrincipal = Annotated[Principal, Depends(require_oversight)]


def require_org_admin(principal: CurrentPrincipal) -> Principal:
    if not principal.is_human or not principal.is_org_admin:
        raise HTTPException(403, "org_admin role required")
    return principal


OrgAdminPrincipal = Annotated[Principal, Depends(require_org_admin)]
