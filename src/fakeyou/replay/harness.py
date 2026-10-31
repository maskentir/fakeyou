import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fakeyou.audio.playback import PlaybackEngine, PlaybackEvent
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.settings import Settings
from fakeyou.conversation.meeting_turns import MeetingTurns
from fakeyou.conversation.session import MeetingAudio, SessionConfig
from fakeyou.foundation.clock import using_clock
from fakeyou.foundation.transcript import (
    AttentionContext,
    TranscriptEvent,
    TranscriptUpdate,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.assembly import (
    AgentWiring,
    ModelWiring,
    TurnBehavior,
    open_conversation,
)
from fakeyou.orchestration.transcript import (
    DrainOptions,
    TranscriptDrain,
    TranscriptSinks,
    build_transcript_callback,
)
from fakeyou.persistence.record import ChatEntry, MeetingRecord, ScreenEntry, TranscriptEntry
from fakeyou.replay.compare import Comparison, compare_journals
from fakeyou.replay.fixture import ReplayFixture
from fakeyou.replay.pacer import create_pacer_backend
from fakeyou.replay.recorded import RecordedJudge, RecordedModel, RecordedSynthesizer

LANES = {
    "vad": "audio",
    "interim": "speech",
    "final": "speech",
    "chat": "meeting",
    "attention": "meeting",
}


@dataclass(frozen=True, slots=True)
class ReplayInput:
    offset: float
    kind: str
    text: str = ""
    entry: TranscriptEntry | ChatEntry | ScreenEntry | None = None
    active: bool = False


@dataclass(frozen=True, slots=True)
class RecordedBehavior:
    twin_names: tuple[str, ...] = ()
    wake_phrases: tuple[str, ...] = ()
    barge_in: bool = False
    participant_policy_enabled: bool = False

    def turn_behavior(self) -> TurnBehavior:
        return TurnBehavior(
            on_transition=lambda *_args: None,
            wake_phrases=self.wake_phrases,
            twin_names=self.twin_names,
            barge_in=self.barge_in,
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    rows: tuple[dict[str, Any], ...]
    comparison: Comparison


@dataclass(slots=True)
class _Speaking:
    text: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)


class EngineAudio:
    def __init__(self, engine: PlaybackEngine) -> None:
        self._engine = engine
        self._cues: asyncio.Task[None] | None = None

    def prepare_cues(self, texts: Sequence[str]) -> None:
        if self._cues is None:
            self._cues = asyncio.create_task(self._prepare_cues(tuple(texts)))

    async def _prepare_cues(self, texts: tuple[str, ...]) -> None:
        for text in texts:
            await self._engine.prepare_cue(text)

    async def play_cue(self, text: str) -> bool:
        return await self._engine.play_cue(text)

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        await self._engine.speak(
            text, sentence_index=sentence_index, transcript_id=transcript_id, language=language
        )

    async def stop_playback(self) -> None:
        await self._engine.stop()

    @property
    def is_speaking(self) -> bool:
        return self._engine.is_speaking

    async def stop(self) -> None:
        if self._cues is not None:
            self._cues.cancel()
        await self._engine.close()

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        return None


def recorded_behavior(session_path: Path) -> RecordedBehavior:
    session = json.loads(session_path.read_text(encoding="utf-8"))
    persona = session.get("persona") or {}
    name = session.get("twin_name") or persona.get("display_name")
    phrases = str(session.get("wake_phrase") or "")
    return RecordedBehavior(
        twin_names=(name,) if isinstance(name, str) and name else (),
        wake_phrases=tuple(phrase.strip() for phrase in phrases.split(",") if phrase.strip()),
        barge_in=bool(session.get("barge_in")),
        participant_policy_enabled=bool(session.get("participant_policy_enabled")),
    )


def schedule(fixture: ReplayFixture, *, attention: bool = False) -> tuple[ReplayInput, ...]:
    items: list[ReplayInput] = [
        ReplayInput(edge.offset, "vad", active=edge.active) for edge in fixture.edges
    ]
    for final in fixture.finals:
        items.extend(
            _interims(
                final.offset, final.entry.text, final.interims, final.first_interim_to_final_ms
            )
        )
        items.append(ReplayInput(final.offset, "final", text=final.entry.text, entry=final.entry))
    for percept in fixture.percepts:
        if isinstance(percept.entry, ChatEntry):
            if percept.entry.own or percept.entry.backlog:
                continue
            items.append(ReplayInput(percept.offset, "chat", entry=percept.entry))
        elif attention:
            items.append(ReplayInput(percept.offset, "attention", entry=percept.entry))
    return tuple(sorted(items, key=lambda item: item.offset))


def _interims(
    final_offset: float, text: str, count: int, first_interim_to_final_ms: float | None
) -> list[ReplayInput]:
    if count <= 0:
        return []
    window = (first_interim_to_final_ms or 0.0) / 1000
    words = text.split()
    items: list[ReplayInput] = []
    for index in range(1, count + 1):
        offset = final_offset - window * (count - index + 1) / count
        size = max(1, round(len(words) * index / (count + 1))) if words else index
        prefix = " ".join(words[:size]) if words else text[:size]
        items.append(ReplayInput(offset, "interim", text=prefix))
    return items


async def replay_session(
    fixture: ReplayFixture,
    settings: Settings,
    config: SessionConfig,
    *,
    behavior: RecordedBehavior | None = None,
    record_path: Path,
) -> ReplayResult:
    loop = asyncio.get_running_loop()
    with using_clock(loop.time):
        return await _replay(fixture, settings, config, behavior, record_path)


async def _replay(
    fixture: ReplayFixture,
    settings: Settings,
    config: SessionConfig,
    behavior: RecordedBehavior | None,
    record_path: Path,
) -> ReplayResult:
    loop = asyncio.get_running_loop()
    rows: list[dict[str, Any]] = []
    started = fixture.started_at

    def wall_clock() -> datetime:
        return started + timedelta(seconds=loop.time())

    journal = Journal(
        fixture.meeting_id,
        emit=lambda line: rows.append(json.loads(line)),
        now=wall_clock,
        echo=False,
    )
    record = MeetingRecord(record_path, journal=journal, now=wall_clock)
    engine = PlaybackEngine(
        RecordedSynthesizer(fixture.syntheses, faults=fixture.synthesis_faults, journal=journal),
        create_pacer_backend(),
        on_event=_playback_journal(journal, record),
        diagnostics=Diagnostics(journal=journal),
    )
    audio: MeetingAudio = EngineAudio(engine)
    wiring = ModelWiring(
        reply=lambda: RecordedModel(fixture.replies, fixture.plans),
        judge=lambda _names: RecordedJudge(fixture.verdicts, fixture.finals, origin=loop.time()),
    )
    try:
        async with open_conversation(
            settings,
            config,
            agent=AgentWiring(history=record),
            behavior=(behavior or RecordedBehavior()).turn_behavior(),
            diagnostics=Diagnostics(journal=journal),
            models=wiring,
        ) as turns:
            turns.bind(audio)
            turns.resume()
            drain = build_transcript_callback(
                sinks=TranscriptSinks(journal=journal, record=record, conversation=turns.ingress),
                binder=SlotBinder(),
                voice_activity=None,
                options=DrainOptions(
                    per_stream_enabled=config.per_stream_enabled,
                    early_hold_seconds=(
                        settings.turn_judge.commit_hold_seconds
                        if settings.turn_judge.early_commit_enabled
                        else None
                    ),
                ),
            )
            try:
                inputs = schedule(fixture, attention=config.attention_enabled)
                await _feed(inputs, drain, turns, journal)
                await turns.lifecycle.wait_idle()
            finally:
                await drain.close()
    finally:
        await audio.stop()
        record.close()
    return ReplayResult(tuple(rows), compare_journals(list(fixture.rows), rows))


def _playback_journal(journal: Journal, record: MeetingRecord) -> Callable[[PlaybackEvent], None]:
    speaking = _Speaking()

    def observe(event: PlaybackEvent) -> None:
        if event.cue:
            journal.record(
                event.kind.replace("playback_", "thinking_cue_"),
                source="playback",
                detail=event.detail,
                cue_text=event.text,
            )
            return
        utterance = event.utterance
        journal.record(
            event.kind,
            source="playback",
            ends_with_question=event.text.rstrip().endswith("?"),
            detail=event.detail,
            transcript_id=utterance.transcript_id,
            sentence_index=utterance.sentence_index,
        )
        if event.kind == "playback_started":
            speaking.text = event.text
        elif event.kind == "playback_finished":
            speaking.text = None
            record.add_twin_speech(event.text, causation_id=utterance.transcript_id)
        elif event.kind == "playback_dropped" and event.text == speaking.text:
            speaking.text = None
            record.add_twin_speech(
                event.text, interrupted=True, causation_id=utterance.transcript_id
            )

    return observe


async def _feed(
    inputs: tuple[ReplayInput, ...],
    drain: TranscriptDrain,
    turns: MeetingTurns,
    journal: Journal,
) -> None:
    origin = asyncio.get_running_loop().time()
    lanes = {
        lane: [item for item in inputs if LANES[item.kind] == lane] for lane in set(LANES.values())
    }
    async with asyncio.TaskGroup() as group:
        for items in lanes.values():
            group.create_task(_play_lane(items, origin, drain, turns, journal))


async def _play_lane(
    items: list[ReplayInput],
    origin: float,
    drain: TranscriptDrain,
    turns: MeetingTurns,
    journal: Journal,
) -> None:
    loop = asyncio.get_running_loop()
    for item in items:
        delay = origin + item.offset - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        await _deliver(item, drain, turns, journal)


async def _deliver(
    item: ReplayInput, drain: TranscriptDrain, turns: MeetingTurns, journal: Journal
) -> None:
    entry = item.entry
    match item.kind:
        case "vad":
            journal.record("speech_started" if item.active else "speech_stopped", source="audio")
            await drain.on_vad(active=item.active)
        case "interim":
            await drain.on_interim(TranscriptUpdate(text=item.text, final=False))
        case "final" if isinstance(entry, TranscriptEntry):
            await drain(
                TranscriptUpdate(
                    text=entry.text,
                    final=True,
                    timestamp=entry.spoken_at,
                    stream_id=entry.stream_id,
                )
            )
        case "chat" if isinstance(entry, ChatEntry):
            turns.receive_chat(_chat_event(entry))
        case "attention" if isinstance(entry, ScreenEntry):
            turns.receive_attention(_attention_event(entry), channel="voice")
        case _:
            pass


def _chat_event(entry: ChatEntry) -> TranscriptEvent:
    return TranscriptEvent(
        id=entry.ref,
        timestamp=entry.sent_at.isoformat(),
        source="meeting_chat",
        text=entry.text,
        finalized=True,
        participant_id=entry.sender_id,
        speaker_evidence_id=None,
        display_claim=entry.sender or None,
    )


def _attention_event(entry: ScreenEntry) -> TranscriptEvent:
    points = "; ".join(entry.key_points[:2])
    seen = f"{entry.title}: {points}" if points else entry.title
    context = AttentionContext(
        kind="screen",
        trigger_text=f"The shared screen is now {seen}.",
        trigger_event_id=entry.ref,
    )
    return TranscriptEvent(
        id=f"attention-{entry.ref}",
        timestamp=entry.shown_at.isoformat(),
        source="meeting_attention",
        text=context.trigger_text,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        attention=context,
    )
