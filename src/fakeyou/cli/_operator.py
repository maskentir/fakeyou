from dataclasses import dataclass
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import PersonaProfile, load_persona


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    persona_path: Path | None
    owner_id: str | None = None
    twin_id: str | None = None


async def load_operator_persona(identity: OperatorIdentity, settings: Settings) -> PersonaProfile:
    if identity.persona_path is None:
        raise FakeYouError(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason="persona path is missing")
    try:
        return await load_persona(
            identity.persona_path,
            owner_id=identity.owner_id or settings.identity.owner_id,
            twin_id=identity.twin_id or settings.identity.twin_id,
        )
    except OSError as error:
        raise FakeYouError(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason=error.strerror) from None
