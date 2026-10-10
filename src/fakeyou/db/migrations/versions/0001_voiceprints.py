import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voiceprints",
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("dim", sa.SmallInteger(), nullable=False),
        sa.Column("n_utterances", sa.Integer(), nullable=False),
        sa.Column("consent_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("is_negative", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("person_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "refusing to drop voiceprints: the baseline downgrade would delete biometric data"
    )
