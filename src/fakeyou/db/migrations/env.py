from typing import cast

from alembic import context
from sqlalchemy import Connection

from fakeyou.db.tables import include_object, metadata

if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run them through fakeyou db upgrade")

connection = cast(Connection, context.config.attributes["connection"])
context.configure(
    connection=connection,
    target_metadata=metadata,
    include_object=include_object,
    compare_type=True,
    compare_server_default=True,
)
with context.begin_transaction():
    context.run_migrations()
