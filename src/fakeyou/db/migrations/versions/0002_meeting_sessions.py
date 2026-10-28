import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_sessions",
        sa.Column("meeting_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("desired_state", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("meeting_id"),
    )
    op.create_index("ix_meeting_sessions_owner_id", "meeting_sessions", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_meeting_sessions_owner_id", table_name="meeting_sessions")
    op.drop_table("meeting_sessions")
