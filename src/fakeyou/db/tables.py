from __future__ import annotations

from collections.abc import MutableMapping
from typing import Literal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    false,
)

metadata = MetaData()

voiceprints = Table(
    "voiceprints",
    metadata,
    Column("person_id", Text, primary_key=True),
    Column("embedding", LargeBinary, nullable=False),
    Column("dim", SmallInteger, nullable=False),
    Column("n_utterances", Integer, nullable=False),
    Column("consent_ts", DateTime(timezone=True), nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("is_negative", Boolean, nullable=False, server_default=false()),
    Column("enrolled_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=False),
    Column("owner_name", Text, nullable=False),
)


def include_name(
    name: str | None,
    type_: Literal[
        "schema",
        "table",
        "column",
        "index",
        "unique_constraint",
        "foreign_key_constraint",
        "check_constraint",
    ],
    _parent_names: MutableMapping[
        Literal["schema_name", "table_name", "schema_qualified_table_name"], str | None
    ],
) -> bool:
    if type_ == "table":
        return name is not None and name in metadata.tables
    return True
