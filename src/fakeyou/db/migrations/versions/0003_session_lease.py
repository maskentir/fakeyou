import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meeting_sessions", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "meeting_sessions",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "meeting_sessions",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("meeting_sessions", "attempts")
    op.drop_column("meeting_sessions", "lease_expires_at")
    op.drop_column("meeting_sessions", "lease_owner")
