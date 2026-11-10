from alembic import op
from procrastinate.schema import SchemaManager

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(SchemaManager.get_schema())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS procrastinate_periodic_defers CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_events CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_workers CASCADE")
    op.execute(
        """
        DO $$
        DECLARE
            routine record;
        BEGIN
            FOR routine IN
                SELECT ns.nspname AS schema_name, p.proname AS routine_name,
                       pg_get_function_identity_arguments(p.oid) AS args
                FROM pg_proc p
                JOIN pg_namespace ns ON ns.oid = p.pronamespace
                WHERE p.proname LIKE 'procrastinate_%'
            LOOP
                EXECUTE format(
                    'DROP FUNCTION IF EXISTS %I.%I(%s) CASCADE',
                    routine.schema_name, routine.routine_name, routine.args
                );
            END LOOP;
        END $$
        """
    )
    op.execute("DROP TYPE IF EXISTS procrastinate_job_to_defer_v1 CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_event_type CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_status CASCADE")
