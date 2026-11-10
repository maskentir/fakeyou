import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_sessions",
        sa.Column("payload", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_sessions", "payload")
