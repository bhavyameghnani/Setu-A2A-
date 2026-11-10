import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MessageKind = Literal[
    "text",
    "task",
    "task_update",
    "result",
    "approval_request",
    "approval_response",
    "activity",
    "system",
]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OrgCreate(BaseModel):
    name: str = Field(min_length=1)


class OrgOut(_Out):
    id: uuid.UUID
    name: str


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")


class DepartmentOut(_Out):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str


Role = Literal["dept_member", "dept_admin", "org_admin", "auditor"]
Scope = Literal["rooms:read", "rooms:write", "messages:send"]


def _default_roles() -> list[Role]:
    return ["dept_member"]


def _default_scopes() -> list[Scope]:
    return ["rooms:read", "messages:send"]


_HANDLE = r"^[a-z0-9][a-z0-9._-]*$"
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AgentCreate(BaseModel):
    """An agent participant. Phase 4 adds registration by Agent Card URL on top of this."""

    department_id: uuid.UUID
    display_name: str = Field(min_length=1)
    handle: str = Field(min_length=1, pattern=_HANDLE)


class HumanCreate(BaseModel):
    department_id: uuid.UUID
    display_name: str = Field(min_length=1)
    handle: str = Field(min_length=1, pattern=_HANDLE)
    email: str = Field(pattern=_EMAIL)
    password: str = Field(min_length=10)
    roles: list[Role] = Field(default_factory=_default_roles)


class LoginRequest(BaseModel):
    email: str
    password: str


class ParticipantOut(_Out):
    id: uuid.UUID
    org_id: uuid.UUID
    department_id: uuid.UUID
    kind: str
    display_name: str
    handle: str
    status: str
    created_at: datetime


class RoomCreate(BaseModel):
    name: str = Field(min_length=1)
    topic: str | None = None
    visibility: Literal["private", "department", "cross_department"] = "private"
    owning_department_id: uuid.UUID | None = None  # defaults to the caller's department


class RoomOut(_Out):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    topic: str | None
    visibility: str
    owning_department_id: uuid.UUID
    created_by: uuid.UUID
    created_at: datetime
    last_seq: int


class MemberAdd(BaseModel):
    participant_id: uuid.UUID
    role: Literal["owner", "member", "observer"] = "member"


class MemberOut(_Out):
    room_id: uuid.UUID
    participant_id: uuid.UUID
    role: str
    joined_at: datetime


class MessageCreate(BaseModel):
    kind: MessageKind = "text"
    body: dict[str, Any]
    mentions: list[uuid.UUID] = Field(default_factory=list)
    reply_to: uuid.UUID | None = None
    task_id: uuid.UUID | None = None  # join an existing task's thread (or parent of a new task)
    # Agents sending a `task` must name the delegation they act under (spec 10).
    delegation_id: uuid.UUID | None = None


class MessageOut(_Out):
    id: uuid.UUID
    room_id: uuid.UUID
    seq: int
    sender_id: uuid.UUID
    kind: str
    body: dict[str, Any]
    mentions: list[uuid.UUID]
    reply_to: uuid.UUID | None
    task_id: uuid.UUID | None
    trace_id: str | None
    span_id: str | None
    delegation_chain_id: uuid.UUID | None
    addressed: list[uuid.UUID]
    created_at: datetime


class AccessGrant(BaseModel):
    department_id: uuid.UUID
    access: Literal["read", "participate"]


class AccessOut(_Out):
    room_id: uuid.UUID
    department_id: uuid.UUID
    access: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    participant: "ParticipantOut"


class MeOut(BaseModel):
    participant: "ParticipantOut"
    roles: list[str]
    scopes: list[str] | None


class ApiKeyCreate(BaseModel):
    scopes: list[Scope] = Field(default_factory=_default_scopes)


class ApiKeyOut(_Out):
    id: uuid.UUID
    participant_id: uuid.UUID
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str  # the only time the full key is ever returned


class CursorSet(BaseModel):
    last_seq: int = Field(ge=0)


class CursorOut(BaseModel):
    room_id: uuid.UUID
    last_seq: int


class StatusSet(BaseModel):
    status: Literal["acked", "completed", "failed"]


class StatusOut(_Out):
    message_id: uuid.UUID
    participant_id: uuid.UUID
    status: str
    updated_at: datetime


class SubscriptionCreate(BaseModel):
    kind: MessageKind | None = None
    skill: str | None = Field(default=None, min_length=1)


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    kind: str | None
    skill: str | None


class SnapshotOut(_Out):
    room_id: uuid.UUID
    up_to_seq: int
    summary_text: str
    created_at: datetime


