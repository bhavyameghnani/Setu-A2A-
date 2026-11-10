from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OversightPrincipal, Session
from app.db import audit
from app.db.models import AuditLog
from app.schemas import AuditEventOut, AuditVerifyOut

router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("", response_model=list[AuditEventOut])
async def list_audit(
    principal: OversightPrincipal,
    session: Session,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Sequence[AuditLog]:
    return await audit.list_events(session, principal.org_id, after_seq, limit)


@router.post("/verify", response_model=AuditVerifyOut)
async def verify_audit(principal: OversightPrincipal, session: Session) -> AuditVerifyOut:
    """Walk the hash chain; `first_broken_seq` is where it stops matching."""
    res = await audit.verify(session, principal.org_id)
    return AuditVerifyOut(ok=res.ok, checked=res.checked, first_broken_seq=res.first_broken_seq)
