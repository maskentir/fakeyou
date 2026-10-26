from pathlib import Path

import pytest

from fakeyou.config.settings import Settings
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.orchestration.join_context import prepare_join
from fakeyou.orchestration.join_types import JoinRequest
from tests._cli_support import MEET_LINK, write_brief, write_persona


def _request(settings: Settings, persona_path: Path, brief_path: Path | None) -> JoinRequest:
    return JoinRequest(
        link=MEET_LINK,
        settings=settings,
        identity=OperatorIdentity(persona_path),
        audio_enabled=True,
        meeting_id="meeting-9",
        conversation_enabled=True,
        brief_path=brief_path,
    )


@pytest.mark.parametrize(
    ("permissions", "enabled"),
    [
        (["explain_facts", "meet_chat"], True),
        (["explain_facts", "meet_act"], True),
        (["explain_facts", "ask_brief_questions"], False),
    ],
)
async def test_meet_actions_follow_the_brief_permissions(
    settings: Settings, tmp_path: Path, permissions: list[str], *, enabled: bool
) -> None:
    prepared = await prepare_join(
        _request(settings, write_persona(tmp_path), write_brief(tmp_path, permissions=permissions))
    )

    assert prepared.config.meet_actions_enabled is enabled


async def test_without_a_brief_meet_actions_stay_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    prepared = await prepare_join(_request(settings, write_persona(tmp_path), None))

    assert prepared.config.meet_actions_enabled is False