class AgentAuth(BaseModel):
    """Credentials Setu uses to call a registered agent. Stored encrypted; never returned."""

    type: Literal["bearer", "api_key"]
    token: str | None = Field(default=None, min_length=1)  # bearer
    header: str | None = Field(default=None, min_length=1)  # api_key
    value: str | None = Field(default=None, min_length=1)  # api_key

    @model_validator(mode="after")
    def _complete(self) -> "AgentAuth":
        if self.type == "bearer" and not self.token:
            raise ValueError("bearer auth needs `token`")
        if self.type == "api_key" and not (self.header and self.value):
            raise ValueError("api_key auth needs `header` and `value`")
        return self

    def to_config(self) -> dict[str, str]:
        if self.type == "bearer":
            return {"type": "bearer", "token": self.token or ""}
        return {"type": "api_key", "header": self.header or "", "value": self.value or ""}


class AgentRegister(BaseModel):
    card_url: str = Field(min_length=8)
    department_id: uuid.UUID
    auth: AgentAuth | None = None
    handle: str | None = Field(default=None, pattern=_HANDLE)
    display_name: str | None = Field(default=None, min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)


class AgentPreviewRequest(BaseModel):
    """Fetch and check an Agent Card without registering it (the wizard's "Test connection")."""

    card_url: str = Field(min_length=8)
    auth: AgentAuth | None = None


class AgentPreviewOut(BaseModel):
    name: str
    description: str
    a2a_version: str
    protocol_versions: list[str]
    streaming: bool
    skills: list["SkillOut"]
    security_schemes: list[str]
    rpc_url: str
    suggested_handle: str
    warnings: list[str] = Field(default_factory=list)


class AgentStatsOut(BaseModel):
    """Activity of one agent: messages it sent, tasks assigned to it, those that failed, rooms."""

    participant_id: uuid.UUID
    messages: int
    tasks: int
    failed: int
    rooms: int


class AgentUpdate(BaseModel):
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    auth: AgentAuth | None = None
    clear_auth: bool = False


class SkillOut(BaseModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class AgentOut(BaseModel):
    """What any org member may see. No endpoint, no credentials (spec 8.5)."""

    participant_id: uuid.UUID
    handle: str
    display_name: str
    department_id: uuid.UUID
    transport: str
    skills: list[SkillOut]
    protocol_versions: list[str]
    a2a_version: str | None
    streaming: bool
    enabled: bool
    card_fetched_at: datetime | None


class AgentAdminOut(AgentOut):
    """For admins of the owning department: adds the registration details."""

    card_url: str | None
    rpc_url: str | None
    timeout_seconds: int
    max_retries: int
    has_credentials: bool
    warnings: list[str] = Field(default_factory=list)
    changed: bool | None = None  # set by /refresh


class ArtifactOut(_Out):
    id: uuid.UUID
    a2a_artifact_id: str
    name: str | None
    parts: list[dict[str, Any]]
    created_at: datetime


class TaskOut(_Out):
    id: uuid.UUID
    room_id: uuid.UUID
    request_message_id: uuid.UUID | None
    requester_id: uuid.UUID
    assignee_id: uuid.UUID
    parent_task_id: uuid.UUID | None
    state: str
    a2a_task_id: str | None
    a2a_context_id: str | None
    trace_id: str | None
    delegation_chain_id: uuid.UUID | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    department_id: uuid.UUID | None = None  # defaults to the caller's department
    member_ids: list[uuid.UUID] = Field(default_factory=list)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class TeamMemberAdd(BaseModel):
    participant_id: uuid.UUID


class TeamOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    department_id: uuid.UUID
    created_by: uuid.UUID
    created_at: datetime
    member_ids: list[uuid.UUID]


class DayCount(BaseModel):
    day: str  # YYYY-MM-DD, UTC
    created: int
    completed: int
    failed: int


class AgentActivity(BaseModel):
    participant_id: uuid.UUID
    tasks: int
    completed: int
    failed: int
    avg_seconds: float | None  # mean time to finish, completed tasks only


class AnalyticsSummary(BaseModel):
    """Task activity over a window. Org admins and auditors see the whole org; everyone else sees
    the tasks their own department asked for or ran."""

    days: int
    scope: Literal["org", "department"]
    total: int
    active: int
    completed: int
    failed: int
    canceled: int
    rejected: int
    success_rate: float | None  # completed / (completed + failed), None if neither yet
    avg_seconds: float | None
    per_day: list[DayCount]
    by_agent: list[AgentActivity]
