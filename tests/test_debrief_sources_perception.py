from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefValidationError
from fakeyou.debrief.debrief_sources import (
    SCREEN_SOURCE_MAX_CHARS,
    SourceEntry,
    load_record_snapshot,
    screen_source_text,
)
from fakeyou.persistence.record import ScreenEntry
from tests._debrief_sources_support import chat, encoded, screen, transcript
from tests._error_support import message_pattern

PRIVATE_MARKER = "PRIVATE-CHAT-MARKER"


def snapshot_sources(tmp_path: Path, *entries: dict[str, Any]) -> tuple[SourceEntry, ...]:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(*entries))
    return load_record_snapshot(path).sources


def test_screen_entry_is_a_source_attributed_to_its_presenter(tmp_path: Path) -> None:
    assert snapshot_sources(tmp_path, screen()) == (
        SourceEntry(
            id="screen:screen-1",
            text="\n".join(
                (
                    "Q3 revenue",
                    "Revenue is up",
                    "12%",
                    "bar chart",
                    "Quarterly revenue grew twelve percent.",
                )
            ),
            kind="screen",
            participant_id="devices/7",
            at=datetime(2026, 10, 3, 12, 0, 3, tzinfo=UTC),
            display_claim="Rina",
        ),
    )


def test_screen_without_a_presenter_belongs_to_the_shared_screen(tmp_path: Path) -> None:
    [source] = snapshot_sources(tmp_path, screen(presenter_id=None, presenter=None))

    assert source.participant_id == "shared_screen"
    assert source.display_claim is None


def test_later_reading_of_a_screen_replaces_the_source_in_place(tmp_path: Path) -> None:
    sources = snapshot_sources(
        tmp_path,
        screen(),
        transcript(),
        screen(ref="screen-2", title="Roadmap"),
        screen(title="Q3 revenue, revised", text="Revenue grew thirteen percent."),
    )

    assert [source.id for source in sources] == [
        "screen:screen-1",
        "transcript:entry-1",
        "screen:screen-2",
    ]
    assert sources[0].text.startswith("Q3 revenue, revised\n")
    assert sources[0].text.endswith("Revenue grew thirteen percent.")


def test_screen_source_text_skips_blank_parts_and_is_capped() -> None:
    entry = ScreenEntry.model_validate(
        screen(key_points=["  ", "Revenue is up"], figures=[], visuals=[], text="x" * 3000)
    )

    text = screen_source_text(entry)

    assert text.startswith("Q3 revenue\nRevenue is up\nxxx")
    assert len(text) == SCREEN_SOURCE_MAX_CHARS


def test_blank_screen_ref_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(screen(ref="  ")))

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_SCREEN_REF_BLANK)
    ) as caught:
        load_record_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_SCREEN_REF_BLANK


def test_chat_entry_is_a_source_attributed_to_its_sender(tmp_path: Path) -> None:
    assert snapshot_sources(tmp_path, chat()) == (
        SourceEntry(
            id="chat:chat-1",
            text="I will send the deck tonight.",
            kind="chat",
            participant_id="devices/8",
            at=datetime(2026, 10, 3, 12, 0, 8, tzinfo=UTC),
            display_claim="Budi",
        ),
    )


def test_own_chat_line_is_twin_speech(tmp_path: Path) -> None:
    [source] = snapshot_sources(tmp_path, chat(own=True, sender="Twin"))

    assert (source.kind, source.participant_id, source.display_claim) == ("twin", "twin", None)


def test_identical_chat_messages_are_deduplicated(tmp_path: Path) -> None:
    sources = snapshot_sources(tmp_path, chat(), chat(), chat(ref="chat-2", message_id="message-2"))

    assert [source.id for source in sources] == ["chat:chat-1", "chat:chat-2"]


@pytest.mark.parametrize("changes", [{"text": PRIVATE_MARKER}, {"sender_id": PRIVATE_MARKER}])
def test_contradictory_chat_duplicates_are_rejected_without_input(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(chat(), chat(**changes)))

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_CHAT_CONTRADICTORY)
    ) as caught:
        load_record_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_CHAT_CONTRADICTORY
    assert PRIVATE_MARKER not in str(caught.value)


def test_signals_are_never_sources(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(
        encoded(
            {
                "kind": "signal",
                "at": "2026-10-03T12:00:00Z",
                "seq": 1,
                "signal": "hand_raised",
                "participant_id": "devices/8",
                "participant": "Budi",
                "observed_at": "2026-10-03T12:00:00Z",
            }
        )
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NO_SPEECH_SOURCES)
    ):
        load_record_snapshot(path)


def test_a_non_meeting_screen_contributes_only_its_title(tmp_path: Path) -> None:
    [source] = snapshot_sources(
        tmp_path,
        screen(
            content="dashboard",
            title="Cartesia Usage",
            text="Receipt number 1450-6289, amount paid Rp899,900.00.",
        ),
    )

    assert source.text == "Cartesia Usage"
    assert "1450-6289" not in source.text
