from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, Session
from app.core import auth
from app.db.models import Human, Participant
from app.schemas import LoginRequest, LoginResponse, MeOut, ParticipantOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, session: Session, request: Request) -> LoginResponse:
    row = (
        await session.execute(
            select(Human, Participant)
            .join(Participant, Participant.id == Human.participant_id)
            .where(Human.email == body.email.strip().lower())
        )
    ).one_or_none()
    if row is None:
        auth.verify_unknown_user()
        raise HTTPException(401, "invalid email or password")
    human, participant = row
    if not auth.verify_secret(human.password_hash, body.password) or participant.status != "active":
        raise HTTPException(401, "invalid email or password")
    settings = request.app.state.settings
    ttl = timedelta(minutes=settings.jwt_ttl_minutes)
    token = auth.create_access_token(request.app.state.jwt_secret, participant.id, ttl)
    return LoginResponse(
        access_token=token,
        expires_in=int(ttl.total_seconds()),
        participant=ParticipantOut.model_validate(participant),
    )


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentPrincipal, session: Session) -> MeOut:
    participant = await session.get(Participant, principal.participant_id)
    assert participant is not None  # resolved moments ago by current_principal
    return MeOut(
        participant=ParticipantOut.model_validate(participant),
        roles=sorted(principal.roles),
        scopes=None if principal.scopes is None else sorted(principal.scopes),
    )
