"""API contract for the control plane: policies, delegations, approvals, audit."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1)
    rule: dict[str, Any]
    enabled: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    rule: dict[str, Any] | None = None
    enabled: bool | None = None


class PolicyOut(_Out):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    rule: dict[str, Any]
    enabled: bool
    created_by: uuid.UUID | None
    created_at: datetime


class DelegationCreate(BaseModel):
    to_participant_id: uuid.UUID
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None
    parent_id: uuid.UUID | None = None  # required when an agent passes its own authority on


class DelegationOut(_Out):
    id: uuid.UUID
    root_human_id: uuid.UUID
    from_participant_id: uuid.UUID
    to_participant_id: uuid.UUID
    scopes: list[str]
    parent_id: uuid.UUID | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    approve: bool
    # For an agent's question this is the answer sent back to the agent.
    note: str | None = Field(default=None, max_length=2000)


class ApprovalOut(_Out):
    id: uuid.UUID
    task_id: uuid.UUID
    room_id: uuid.UUID | None = None
    kind: str
    state: str
    approver_department_id: uuid.UUID
    approver_role: str
    approver_id: uuid.UUID | None
    summary: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


class AuditEventOut(_Out):
    seq: int
    event_type: str
    actor_id: uuid.UUID | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    first_broken_seq: int | None


class SpanOut(_Out):
    id: str
    trace_id: str
    parent_span_id: str | None
    kind: str
    name: str
    room_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    task_id: uuid.UUID | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    duration_ms: int | None = None
    attributes: dict[str, Any]
    children: list["SpanOut"] = Field(default_factory=list)


class TraceOut(BaseModel):
    trace_id: str
    span_count: int
    hidden_spans: int  # spans in this trace that this caller's departments cannot see
    roots: list[SpanOut]


class NotificationActions(BaseModel):
    """What this person can do with a notification right now (computed on read)."""

    open_room: bool = False
    reply: bool = False
    approval_id: uuid.UUID | None = None  # a pending approval this person may decide
    cancel_task: bool = False


class NotificationOut(_Out):
    id: uuid.UUID
    recipient_id: uuid.UUID
    department_id: uuid.UUID
    room_id: uuid.UUID | None
    task_id: uuid.UUID | None
    trace_id: str | None
    event_type: str
    summary: str
    detail: dict[str, Any]
    read_at: datetime | None
    created_at: datetime
    actions: NotificationActions = Field(default_factory=NotificationActions)


class NotificationList(BaseModel):
    unread: int
    items: list[NotificationOut]
    next_before: uuid.UUID | None = None  # pass as `before` for the next (older) page


class NotificationReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
