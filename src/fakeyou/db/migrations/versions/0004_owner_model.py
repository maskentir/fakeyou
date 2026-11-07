import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_model_revisions",
        sa.Column("revision_id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("twin_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("disclosure", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("item_id", "revision", name="owner_model_item_revision"),
        sa.UniqueConstraint("owner_id", "twin_id", "model_version", name="owner_model_version"),
    )
    op.create_index("owner_model_scope", "owner_model_revisions", ["owner_id", "twin_id"])


def downgrade() -> None:
    op.drop_index("owner_model_scope", table_name="owner_model_revisions")
    op.drop_table("owner_model_revisions")
