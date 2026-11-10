from __future__ import annotations

from collections.abc import MutableMapping
from typing import Literal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB

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

meeting_sessions = Table(
    "meeting_sessions",
    metadata,
    Column("meeting_id", Text, primary_key=True),
    Column("owner_id", Text, nullable=False, index=True),
    Column("desired_state", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("terminal_reason", Text, nullable=True),
    Column("detail", Text, nullable=True),
    Column("attempt_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", Text, nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("payload", JSONB(none_as_null=True), nullable=True),
)


owner_model_revisions = Table(
    "owner_model_revisions",
    metadata,
    Column("revision_id", Text, primary_key=True),
    Column("item_id", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("twin_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("topic", Text, nullable=False),
    Column("statement", Text, nullable=False),
    Column("reason", Text, nullable=True),
    Column("confidence", Text, nullable=False),
    Column("disclosure", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("model_version", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("valid_at", DateTime(timezone=True), nullable=True),
    Column("meeting_id", Text, nullable=True),
    Column("evidence", JSONB(none_as_null=True), nullable=True),
    UniqueConstraint("item_id", "revision", name="owner_model_item_revision"),
    UniqueConstraint("owner_id", "twin_id", "model_version", name="owner_model_version"),
    Index("owner_model_scope", "owner_id", "twin_id"),
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
