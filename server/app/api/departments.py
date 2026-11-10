from collections.abc import Sequence

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentPrincipal, Session
from app.db.models import Department
from app.schemas import DepartmentCreate, DepartmentOut

router = APIRouter(prefix="/v1", tags=["departments"])


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    body: DepartmentCreate, principal: CurrentPrincipal, session: Session
) -> Department:
    if not (principal.is_human and principal.is_org_admin):
        raise HTTPException(403, "org_admin required")
    dept = Department(org_id=principal.org_id, name=body.name, slug=body.slug)
    session.add(dept)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "department slug already exists in org") from None
    return dept


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(principal: CurrentPrincipal, session: Session) -> Sequence[Department]:
    rows = await session.execute(
        select(Department).where(Department.org_id == principal.org_id).order_by(Department.name)
    )
    return rows.scalars().all()
