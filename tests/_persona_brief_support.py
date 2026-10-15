import json
from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.observability.journal import journal_path
from tests._cli_support import enable_audio_platform, forbid_resources


def meeting_files(settings: Settings) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for root in (settings.paths.record_dir, settings.paths.journal_dir)
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def journal_rows(settings: Settings, meeting_id: str = "meeting-9") -> list[dict[str, Any]]:
    path = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def prepare_brief_join(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)
