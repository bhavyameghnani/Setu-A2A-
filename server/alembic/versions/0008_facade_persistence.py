"""persistent A2A facade: tasks and push-notification configs

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facade_tasks",
        sa.Column("wire_task_id", sa.Text(), nullable=False),
        sa.Column("caller_id", sa.UUID(), nullable=False),
        sa.Column("callee_id", sa.UUID(), nullable=False),
        sa.Column("setu_task_id", sa.UUID(), nullable=True),
        sa.Column("context_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column(
            "task", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["caller_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["callee_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["setu_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("wire_task_id"),
    )
    op.create_index(
        "ix_facade_tasks_owner", "facade_tasks", ["caller_id", "callee_id", "updated_at"]
    )
    op.create_index("ix_facade_tasks_setu", "facade_tasks", ["setu_task_id"])
    op.create_table(
        "facade_push_configs",
        sa.Column("wire_task_id", sa.Text(), nullable=False),
        sa.Column("config_id", sa.Text(), nullable=False),
        sa.Column("caller_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        # token and authentication credentials, Fernet-encrypted (app.core.secrets)
        sa.Column("secret", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["caller_id"], ["participants.id"]),
        sa.PrimaryKeyConstraint("wire_task_id", "config_id"),
    )


def downgrade() -> None:
    op.drop_table("facade_push_configs")
    op.drop_table("facade_tasks")
