import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.ids import uuid7

MESSAGE_KINDS = (
    "text",
    "task",
    "task_update",
    "result",
    "approval_request",
    "approval_response",
    "activity",
    "system",
)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, unique=True)


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("org_id", "handle"),
        CheckConstraint("kind in ('human','agent')", name="participants_kind_check"),
    )
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    kind: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    handle: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = _created()


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(
            "visibility in ('private','department','cross_department')",
            name="rooms_visibility_check",
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    name: Mapped[str] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(Text, server_default="private")
    owning_department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    created_at: Mapped[datetime] = _created()
    # Highest seq handed out in this room. Bumped with UPDATE ... RETURNING, whose row lock
    # serializes concurrent senders so seq is gap-free and commits in order.
    last_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")


class RoomMember(Base):
    __tablename__ = "room_members"
    __table_args__ = (
        CheckConstraint("role in ('owner','member','observer')", name="room_members_role_check"),
    )
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, server_default="member")
    joined_at: Mapped[datetime] = _created()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("room_id", "seq"),
        CheckConstraint(
            "kind in (" + ",".join(f"'{k}'" for k in MESSAGE_KINDS) + ")",
            name="messages_kind_check",
        ),
        Index("ix_messages_room_seq", "room_id", "seq"),
    )
    id: Mapped[uuid.UUID] = _pk()
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    kind: Mapped[str] = mapped_column(Text, server_default="text")
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    mentions: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), server_default="{}"
    )
    reply_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    # trace_id / span_id / delegation_chain_id are plain columns until their tables arrive.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", use_alter=True, name="fk_messages_task_id"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegation_chain_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # Participants expected to act on this message, decided once at send time by core.routing.
    addressed: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), server_default="{}"
    )
    created_at: Mapped[datetime] = _created()


class Human(Base):
    __tablename__ = "humans"
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _pk()
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"), index=True)
    key_prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoomDepartmentAccess(Base):
    __tablename__ = "room_department_access"
    __table_args__ = (
        CheckConstraint("access in ('read','participate')", name="room_dept_access_check"),
    )
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"), primary_key=True)
    access: Mapped[str] = mapped_column(Text)


class MessageStatus(Base):
    __tablename__ = "message_status"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','acked','completed','failed')", name="message_status_check"
        ),
        Index("ix_message_status_participant", "participant_id", "status"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), primary_key=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Cursor(Base):
    __tablename__ = "cursors"
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    last_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")


