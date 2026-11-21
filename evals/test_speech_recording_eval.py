from typing import Any

import pytest

from evals._speech_recording import (
    MAX_AUDIO_SECONDS,
    MAX_WALL_SECONDS,
    MUSE_AUDIO_ENCODING,
    MUSE_MODEL,
    PCM_BYTES_PER_SECOND,
    VOICE_END,
    SpeechRecording,
    SpeechStopped,
)

CLIP = "review-token.wav"
HANDSHAKE = {"model": MUSE_MODEL, "audioEncoding": MUSE_AUDIO_ENCODING}
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


def opened(
    max_audio_seconds: float = MAX_AUDIO_SECONDS, max_wall_seconds: float = MAX_WALL_SECONDS
) -> tuple[SpeechRecording, Clock]:
    clock = Clock()
    recording = SpeechRecording(
        frozenset({CLIP}),
        max_audio_seconds=max_audio_seconds,
        max_wall_seconds=max_wall_seconds,
        clock=clock,
    )
    recording.open_session(HANDSHAKE)
    return recording, clock


def test_only_one_session_with_the_expected_model_and_audio_is_opened() -> None:
    recording, _clock = opened()

    with pytest.raises(SpeechStopped, match="session_limit"):
        recording.open_session(HANDSHAKE)
    for handshake, reason in (
        ({**HANDSHAKE, "model": "other"}, "unexpected_handshake"),
        ({**HANDSHAKE, "audioEncoding": "OPUS"}, "unsupported_audio"),
    ):
        with pytest.raises(SpeechStopped, match=reason):
            SpeechRecording(frozenset({CLIP})).open_session(handshake)


def test_audio_is_counted_and_stops_at_its_limit() -> None:
    recording, _clock = opened(max_audio_seconds=1.0)

    recording.admit_audio(bytes(PCM_BYTES_PER_SECOND // 2))
    with pytest.raises(SpeechStopped, match="audio_limit"):
        recording.admit_audio(bytes(PCM_BYTES_PER_SECOND))
    with pytest.raises(SpeechStopped, match="audio_limit"):
        recording.admit_audio(bytes(2))

    assert recording.report()["audio_seconds"] == 0.5


def test_odd_length_audio_is_not_16_bit_pcm() -> None:
    recording, _clock = opened()

    with pytest.raises(SpeechStopped, match="unsupported_audio"):
        recording.admit_audio(bytes(3))


def test_the_wall_clock_deadline_stops_the_session() -> None:
    recording, clock = opened(max_wall_seconds=5.0)
    recording.admit_audio(bytes(2))

    clock.now += 5.0

    with pytest.raises(SpeechStopped, match="deadline"):
        recording.admit_audio(bytes(2))


def test_only_the_first_take_of_an_allowed_clip_is_recorded() -> None:
    recording, _clock = opened()

    recording.admit_claim(CLIP, 1)

    with pytest.raises(SpeechStopped, match="additional_take"):
        recording.admit_claim(CLIP, 2)
    with pytest.raises(SpeechStopped, match="additional_take"):
        opened()[0].admit_claim(CLIP, 2)
    with pytest.raises(SpeechStopped, match="unexpected_clip"):
        opened()[0].admit_claim("beta-question.wav", 1)


def test_a_provider_error_or_an_unexpected_close_stops_the_session() -> None:
    errored, _clock = opened()
    closed, _other = opened()

    with pytest.raises(SpeechStopped, match="provider_error"):
        errored.provider_message({"type": "error", "message": "unauthorized"})
    with pytest.raises(SpeechStopped, match="provider_closed"):
        closed.provider_closed(1011, expected=False)
    finished, _third = opened()
    finished.provider_closed(1000, expected=True)

    assert finished.report()["upstream_close_code"] == 1000
    assert finished.stop_reason is None


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
def test_an_incomplete_take_is_refused(messages: list[dict[str, Any]]) -> None:
    recording, _clock = opened()
    if messages:
        recording.admit_claim(CLIP, 1)

    with pytest.raises(SpeechStopped, match="incomplete_transcript"):
        recording.verify({(CLIP, 1): messages})


def test_a_complete_take_is_accepted_and_priced_by_audio_minute() -> None:
    recording, clock = opened()
    recording.admit_claim(CLIP, 1)
    recording.admit_audio(bytes(PCM_BYTES_PER_SECOND * 60))
    clock.now += 61.0

    recording.verify({(CLIP, 1): COMPLETE})
    recording.provider_closed(1000, expected=True)
    report = recording.report()

    assert report["estimated_usd"] == pytest.approx(0.003)
    assert report["recorded"] == [f"{CLIP} take 1"]
    assert report["wall_seconds"] == 61.0
    assert report["stop_reason"] is None


def test_a_stopped_recording_refuses_everything_after() -> None:
    recording, _clock = opened()
    with pytest.raises(SpeechStopped):
        recording.admit_claim("other.wav", 1)

    with pytest.raises(SpeechStopped, match="unexpected_clip"):
        recording.admit_audio(bytes(2))
    with pytest.raises(SpeechStopped, match="unexpected_clip"):
        recording.verify({})
