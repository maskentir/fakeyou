import asyncio
from typing import Any

import pytest

from evals._speech_recording import (
    MAX_AUDIO_SECONDS,
    MAX_WALL_SECONDS,
    MUSE_AUDIO_ENCODING,
    MUSE_MODEL,
    PCM_BYTES_PER_SECOND,
    VOICE_END,
    SessionRefused,
    SpeechRecording,
    SpeechSession,
    SpeechStopped,
)

CLIP = "review-token.wav"
HANDSHAKE: dict[str, Any] = {"model": MUSE_MODEL, "audioEncoding": MUSE_AUDIO_ENCODING}
COMPLETE: list[dict[str, Any]] = [
    {"type": "speechStart", "turnId": 0},
    {"type": "transcript", "transcript": " Rani", "final": False},
    {"type": VOICE_END},
    {"type": "speechComplete", "turnId": 0, "transcript": " Rani Twin, read the token. "},
]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def recording(
    *,
    sessions: int = 1,
    max_audio_seconds: float = MAX_AUDIO_SECONDS,
    max_wall_seconds: float = MAX_WALL_SECONDS,
) -> tuple[SpeechRecording, Clock]:
    clock = Clock()
    return (
        SpeechRecording(
            frozenset({CLIP}),
            max_sessions=sessions,
            max_audio_seconds=max_audio_seconds,
            max_wall_seconds=max_wall_seconds,
            handover_seconds=0.05,
            clock=clock,
        ),
        clock,
    )


def latest(speech: SpeechRecording) -> SpeechSession:
    return speech.sessions[-1]


async def opened(**limits: Any) -> tuple[SpeechRecording, Clock]:
    speech, clock = recording(**limits)
    await speech.open_session(HANDSHAKE)
    return speech, clock


async def reconnect(speech: SpeechRecording, keywords: tuple[str, ...] = ()) -> None:
    speech.worker_closing(latest(speech))
    speech.provider_closed(latest(speech), 1000, expected=True)
    await speech.open_session({**HANDSHAKE, "keywords": list(keywords)})


def test_the_session_limit_is_explicit_and_bounded() -> None:
    for sessions in (0, 5):
        with pytest.raises(ValueError, match="1 to 4 sessions"):
            SpeechRecording(frozenset({CLIP}), max_sessions=sessions)


async def test_a_single_session_recording_refuses_a_second_session() -> None:
    speech, _clock = await opened()

    with pytest.raises(SpeechStopped, match="session_limit"):
        await reconnect(speech)


@pytest.mark.parametrize(
    ("handshake", "reason"),
    [
        ({**HANDSHAKE, "model": "other"}, "unexpected_handshake"),
        ({**HANDSHAKE, "audioEncoding": "OPUS"}, "unsupported_audio"),
    ],
)
async def test_an_unexpected_handshake_is_refused(handshake: dict[str, Any], reason: str) -> None:
    speech, _clock = recording()

    with pytest.raises(SpeechStopped, match=reason):
        await speech.open_session(handshake)


async def test_cleanly_closed_sessions_hand_over_up_to_the_limit() -> None:
    speech, _clock = await opened(sessions=4)

    for keywords in (("Orchid",), ("Orchid", "Launch"), ("Orchid", "Launch", "Plan")):
        await reconnect(speech, keywords)

    with pytest.raises(SpeechStopped, match="session_limit"):
        await reconnect(speech)
    report: Any = speech.report()
    assert [cycle["keywords"] for cycle in report["session_lifecycles"]] == [0, 1, 2, 3]
    assert [cycle["ending"] for cycle in report["session_lifecycles"]] == ["clean"] * 4
    assert (report["sessions"], report["stop_reason"]) == (4, "session_limit")


async def test_a_session_that_is_still_open_cannot_be_joined_by_another() -> None:
    speech, _clock = await opened(sessions=4)

    with pytest.raises(SpeechStopped, match="concurrent_session"):
        await speech.open_session(HANDSHAKE)