class Subscription(Base):
    """'' in kind or skill means any. Unique so re-subscribing is idempotent."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("room_id", "participant_id", "kind", "skill"),)
    id: Mapped[uuid.UUID] = _pk()
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    kind: Mapped[str] = mapped_column(Text, server_default="")
    skill: Mapped[str] = mapped_column(Text, server_default="")


class RoomSnapshot(Base):
    __tablename__ = "room_snapshots"
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    up_to_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Agent(Base):
    """Registry entry for an agent participant. `transport='sdk'` agents connect over the
    WebSocket; `a2a_remote` agents are called by Setu over A2A. The a2a_* columns are only set
    for the latter."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("transport in ('a2a_remote','sdk')", name="agents_transport_check"),
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    owner_department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    transport: Mapped[str] = mapped_column(Text)
    a2a_card_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    a2a_card_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    a2a_protocol_versions: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    a2a_version: Mapped[str | None] = mapped_column(Text, nullable=True)  # wire dialect: 1.0 | 0.3
    a2a_rpc_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # never shown to other agents
    streaming: Mapped[bool] = mapped_column(Boolean, server_default="false")
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    # {"v":1,"ct":"<fernet token>"}: encrypted credentials Setu uses to call the agent.
    auth_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    card_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    card_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default="60")
    max_retries: Mapped[int] = mapped_column(Integer, server_default="2")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "state in ('submitted','working','input_required','auth_required',"
            "'completed','failed','canceled','rejected')",
            name="tasks_state_check",
        ),
        Index("ix_tasks_room", "room_id", "created_at"),
        Index("ix_tasks_dispatch", "state", "claimed_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id"))
    request_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    a2a_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    a2a_context_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    assignee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", use_alter=True, name="fk_messages_task_id"), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, server_default="submitted")
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegation_chain_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when a dispatcher picks the task up; makes dispatch single-shot across restarts.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The `a2a_call` span of the latest call to the agent; child work hangs off it.
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskArtifact(Base):
    __tablename__ = "task_artifacts"
    __table_args__ = (UniqueConstraint("task_id", "a2a_artifact_id"),)
    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    a2a_artifact_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = _created()


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    rule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("participants.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created()


class Delegation(Base):
    __tablename__ = "delegations"
    id: Mapped[uuid.UUID] = _pk()
    root_human_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    from_participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    to_participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"), index=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delegations.id"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class Approval(Base):
    """A human decision a task is waiting for. `policy` approvals gate a task before the agent
    is called; `agent` approvals answer an agent's own `input_required`."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("kind in ('policy','agent')", name="approvals_kind_check"),
        CheckConstraint("state in ('pending','approved','denied')", name="approvals_state_check"),
        Index("ix_approvals_state", "state", "approver_department_id"),
    )
    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policies.id", ondelete="SET NULL"), nullable=True
    )
    approver_department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    approver_role: Mapped[str] = mapped_column(Text)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("participants.id"), nullable=True
    )  # a specific person who may answer (the requester, for `agent` approvals)
    state: Mapped[str] = mapped_column(Text, server_default="pending")
    summary: Mapped[str] = mapped_column(Text, server_default="")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("participants.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `agent` approvals: set when the dispatcher has sent the answer back to the agent.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # the `approval` span
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only and hash-chained per org. A trigger blocks UPDATE/DELETE/TRUNCATE."""

    __tablename__ = "audit_log"
    __table_args__ = (UniqueConstraint("org_id", "seq"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id"), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Span(Base):
    """One step in a trace. `id` is the 16-hex W3C span id. `org_id` and `room_id` are extra
    columns beyond spec section 7 so visibility filtering is a plain join."""

    __tablename__ = "spans"
    __table_args__ = (
        CheckConstraint(
            "kind in ('message','a2a_call','policy','delegation','approval')",
            name="spans_kind_check",
        ),
        Index("ix_spans_trace", "trace_id", "started_at"),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rooms.id"), nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, server_default="")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="ok")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class Notification(Base):
    """One inbox entry for one human (spec section 12). `summary` is rendered from a template
    when the event happens (`core/notifications.py`), never by an LLM. `event_type` is an extra
    column beyond spec section 7."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_inbox", "recipient_id", "id"),)
    id: Mapped[uuid.UUID] = _pk()  # uuid7: time-ordered, so it doubles as the paging cursor
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rooms.id"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class FacadeTask(Base):
    """An A2A task as seen through Setu's facade: the SDK's task snapshot, owned by the caller,
    plus the Setu task it stands for. Replaces the in-memory SDK task store (decision 124)."""

    __tablename__ = "facade_tasks"
    __table_args__ = (
        Index("ix_facade_tasks_owner", "caller_id", "callee_id", "updated_at"),
        Index("ix_facade_tasks_setu", "setu_task_id"),
    )
    wire_task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    caller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    callee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    setu_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    context_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    task: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class FacadePushConfig(Base):
    __tablename__ = "facade_push_configs"
    wire_task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    config_id: Mapped[str] = mapped_column(Text, primary_key=True)
    caller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class Team(Base):
    """A named group of people and agents, owned by a department. Membership is only a grouping
    (handy for adding a whole team to a room); it grants no access or authority by itself."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id"))
    created_at: Mapped[datetime] = _created()


class TeamMember(Base):
    __tablename__ = "team_members"
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id"), primary_key=True
    )
    added_at: Mapped[datetime] = _created()
