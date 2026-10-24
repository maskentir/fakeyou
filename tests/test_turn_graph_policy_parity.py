import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation import participation_graph, turn_ingress, turn_lifecycle
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.conversation.turn_ingress import PolicyOptions, TurnIngress
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent
from tests._audio_fakes import wait_until

INGRESS_SOURCE = Path(turn_ingress.__file__).read_text(encoding="utf-8")
PARTICIPATION_SOURCE = Path(participation_graph.__file__).read_text(encoding="utf-8")
BARGE_NAMES = ("barge_in_detected", "agent_cancel_requested", "agent_cancelled")


class Journal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append({"source": source, **fields})


class Audio:
    def __init__(self) -> None:
        self.utterances: list[str] = []
        self.stops = 0
        self.is_speaking = False

    async def speak(self, text: str, **_: Any) -> None:
        self.utterances.append(text)

    async def stop_playback(self) -> None:
        self.stops += 1

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class Responder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


def make_ingress(
    journal: Journal,
    *,
    addressing_names: tuple[str, ...] = ("twin",),
    participant_policy: bool = False,
    others_speaking: Callable[[], bool] | None = None,
    barge_in: bool = True,
) -> tuple[TurnIngress, TurnLifecycle, Audio]:
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=None, window_turns=3),
        journal=journal,
        responder=Responder(),
    )
    lifecycle.configure_barge_in(enabled=barge_in)
    audio = Audio()
    lifecycle.bind(audio)
    ingress = TurnIngress(lifecycle, addressing_names=addressing_names, wake_phrases=("twin",))
    ingress.configure_policy(
        PolicyOptions(enabled=participant_policy, others_speaking=others_speaking)
    )
    return ingress, lifecycle, audio


def final(event_id: str, text: str, participant_id: str = "p1") -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def policy_reasons(journal: Journal) -> list[str]:
    return [
        str(fields["reason"])
        for name, fields in zip(journal.names, journal.fields, strict=True)
        if name == "policy_decided"
    ]


async def start_speaking(lifecycle: TurnLifecycle) -> asyncio.Task[bool]:
    speech = asyncio.create_task(lifecycle.floor.hold(wait_until(asyncio.Event())))
    await asyncio.sleep(0)
    return speech


async def stop_speaking(speech: asyncio.Task[bool]) -> None:
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_policy_addresses_only_a_leading_vocative_like_the_loop() -> None:
    journal = Journal()
    ingress, _, audio = make_ingress(journal, participant_policy=True)

    late = await ingress.handle_final(final("f1", "are you there, twin?"))
    leading = await ingress.handle_final(final("f2", "twin, are you there?"))

    assert late is not None
    assert late.decision == "silent"
    assert leading is not None
    assert leading.decision == "speak"
    assert audio.utterances == ["Yes, I am here."]


async def test_policy_unknown_speaker_is_not_the_engaged_speaker_like_the_loop() -> None:
    journal = Journal()
    ingress, _, _ = make_ingress(journal, participant_policy=True)

    admitted = await ingress.handle_final(final("f1", "twin, hi there", participant_id="p1"))
    followup = await ingress.handle_final(
        final("f2", "and what about lunch?", participant_id="unknown")
    )

    assert admitted is not None
    assert admitted.decision == "speak"
    assert followup is not None
    assert followup.decision == "silent"


async def test_policy_others_speaking_silences_an_addressed_final_like_the_loop() -> None:
    journal = Journal()
    ingress, _, audio = make_ingress(journal, participant_policy=True, others_speaking=lambda: True)

    decision = await ingress.handle_final(final("f1", "twin, are you there?"))

    assert decision is not None
    assert decision.decision == "silent"
    assert policy_reasons(journal) == ["floor_occupied"]
    assert audio.utterances == []


async def test_others_speaking_is_ignored_without_policy_like_the_loop() -> None:
    journal = Journal()
    ingress, _, audio = make_ingress(journal, others_speaking=lambda: True)

    decision = await ingress.handle_final(final("f1", "twin, are you there?"))

    assert decision is not None
    assert decision.decision == "speak"
    assert audio.utterances == ["Yes, I am here."]


async def test_others_speaking_is_read_for_each_final() -> None:
    journal = Journal()
    answers = [True, False]
    ingress, _, audio = make_ingress(
        journal, participant_policy=True, others_speaking=lambda: answers.pop(0)
    )

    first = await ingress.handle_final(final("f1", "twin, hello?"))
    second = await ingress.handle_final(final("f2", "twin, are you there?"))

    assert first is not None
    assert first.decision == "silent"
    assert second is not None
    assert second.decision == "speak"
    assert audio.utterances == ["Yes, I am here."]


async def test_barge_in_off_ignores_an_interim_during_speech() -> None:
    journal = Journal()
    ingress, lifecycle, audio = make_ingress(journal, addressing_names=(), barge_in=False)
    speech = await start_speaking(lifecycle)

    await ingress.handle_interim("please stop talking now")

    assert not speech.done()
    assert [name for name in journal.names if name in BARGE_NAMES] == []
    assert audio.stops == 0
    await stop_speaking(speech)


async def test_barge_in_off_ignores_an_unaddressed_final_during_speech() -> None:
    journal = Journal()
    ingress, lifecycle, audio = make_ingress(journal, barge_in=False)
    speech = await start_speaking(lifecycle)

    decision = await ingress.handle_final(final("f1", "Alice, lunch at noon today?"))

    assert decision is not None
    assert decision.decision == "silent"
    assert [name for name in journal.names if name in BARGE_NAMES] == []
    assert audio.stops == 0
    assert not speech.done()
    await stop_speaking(speech)


async def test_reply_mode_maps_sentence_streaming_to_the_graph_mode() -> None:
    assert DEFAULTS.conversation.sentence_streaming is True
    assert (
        turn_lifecycle.reply_mode(sentence_streaming=DEFAULTS.conversation.sentence_streaming)
        == "streamed"
    )
    assert turn_lifecycle.reply_mode(sentence_streaming=False) == "whole_turn"
    assert turn_lifecycle.reply_mode(sentence_streaming=True) == "streamed"


def test_policy_literals_are_in_the_graph_ingress_and_participation_source() -> None:
    ingress_literals = (
        "return is_vocative(text, self._wake_phrases)",
        "others_speaking=self._others_speaking(),",
        "return self._participant_policy and signal is not None and signal()",
        "return bool(self._addressing_names) or self._participant_policy",
        "return same_observed_speaker(self._engaged_speaker, event.participant_id)",
        "if bare_ack or self._wake_phrases or not lifecycle.barge_in or session is None:",
        "or not lifecycle.barge_in\n",
    )
    for literal in ingress_literals:
        assert literal in INGRESS_SOURCE, literal
    for literal in ('"policy_decided"', 'f"rules_fallback:{rule.reason_code}"'):
        assert literal in PARTICIPATION_SOURCE, literal
