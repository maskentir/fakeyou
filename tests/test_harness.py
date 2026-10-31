import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig
from fakeyou.conversation.state_speech import render
from fakeyou.persistence.record import ChatEntry, TwinSpeechEntry, load_meeting_record
from fakeyou.replay.fixture import ReplayFixture, build_fixture, load_fixture
from fakeyou.replay.harness import (
    RecordedBehavior,
    ReplayOptions,
    ReplayResult,
    recorded_behavior,
    replay_session,
    schedule,
)
from fakeyou.session.facts import Unknown
from tests._replay_support import START, recorded_conversation, run_virtual

RUNTIME = Path(".runtime/audio")


def conversation() -> ReplayFixture:
    return build_fixture(*recorded_conversation())


def config(settings: Settings) -> SessionConfig:
    return SessionConfig(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        meeting_id=SessionId("replay"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Harun",
    )


def replayed(fixture: ReplayFixture, settings: Settings, tmp_path: Path) -> ReplayResult:
    return run_virtual(
        lambda: replay_session(
            fixture,
            settings,
            config(settings),
            ReplayOptions(
                tmp_path / "record.jsonl",
                RecordedBehavior(twin_names=("Harun",), wake_phrases=("Harun",)),
            ),
        )
    )


def events(result: ReplayResult, name: str) -> list[dict[str, Any]]:
    return [entry for entry in result.rows if entry["event"] == name]


def offset(entry: dict[str, Any]) -> float:
    return (datetime.fromisoformat(entry["at"]) - START).total_seconds()


def test_a_recorded_question_is_heard_judged_and_answered_on_the_recorded_clock(
    settings: Settings, tmp_path: Path
) -> None:
    result = replayed(conversation(), settings, tmp_path)

    [final] = events(result, "transcript_finalized")
    [decided] = events(result, "policy_decided")
    [pcm] = [
        entry
        for entry in events(result, "synthesis_pcm_emitted")
        if entry.get("transcript_id") == final["transcript_id"]
    ]
    assert offset(final) == pytest.approx(2.0)
    assert decided["action"] == "reply"
    assert offset(decided) == pytest.approx(2.1)
    assert pcm["first_chunk_ms"] == pytest.approx(400.0)
    assert len(events(result, "playback_finished")) == 1


def test_the_voice_edges_are_journaled_where_they_were_recorded(
    settings: Settings, tmp_path: Path
) -> None:
    result = replayed(conversation(), settings, tmp_path)

    edges = [
        entry for entry in result.rows if entry["event"] in ("speech_started", "speech_stopped")
    ]
    assert [entry["event"] for entry in edges] == [
        "speech_started",
        "speech_stopped",
        "speech_started",
        "speech_stopped",
    ]
    assert [offset(entry) for entry in edges] == pytest.approx([0.0, 1.0, 9.0, 9.5])


def test_the_replay_reproduces_the_inputs_the_gate_compares(
    settings: Settings, tmp_path: Path
) -> None:
    result = replayed(conversation(), settings, tmp_path)

    gated = {check.name: check for check in result.comparison.gated()}
    assert gated["vad_stop_to_final_ms.p50"].passed
    assert gated["speaker_bound.of"].passed
    assert gated["final_to_first_pcm_ms.p50"].passed


def test_the_recorded_session_sets_the_names_the_twin_answers_to(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "twin_name": None,
                "persona": {"display_name": "Harun"},
                "wake_phrase": "Harun, Run",
                "barge_in": True,
            }
        ),
        encoding="utf-8",
    )

    behavior = recorded_behavior(session)

    assert behavior == RecordedBehavior(
        twin_names=("Harun",), wake_phrases=("Harun", "Run"), barge_in=True
    )


