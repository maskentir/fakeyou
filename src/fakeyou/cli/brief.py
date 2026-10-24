import asyncio
from pathlib import Path

from fakeyou.catalog.errors import FakeYouError
from fakeyou.cli.output import emit_json
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import read_debrief
from fakeyou.memory.memory import open_graphiti_memory, search_memory_evidence
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.brief_preparation import (
    BriefSources,
    build_brief,
    draft_brief,
)
from fakeyou.orchestration.providers import create_chat_model

BRIEF_QUERY = "what the owner wants to settle, notes, and open items from earlier meetings"


def _open_questions(settings: Settings) -> tuple[str, ...]:
    reports = sorted(
        settings.paths.record_dir.glob("*/debrief.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in reports:
        try:
            report = read_debrief(path.parent.parent / "record.jsonl")
        except (FakeYouError, OSError, ValueError):
            continue
        questions = tuple(
            finding.objective_id
            for finding in report.findings
            if finding.status in ("open", "uncertain")
        )
        if questions:
            return questions
    return ()


async def _owner_memory(settings: Settings, profile: PersonaProfile) -> str:
    async with open_graphiti_memory(settings.memory) as memory:
        return await search_memory_evidence(
            memory,
            BRIEF_QUERY,
            group_id=profile.memory_group,
            recall=settings.memory.recall,
        )


async def prepare_brief(
    *,
    meeting_id: str,
    persona_path: Path,
    out_path: Path,
    settings: Settings,
) -> None:
    parsed = parse_session_id(meeting_id, field="meeting_id")
    profile = PersonaProfile.model_validate_json(await asyncio.to_thread(persona_path.read_bytes))
    sources = BriefSources(
        notes=(await _owner_memory(settings, profile),),
        open_questions=_open_questions(settings),
    )
    draft = await draft_brief(create_chat_model(settings), profile, parsed, sources)
    brief = build_brief(draft, profile, parsed)
    await asyncio.to_thread(out_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        out_path.write_text, brief.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    emit_json(
        {
            "meeting_id": meeting_id,
            "path": str(out_path),
            "agenda": len(brief.agenda),
            "objectives": len(brief.objectives),
            "questions": len(brief.questions),
            "permissions": sorted(brief.permissions),
        }
    )


def default_brief_path(settings: Settings, meeting_id: str) -> Path:
    return settings.paths.record_dir.parent / "briefs" / f"{meeting_id}.json"
