"""Teams: named groups of people and agents, owned by a department.

A team is only a grouping (so a whole team can be added to a room in one click). Being in a team
grants no room access and no delegation; everything those need still goes through their own
checks. Anyone in the org may read teams; the admins of the owning department (and org admins)
change them.
"""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminPrincipal, CurrentPrincipal, Session
from app.core.auth import Principal
from app.db.models import Department, Participant, Team, TeamMember
from app.schemas import TeamCreate, TeamMemberAdd, TeamOut, TeamUpdate

router = APIRouter(prefix="/v1/teams", tags=["teams"])


async def _members(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    if ids:
        rows = await session.execute(
            select(TeamMember.team_id, TeamMember.participant_id)
            .where(TeamMember.team_id.in_(ids))
            .order_by(TeamMember.added_at)
        )
        for team_id, pid in rows.all():
            out[team_id].append(pid)
    return out


def _out(t: Team, members: list[uuid.UUID]) -> TeamOut:
    return TeamOut(
        id=t.id,
        name=t.name,
        description=t.description,
        department_id=t.department_id,
        created_by=t.created_by,
        created_at=t.created_at,
        member_ids=members,
    )


async def _load(session: Session, principal: Principal, team_id: uuid.UUID) -> Team:
    team = await session.get(Team, team_id)
    if team is None or team.org_id != principal.org_id:
        raise HTTPException(404, "team not found")
    return team


def _require_admin(principal: Principal, team: Team) -> None:
    if not principal.can_administer(team.department_id):
        raise HTTPException(403, "not an admin of the team's department")


async def _check_people(session: Session, principal: Principal, ids: list[uuid.UUID]) -> None:
    if not ids:
        return
    found = (
        (
            await session.execute(
                select(Participant.id).where(
                    Participant.id.in_(ids), Participant.org_id == principal.org_id
                )
            )
        )
        .scalars()
        .all()
    )
    if set(found) != set(ids):
        raise HTTPException(404, "participant not found")


@router.get("", response_model=list[TeamOut])
async def list_teams(principal: CurrentPrincipal, session: Session) -> list[TeamOut]:
    teams = (
        (
            await session.execute(
                select(Team).where(Team.org_id == principal.org_id).order_by(Team.name)
            )
        )
        .scalars()
        .all()
    )
    members = await _members(session, [t.id for t in teams])
    return [_out(t, members[t.id]) for t in teams]


@router.post("", response_model=TeamOut, status_code=201)
async def create_team(body: TeamCreate, principal: AdminPrincipal, session: Session) -> TeamOut:
    dept_id = body.department_id or principal.department_id
    dept = await session.get(Department, dept_id)
    if dept is None or dept.org_id != principal.org_id:
        raise HTTPException(404, "department not found")
    if not principal.can_administer(dept.id):
        raise HTTPException(403, "not an admin of that department")
    member_ids = list(dict.fromkeys(body.member_ids))
    await _check_people(session, principal, member_ids)
    team = Team(
        org_id=principal.org_id,
        department_id=dept.id,
        name=body.name.strip(),
        description=body.description,
        created_by=principal.participant_id,
    )
    session.add(team)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "a team with that name already exists") from None
    for pid in member_ids:
        session.add(TeamMember(team_id=team.id, participant_id=pid))
    await session.commit()
    await session.refresh(team)
    return _out(team, member_ids)


@router.patch("/{team_id}", response_model=TeamOut)
async def update_team(
    team_id: uuid.UUID, body: TeamUpdate, principal: AdminPrincipal, session: Session
) -> TeamOut:
    team = await _load(session, principal, team_id)
    _require_admin(principal, team)
    if body.name is not None:
        team.name = body.name.strip()
    if body.description is not None:
        team.description = body.description
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "a team with that name already exists") from None
    await session.refresh(team)
    return _out(team, (await _members(session, [team.id]))[team.id])


@router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: uuid.UUID, principal: AdminPrincipal, session: Session) -> None:
    team = await _load(session, principal, team_id)
    _require_admin(principal, team)
    await session.delete(team)  # members go with it (ON DELETE CASCADE)
    await session.commit()


@router.post("/{team_id}/members", response_model=TeamOut)
async def add_member(
    team_id: uuid.UUID, body: TeamMemberAdd, principal: AdminPrincipal, session: Session
) -> TeamOut:
    team = await _load(session, principal, team_id)
    _require_admin(principal, team)
    await _check_people(session, principal, [body.participant_id])
    await session.execute(
        insert(TeamMember)
        .values(team_id=team.id, participant_id=body.participant_id)
        .on_conflict_do_nothing()
    )
    await session.commit()
    return _out(team, (await _members(session, [team.id]))[team.id])


@router.delete("/{team_id}/members/{participant_id}", response_model=TeamOut)
async def remove_member(
    team_id: uuid.UUID, participant_id: uuid.UUID, principal: AdminPrincipal, session: Session
) -> TeamOut:
    team = await _load(session, principal, team_id)
    _require_admin(principal, team)
    await session.execute(
        delete(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.participant_id == participant_id
        )
    )
    await session.commit()
    return _out(team, (await _members(session, [team.id]))[team.id])