def test_the_last_interim_carries_the_final_words(settings: Settings) -> None:
    del settings
    items = schedule(conversation())

    interims = [item for item in items if item.kind == "interim"]
    final = next(item for item in items if item.kind == "final")
    assert [item.offset for item in interims] == [pytest.approx(1.4), pytest.approx(1.7)]
    assert all(final.text.startswith(item.text) for item in interims)


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_oue_4_session_replays_its_inputs_and_syntheses_exactly(
    settings: Settings, tmp_path: Path
) -> None:
    fixture = load_fixture(
        RUNTIME / "journals/live-oue-4.jsonl", RUNTIME / "meetings/live-oue-4/record.jsonl"
    )
    behavior = recorded_behavior(RUNTIME / "meetings/live-oue-4/session.json")

    result = run_virtual(
        lambda: replay_session(
            fixture,
            settings,
            config(settings),
            ReplayOptions(tmp_path / "record.jsonl", behavior),
        )
    )

    gated = {check.name: check for check in result.comparison.gated()}
    assert gated["vad_stop_to_final_ms.count"].passed
    assert gated["vad_stop_to_final_ms.p50"].replay == gated["vad_stop_to_final_ms.p50"].original
    assert gated["speaker_bound.of"].passed
    replayed_syntheses = {
        (entry["transcript_id"], entry.get("sentence_index") or 0): entry
        for entry in events(result, "synthesis_finished")
        if entry.get("transcript_id")
    }
    matched = [key for key in fixture.syntheses if key in replayed_syntheses]
    assert len(matched) >= 15
    for key in matched:
        recorded = fixture.syntheses[key]
        assert replayed_syntheses[key]["first_chunk_ms"] == pytest.approx(recorded.first_chunk_ms)


def test_the_twins_own_chat_is_not_heard_as_a_message() -> None:
    rows, entries = recorded_conversation()
    own = ChatEntry(
        at=START + timedelta(seconds=5),
        seq=9,
        ref="chat#1",
        message_id="twin-1",
        sender_id="twin",
        sender="Harun",
        text="I am Harun's AI twin.",
        sent_at=START + timedelta(seconds=5),
        own=True,
        backlog=False,
    )

    items = schedule(build_fixture(rows, [*entries, own]))

    assert not [item for item in items if item.kind == "chat"]


def test_a_slower_reply_model_moves_the_first_audio_by_the_scaled_latency(
    settings: Settings, tmp_path: Path
) -> None:
    def first_pcm(scale: float) -> float:
        result = run_virtual(
            lambda: replay_session(
                conversation(),
                settings,
                config(settings),
                ReplayOptions(
                    tmp_path / f"record-{scale}.jsonl",
                    RecordedBehavior(twin_names=("Harun",), wake_phrases=("Harun",)),
                    reply_latency_scale=scale,
                ),
            )
        )
        [pcm] = [
            entry for entry in events(result, "synthesis_pcm_emitted") if entry.get("transcript_id")
        ]
        return offset(pcm)

    assert first_pcm(1.0) - first_pcm(0.5) == pytest.approx(0.4)


def test_a_recorded_state_question_is_answered_from_the_kernel_without_the_model(
    settings: Settings, tmp_path: Path
) -> None:
    rows, entries = recorded_conversation()
    asking = [
        {**entry, "state_question": "participant_count"}
        if entry["event"] == "policy_decided"
        else entry
        for entry in rows
        if entry["event"] not in ("request_started", "answer_sentence_emitted", "request_finished")
    ]

    result = replayed(build_fixture(asking, entries), settings, tmp_path)

    [selected] = events(result, "state_answer_selected")
    assert selected["question"] == "participant_count"
    assert not events(result, "request_started")
    spoken = [
        entry.text
        for entry in load_meeting_record(tmp_path / "record.jsonl")
        if isinstance(entry, TwinSpeechEntry)
    ]
    assert spoken == [render(Unknown("participant_count", "stale_source"), "id")]


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_oue_4_gate_passes_and_fails_when_the_reply_model_halves_its_latency(
    settings: Settings, tmp_path: Path
) -> None:
    fixture = load_fixture(
        RUNTIME / "journals/live-oue-4.jsonl", RUNTIME / "meetings/live-oue-4/record.jsonl"
    )
    behavior = recorded_behavior(RUNTIME / "meetings/live-oue-4/session.json")

    def gate(scale: float) -> bool:
        options = ReplayOptions(tmp_path / f"record-{scale}.jsonl", behavior, scale)
        result = run_virtual(lambda: replay_session(fixture, settings, config(settings), options))
        return result.comparison.passed

    assert gate(1.0)
    assert not gate(0.5)
