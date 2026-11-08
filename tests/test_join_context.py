from dataclasses import replace
from pathlib import Path

from fakeyou.config.settings import Settings
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.orchestration.join_context import prepare_join
from fakeyou.orchestration.join_types import JoinRequest
from fakeyou.persistence.owner_model import (
    FrozenOwnerModel,
    FrozenOwnerModelItem,
    owner_model_digest,
)
from tests._cli_support import MEET_LINK, write_brief, write_persona

OWNER_MODEL = FrozenOwnerModel(
    version=7,
    items=(
        FrozenOwnerModelItem(
            item_id="ship",
            kind="position",
            topic="release",
            statement="Ship after the load test passes.",
            confidence="firm",
            disclosure="room_speakable",
        ),
    ),
)


def _request(settings: Settings, tmp_path: Path) -> JoinRequest:
    return JoinRequest(
        link=MEET_LINK,
        settings=settings,
        identity=OperatorIdentity(write_persona(tmp_path)),
        audio_enabled=True,
        meeting_id="meeting-9",
        conversation_enabled=True,
        brief_path=write_brief(tmp_path),
    )


async def test_the_frozen_owner_model_reaches_the_join_and_its_context(
    settings: Settings, tmp_path: Path
) -> None:
    prepared = await prepare_join(replace(_request(settings, tmp_path), owner_model=OWNER_MODEL))

    assert prepared.owner_model == OWNER_MODEL
    assert prepared.meeting_context is not None
    assert prepared.meeting_context.owner_model_version == 7
    assert prepared.meeting_context.owner_model_sha256 == owner_model_digest(OWNER_MODEL)


async def test_a_local_join_without_an_owner_model_records_none(
    settings: Settings, tmp_path: Path
) -> None:
    prepared = await prepare_join(_request(settings, tmp_path))

    assert prepared.owner_model is None
    assert prepared.meeting_context is not None
    assert prepared.meeting_context.owner_model_version is None
    assert prepared.meeting_context.owner_model_sha256 is None
