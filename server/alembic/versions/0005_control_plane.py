"""control plane: policies, delegations, approvals, audit log

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("id", sa.UUID(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "policies",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["participants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_policies_org_id", "policies", ["org_id"])

    op.create_table(
        "delegations",
        _id(),
        sa.Column("root_human_id", sa.UUID(), nullable=False),
        sa.Column("from_participant_id", sa.UUID(), nullable=False),
        sa.Column("to_participant_id", sa.UUID(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["root_human_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["from_participant_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["to_participant_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["delegations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegations_to_participant_id", "delegations", ["to_participant_id"])

    op.create_table(
        "approvals",
        _id(),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("approver_department_id", sa.UUID(), nullable=False),
        sa.Column("approver_role", sa.Text(), nullable=False),
        sa.Column("approver_id", sa.UUID(), nullable=True),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind in ('policy','agent')", name="approvals_kind_check"),
        sa.CheckConstraint(
            "state in ('pending','approved','denied')", name="approvals_state_check"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["policies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approver_department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["approver_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["participants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_task_id", "approvals", ["task_id"])
    op.create_index("ix_approvals_state", "approvals", ["state", "approver_department_id"])

    op.create_table(
        "audit_log",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["participants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "seq"),
    )
    op.create_index("ix_tasks_delegation_chain", "tasks", ["delegation_chain_id"])

    op.execute(
        """
        CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()
        """
    )