async def test_a_closing_session_hands_over_once_it_has_closed() -> None:
    speech, _clock = await opened(sessions=2)
    speech.worker_closing(latest(speech))

    joining = asyncio.ensure_future(speech.open_session(HANDSHAKE))
    await asyncio.sleep(0)
    waited = not joining.done()
    speech.provider_closed(latest(speech), 1000, expected=True)
    second = await joining

    assert waited
    assert (second.index, second.closing) == (2, False)


async def test_a_closing_session_that_never_closes_stops_the_recording() -> None:
    speech, _clock = await opened(sessions=2)
    speech.worker_closing(latest(speech))

    with pytest.raises(SpeechStopped, match="concurrent_session"):
        await speech.open_session(HANDSHAKE)


async def test_audio_accumulates_across_sessions() -> None:
    speech, _clock = await opened(sessions=2, max_audio_seconds=1.0)
    speech.admit_audio(latest(speech), bytes(PCM_BYTES_PER_SECOND // 2))
    await reconnect(speech)

    speech.admit_audio(latest(speech), bytes(PCM_BYTES_PER_SECOND // 4))
    with pytest.raises(SpeechStopped, match="audio_limit"):
        speech.admit_audio(latest(speech), bytes(PCM_BYTES_PER_SECOND // 2))

    assert speech.report()["audio_seconds"] == 0.75


async def test_the_deadline_accumulates_across_sessions() -> None:
    speech, clock = await opened(sessions=3, max_wall_seconds=10.0)
    clock.now += 6.0
    await reconnect(speech)
    clock.now += 4.0

    with pytest.raises(SpeechStopped, match="deadline"):
        speech.admit_audio(latest(speech), bytes(2))
    with pytest.raises(SpeechStopped, match="deadline"):
        await reconnect(speech)


async def test_odd_length_audio_is_not_16_bit_pcm() -> None:
    speech, _clock = await opened()

    with pytest.raises(SpeechStopped, match="unsupported_audio"):
        speech.admit_audio(latest(speech), bytes(3))


async def test_an_unexpected_close_in_a_later_session_is_fatal_and_final() -> None:
    speech, _clock = await opened(sessions=4)
    await reconnect(speech)

    with pytest.raises(SpeechStopped, match="provider_closed"):
        speech.provider_closed(latest(speech), 1011, expected=False)
    with pytest.raises(SpeechStopped, match="provider_closed"):
        await speech.open_session(HANDSHAKE)
    report: Any = speech.report()
    assert [cycle["ending"] for cycle in report["session_lifecycles"]] == [
        "clean",
        "provider_closed",
    ]
    assert report["session_lifecycles"][1]["close_code"] == 1011


async def test_a_provider_error_forbids_any_further_session() -> None:
    speech, _clock = await opened(sessions=4)
    await reconnect(speech)

    with pytest.raises(SpeechStopped, match="provider_error"):
        speech.provider_message({"type": "error", "message": "unauthorized"})
    with pytest.raises(SpeechStopped, match="provider_error"):
        await speech.open_session(HANDSHAKE)

    assert speech.report()["sessions"] == 2


async def test_only_the_first_take_of_an_allowed_clip_is_recorded() -> None:
    speech, _clock = await opened(sessions=2)
    speech.admit_claim(CLIP, 1)
    await reconnect(speech)

    with pytest.raises(SpeechStopped, match="additional_take"):
        speech.admit_claim(CLIP, 1)
    other, _other_clock = await opened()
    with pytest.raises(SpeechStopped, match="unexpected_clip"):
        other.admit_claim("beta-question.wav", 1)


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [message for message in COMPLETE if message["type"] != "speechComplete"],
        [message for message in COMPLETE if message["type"] != VOICE_END],
        [*COMPLETE[:-1], {"type": "speechComplete", "turnId": 0, "transcript": "  "}],
    ],
    ids=["never_heard", "no_completion", "no_voice_end", "empty_completion"],
)
async def test_an_incomplete_take_is_refused(messages: list[dict[str, Any]]) -> None:
    speech, _clock = await opened()
    if messages:
        speech.admit_claim(CLIP, 1)

    with pytest.raises(SpeechStopped, match="incomplete_transcript"):
        speech.verify({(CLIP, 1): messages})


async def test_a_complete_take_is_accepted_and_priced_by_audio_minute() -> None:
    speech, clock = await opened(sessions=2)
    speech.admit_audio(latest(speech), bytes(PCM_BYTES_PER_SECOND * 30))
    clock.now += 31.0
    await reconnect(speech)
    speech.admit_claim(CLIP, 1)
    speech.admit_audio(latest(speech), bytes(PCM_BYTES_PER_SECOND * 30))
    clock.now += 30.0

    speech.verify({(CLIP, 1): COMPLETE})
    speech.worker_closing(latest(speech))
    speech.provider_closed(latest(speech), 1000, expected=True)
    report: Any = speech.report()

    assert report["estimated_usd"] == pytest.approx(0.003)
    assert report["recorded"] == [f"{CLIP} take 1"]
    assert report["wall_seconds"] == 61.0
    assert [cycle["opened_s"] for cycle in report["session_lifecycles"]] == [0.0, 31.0]
    assert report["stop_reason"] is None


async def test_a_second_session_is_refused_at_once_even_if_the_first_closes_soon_after() -> None:
    speech, _clock = await opened(sessions=4)

    joining = asyncio.ensure_future(speech.open_session(HANDSHAKE))
    await asyncio.sleep(0)
    speech.worker_closing(latest(speech))
    speech.provider_closed(latest(speech), 1000, expected=True)

    with pytest.raises(SpeechStopped, match="concurrent_session"):
        await joining


async def test_only_one_waiting_connection_takes_over_a_closing_session() -> None:
    speech, _clock = await opened(sessions=4)
    first = latest(speech)
    speech.worker_closing(first)

    taking = asyncio.ensure_future(speech.open_session(HANDSHAKE))
    competing = asyncio.ensure_future(speech.open_session(HANDSHAKE))
    await asyncio.sleep(0)
    speech.provider_closed(first, 1000, expected=True)
    replacement = await taking

    with pytest.raises(SessionRefused):
        await competing
    assert [session.index for session in speech.sessions] == [1, 2]
    assert replacement is speech.current
    assert speech.refused_claimants == 1
    assert speech.stop_reason is None


async def test_a_late_close_of_an_earlier_session_leaves_the_replacement_open() -> None:
    speech, _clock = await opened(sessions=4)
    first = latest(speech)
    await reconnect(speech)
    replacement = latest(speech)

    speech.worker_closing(first)
    speech.provider_closed(first, 1000, expected=True)
    speech.provider_closed(first, 1011, expected=False)

    assert speech.current is replacement
    assert (replacement.closing, replacement.closed_at, replacement.ending) == (False, None, None)
    assert speech.stop_reason is None
    with pytest.raises(SpeechStopped, match="concurrent_session"):
        await speech.open_session(HANDSHAKE)


async def test_audio_for_an_earlier_session_is_never_counted_for_the_replacement() -> None:
    speech, _clock = await opened(sessions=4)
    first = latest(speech)
    first.pcm_bytes = 64
    await reconnect(speech)

    with pytest.raises(SpeechStopped, match="concurrent_session"):
        speech.admit_audio(first, bytes(PCM_BYTES_PER_SECOND))

    assert latest(speech).pcm_bytes == 0
    assert first.pcm_bytes == 64


async def test_a_late_close_of_an_earlier_session_does_not_wake_the_next_handover() -> None:
    speech, _clock = await opened(sessions=4)
    first = latest(speech)
    await reconnect(speech)
    second = latest(speech)
    speech.worker_closing(second)

    waiting = asyncio.ensure_future(speech.open_session(HANDSHAKE))
    await asyncio.sleep(0)
    speech.provider_closed(first, 1000, expected=True)
    await asyncio.sleep(0)
    woke_early = waiting.done()
    speech.provider_closed(second, 1000, expected=True)
    third = await waiting

    assert not woke_early
    assert [session.index for session in speech.sessions] == [1, 2, 3]
    assert third is speech.current
