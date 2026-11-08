from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.db.owner_model import (
    ItemDisclosure,
    OwnerModelSnapshot,
)
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.session.facts import Confidence, OwnerModelKind

OWNER_MODEL_REF_PREFIX = "om-"


class FrozenOwnerModelItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    item_id: str = Field(min_length=1)
    kind: OwnerModelKind
    topic: str
    statement: str
    confidence: Confidence
    disclosure: ItemDisclosure
    valid_at: AwareDatetime | None = None

    @property
    def ref(self) -> str:
        return f"{OWNER_MODEL_REF_PREFIX}{self.item_id}"


class FrozenOwnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    version: int = Field(ge=0)
    items: tuple[FrozenOwnerModelItem, ...] = ()

    @classmethod
    def of(cls, snapshot: OwnerModelSnapshot) -> Self:
        return cls(
            version=snapshot.version,
            items=tuple(
                FrozenOwnerModelItem(
                    item_id=item.item_id,
                    kind=item.entry.kind,
                    topic=item.entry.topic,
                    statement=item.entry.statement,
                    confidence=item.entry.confidence,
                    disclosure=item.entry.disclosure,
                    valid_at=item.entry.valid_at,
                )
                for item in snapshot.items
            ),
        )


def owner_model_digest(model: FrozenOwnerModel) -> str:
    return canonical_json_digest(model.model_dump(mode="json"), compact=True)
