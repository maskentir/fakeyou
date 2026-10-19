from pathlib import Path

from pydantic import BaseModel

from fakeyou.config.identifiers import parse_session_id
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir

PERSONA_FILE_NAME = "worker-persona.json"
BRIEF_FILE_NAME = "worker-brief.json"


def session_dir(record_dir: Path, meeting_id: str) -> Path:
    return MeetingDir(record_dir, parse_session_id(meeting_id, field="meeting_id")).dir


def write_session_document(path: Path, payload: BaseModel) -> Path:
    write_atomically(path, (payload.model_dump_json() + "\n").encode("utf-8"))
    return path


def write_session_persona(record_dir: Path, meeting_id: str, persona: PersonaProfile) -> Path:
    return write_session_document(session_dir(record_dir, meeting_id) / PERSONA_FILE_NAME, persona)
