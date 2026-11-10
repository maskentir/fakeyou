import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "owner_model_revisions",
        sa.Column("meeting_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "owner_model_revisions",
        sa.Column("evidence", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("owner_model_revisions", "evidence")
    op.drop_column("owner_model_revisions", "meeting_id")
