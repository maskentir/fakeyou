from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import PersonaProfile, load_persona


async def load_operator_persona(
    persona_path: Path, owner_id: str | None, twin_id: str | None, settings: Settings
) -> PersonaProfile:
    try:
        return await load_persona(
            persona_path,
            owner_id=owner_id or settings.identity.owner_id,
            twin_id=twin_id or settings.identity.twin_id,
        )
    except OSError as error:
        raise FakeYouError(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason=error.strerror) from None
