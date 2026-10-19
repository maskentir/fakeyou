from functools import partial
from typing import Any

import fakeyou.orchestration.providers as providers
from fakeyou.cli.output import emit_json
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import DebriefModels
from fakeyou.debrief.debrief_service import DebriefRequest, DebriefWiring, run_debrief
from fakeyou.memory.persona import OperatorIdentity, load_operator_persona
from fakeyou.orchestration.assembly import stop_signal_handlers


def _debrief_models(settings: Settings) -> DebriefModels:
    return DebriefModels(
        providers.create_debrief_chat_model(settings),
        providers.create_fallback_chat_model(settings),
    )


async def debrief_in_process(
    meeting_id: SessionId,
    identity: OperatorIdentity,
    *,
    force: bool,
    settings: Settings,
    **_unused: Any,
) -> None:
    operator = await load_operator_persona(identity, settings)
    outcome = await run_debrief(
        DebriefRequest(meeting_id=meeting_id, operator=operator, settings=settings, force=force),
        DebriefWiring(
            models=partial(_debrief_models, settings),
            stop_signals=stop_signal_handlers,
        ),
    )
    emit_json(
        {
            "meeting_id": outcome.meeting_id,
            "status": "completed",
            "review_status": "unreviewed",
            "cached": outcome.cached,
            "json_path": str(outcome.json_path),
            "markdown_path": str(outcome.markdown_path),
        }
    )
