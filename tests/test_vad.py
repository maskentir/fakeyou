import hashlib
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from fakeyou.audio.vad import (
    DEFAULT_MODEL_RESET_SAMPLES,
    VadAnalyzer,
    VadController,
    VadEvent,
    VadEventKind,
    VadParams,
    VadState,
)

FIXTURES = Path(__file__).parent / "fixtures"
RATE = 16000
WINDOW = 512
GOLDEN_TOLERANCE = 1e-5


class ScriptedModel:
    def __init__(self, probability: float) -> None:
        self.probability = probability
        self.resets = 0

    def predict(self, window: np.ndarray) -> float:
        del window
        return self.probability

    def reset(self) -> None:
        self.resets += 1


class ManualClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _standup_pcm() -> bytes:
    with wave.open(str(FIXTURES / "standup-16k.wav"), "rb") as source:
        assert source.getframerate() == RATE
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        return source.readframes(source.getnframes())


def _golden() -> dict[str, Any]:
    golden: dict[str, Any] = json.loads((FIXTURES / "vad-golden-standup-16k.json").read_text())
    return golden


def _params(golden: dict[str, Any]) -> VadParams:
    return VadParams(**golden["params"])


def _tone(seconds: float, amplitude: float = 0.5) -> bytes:
    count = int(RATE * seconds)
    samples = (
        int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / RATE))
        for index in range(count)
    )
    return struct.pack(f"<{count}h", *samples)


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def _chunks(pcm: bytes, size: int) -> list[bytes]:
    step = size * 2
    return [pcm[offset : offset + step] for offset in range(0, len(pcm), step)]


def test_windows_match_pipecat_golden_per_window() -> None:
    golden = _golden()
    analyzer = VadAnalyzer(_params(golden), model=None, model_reset_samples=None)
    windows = analyzer.feed(_standup_pcm())

    expected = golden["windows"]
    assert len(windows) == len(expected)
    for window, (end, confidence, volume, state) in zip(windows, expected, strict=True):
        assert window.end_sample == end
        assert window.confidence == pytest.approx(confidence, abs=GOLDEN_TOLERANCE)
        assert window.volume == pytest.approx(volume, abs=GOLDEN_TOLERANCE)
        assert window.state.name == state


def test_controller_events_match_pipecat_golden_on_512_windows() -> None:
    golden = _golden()
    analyzer = VadAnalyzer(_params(golden), model=None, model_reset_samples=None)
    controller = VadController(analyzer, clock=ManualClock())

    events: list[tuple[str, int]] = []
    for chunk in _chunks(_standup_pcm(), WINDOW):
        for event in controller.feed(chunk):
            if event.kind is not VadEventKind.ACTIVITY:
                events.append((event.kind.value, event.sample))

    assert events == [tuple(pair) for pair in golden["events"]]


@pytest.mark.parametrize("chunk_samples", [1280, 333, 4000])
def test_window_states_do_not_depend_on_chunking(chunk_samples: int) -> None:
    golden = _golden()
    analyzer = VadAnalyzer(_params(golden), model=None, model_reset_samples=None)
    states: list[str] = []
    for chunk in _chunks(_standup_pcm(), chunk_samples):
        states.extend(window.state.name for window in analyzer.feed(chunk))

    expected = [state for *_, state in golden["windows"]]
    assert states == expected


@pytest.mark.parametrize("chunk_samples", [1280, 333])
def test_event_kinds_hold_under_chunking_and_land_in_the_detecting_chunk(
    chunk_samples: int,
) -> None:
    golden = _golden()
    analyzer = VadAnalyzer(_params(golden), model=None, model_reset_samples=None)
    controller = VadController(analyzer, clock=ManualClock())

    seen: list[tuple[str, int]] = []
    fed = 0
    for chunk in _chunks(_standup_pcm(), chunk_samples):
        fed += len(chunk) // 2
        for event in controller.feed(chunk):
            if event.kind is not VadEventKind.ACTIVITY:
                seen.append((event.kind.value, event.sample))
                assert event.sample <= fed

    golden_events = [tuple(pair) for pair in golden["events"]]
    assert [kind for kind, _ in seen] == [kind for kind, _ in golden_events]
    for (_, found), (_, expected) in zip(seen, golden_events, strict=True):
        assert expected <= found < expected + chunk_samples


def test_activity_fires_on_the_audio_clock_only_while_speaking() -> None:
    golden = _golden()
    analyzer = VadAnalyzer(_params(golden), model=None, model_reset_samples=None)
    controller = VadController(analyzer, clock=ManualClock())

    speaking_at: list[tuple[float, float]] = []
    started: int | None = None
    activity: list[int] = []
    for chunk in _chunks(_standup_pcm(), 1280):
        for event in controller.feed(chunk):
            if event.kind is VadEventKind.STARTED:
                started = event.sample
            elif event.kind is VadEventKind.STOPPED and started is not None:
                speaking_at.append((started, event.sample))
                started = None
            elif event.kind is VadEventKind.ACTIVITY:
                activity.append(event.sample)

    if started is not None:
        speaking_at.append((started, math.inf))
    assert activity
    assert all(
        any(start <= sample <= end + 1280 for start, end in speaking_at) for sample in activity
    )
    gaps = [later - earlier for earlier, later in zip(activity, activity[1:], strict=False)]
    assert all(gap >= int(0.2 * RATE) for gap in gaps)


def test_idle_timeout_forces_stop_and_resets_the_analyzer_to_quiet() -> None:
    clock = ManualClock()
    analyzer = VadAnalyzer(VadParams(), model=ScriptedModel(0.99), model_reset_samples=None)
    controller = VadController(analyzer, clock=clock, audio_idle_timeout_seconds=1.0)

    first = controller.feed(_tone(1.0))
    assert [event.kind for event in first if event.kind is not VadEventKind.ACTIVITY] == [
        VadEventKind.STARTED
    ]

    clock.now += 0.5
    assert controller.check_idle() == []

    clock.now += 0.6
    forced = controller.check_idle()
    assert [event.kind for event in forced] == [VadEventKind.STOPPED]
    assert analyzer.state is VadState.QUIET

    resumed = controller.feed(_tone(1.0))
    assert VadEventKind.STARTED in [event.kind for event in resumed]


def test_model_state_resets_on_the_audio_clock_not_the_wall_clock() -> None:
    model = ScriptedModel(0.0)
    analyzer = VadAnalyzer(
        VadParams(), model=model, model_reset_samples=DEFAULT_MODEL_RESET_SAMPLES
    )
    controller = VadController(analyzer, clock=ManualClock())

    controller.feed(_silence(4.9))
    assert model.resets == 0
    controller.feed(_silence(0.2))
    assert model.resets == 1
    controller.feed(_silence(5.0))
    assert model.resets == 2


def test_partial_windows_wait_for_the_rest_of_the_frame() -> None:
    analyzer = VadAnalyzer(VadParams(), model=ScriptedModel(0.0), model_reset_samples=None)
    assert analyzer.feed(_silence(WINDOW / RATE / 2)) == []
    assert len(analyzer.feed(_silence(WINDOW / RATE / 2))) == 1


def test_quiet_audio_never_emits_speech_events() -> None:
    analyzer = VadAnalyzer(VadParams(), model=ScriptedModel(0.99), model_reset_samples=None)
    controller = VadController(analyzer, clock=ManualClock())
    events: list[VadEvent] = controller.feed(_silence(2.0))
    assert events == []


def test_vendored_silero_model_matches_the_recorded_checksum() -> None:
    from fakeyou.audio.vad import MODEL_PATH

    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    assert digest == "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
