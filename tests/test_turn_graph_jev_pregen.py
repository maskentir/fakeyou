from collections.abc import AsyncIterator, Sequence
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.jev_refresh import JevCache, JevRefreshInput, jev_cache_key
from fakeyou.conversation.participation_decider import DeciderTurn, ParticipationDecision
from fakeyou.conversation.participation_graph import DeciderFailure
from fakeyou.conversation.reply_planner import SpokenChunk


class RecordingDecider:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.windows: list[Sequence[DeciderTurn]] = []

    async def decide(self, window: Sequence[DeciderTurn]) -> ParticipationDecision:
        self.windows.append(window)
        if self._fail:
            raise TimeoutError("decider too slow")
        return ParticipationDecision(decision="speak", confidence=0.9, reason_code="addressed")


def turn(text: str) -> DeciderTurn:
    return DeciderTurn(speaker="alice", text=text, mine=False)


def refresh_input(
    event_ids: tuple[str, ...], *, cue: str = "explicit", mine: bool = True, window_turns: int = 3
) -> JevRefreshInput:
    turns = tuple(turn(f"text {event_id}") for event_id in event_ids)
    return JevRefreshInput(
        event_ids=event_ids, turns=turns, cue=cue, mine=mine, window_turns=window_turns
    )


async def test_jev_refresh_decides_once_while_the_inputs_are_unchanged() -> None:
    decider = RecordingDecider()
    cache = JevCache()
    request = refresh_input(("e1", "e2"))

    first = await cache.refresh(decider, request)
    second = await cache.refresh(decider, request)

    assert first == "decided"
    assert second == "skipped"
    assert len(decider.windows) == 1


async def test_jev_refresh_decides_again_when_the_window_changes() -> None:
    decider = RecordingDecider()
    cache = JevCache()

    await cache.refresh(decider, refresh_input(("e1", "e2")))
    outcome = await cache.refresh(decider, refresh_input(("e1", "e2", "e3")))

    assert outcome == "decided"
    assert len(decider.windows) == 2


async def test_jev_refresh_decides_again_when_the_cue_or_mine_changes() -> None:
    decider = RecordingDecider()
    cache = JevCache()

    await cache.refresh(decider, refresh_input(("e1",), cue="explicit", mine=True))
    cue_changed = await cache.refresh(decider, refresh_input(("e1",), cue="soft", mine=True))
    mine_changed = await cache.refresh(decider, refresh_input(("e1",), cue="soft", mine=False))

    assert cue_changed == "decided"
    assert mine_changed == "decided"
    assert len(decider.windows) == 3


async def test_jev_refresh_keeps_only_the_window_turns_the_decider_sees() -> None:
    decider = RecordingDecider()
    cache = JevCache()

    await cache.refresh(decider, refresh_input(("e1", "e2", "e3", "e4"), window_turns=2))

    assert [item["text"] for item in decider.windows[0]] == ["text e3", "text e4"]


async def test_jev_refresh_records_a_failure_and_retries_on_the_next_refresh() -> None:
    decider = RecordingDecider(fail=True)
    cache = JevCache()
    request = refresh_input(("e1",))

    first = await cache.refresh(decider, request)
    second = await cache.refresh(decider, request)

    assert first == "failed"
    assert second == "failed"
    assert len(decider.windows) == 2
    assert cache.decision(jev_cache_key(request)) is None
    failure = cache.failure(jev_cache_key(request))
    assert isinstance(failure, DeciderFailure)
    assert failure.code == "decider_timeout"


async def test_jev_refresh_stores_the_decision_under_the_input_key() -> None:
    decider = RecordingDecider()
    cache = JevCache()
    request = refresh_input(("e1",))

    await cache.refresh(decider, request)

    decision = cache.decision(jev_cache_key(request))
    assert decision is not None
    assert decision.decision == "speak"


class RecordingResponder:
    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        self.transcripts.append(transcript)
        yield SpokenChunk(text="Fresh one.", language=DEFAULTS.speech.language)


class RecordingSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)


class RecordingJournal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append({"source": source, **fields})
