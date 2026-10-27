import random
from collections.abc import Callable
from datetime import UTC, datetime

from fakeyou.session.contracts import (
    AudioStreamObserved,
    CallStateObserved,
    ChatObserved,
    Endpoint,
    Observation,
    ParticipantCountObserved,
    PlaybackObserved,
    RosterObserved,
    ScreenObserved,
    SessionEvent,
    SourceHealthObserved,
    TranscriptObserved,
    VoiceTrackObserved,
)
from fakeyou.session.identity import presence
from fakeyou.session.kernel import Mailbox, SessionKernel
from fakeyou.session.world import Limits, SessionState

NOW = datetime(2026, 10, 27, tzinfo=UTC)
TWO_HOURS_OF_EVENTS = 40_000
LIMITS = Limits(dialogue_lines=50, interims=32)


Maker = Callable[[random.Random, int, str], Observation]
MAKERS: tuple[tuple[Maker, float], ...] = (
    (
        lambda rng, index, device: TranscriptObserved(
            transcript_id=f"t{index}",
            text=f"line {index}",
            final=rng.random() < 0.3,
            track_id=f"voice-{device}",
        ),
        45,
    ),
    (
        lambda _rng, index, device: ChatObserved(
            message_id=f"m{index}", device_id=device, text=f"chat {index}"
        ),
        10,
    ),
    (
        lambda rng, _index, device: RosterObserved(
            endpoints=(
                Endpoint(device_id="dev-twin", present=True, current_user=True),
                Endpoint(device_id=device, display_name=device, present=rng.random() < 0.9),
            ),
            synced=True,
        ),
        7,
    ),
    (
        lambda _rng, _index, device: AudioStreamObserved(
            device_id=device, stream_id=f"csrc:{device}"
        ),
        5,
    ),
    (
        lambda _rng, _index, device: VoiceTrackObserved(
            track_id=f"voice-{device}", device_id=device
        ),
        5,
    ),
    (
        lambda _rng, index, _device: ScreenObserved(title=f"slide {index % 40}"),
        6,
    ),
    (lambda rng, _index, _device: ParticipantCountObserved(remote=rng.randint(0, 12)), 6),
    (
        lambda rng, _index, _device: SourceHealthObserved(
            source=rng.choice(["meet_protocol", "stt", "memory"]),
            status=rng.choice(["healthy", "healthy", "stale"]),
        ),
        6,
    ),
    (
        lambda rng, index, _device: PlaybackObserved(
            utterance_id=f"u{index // 3}",
            sentence_index=index % 3,
            phase=rng.choice(["started", "finished", "interrupted"]),
            text=f"reply {index}",
        ),
        9.9,
    ),
    (
        lambda _rng, index, _device: CallStateObserved(state="in_call", browser_epoch=2 + index),
        0.1,
    ),
)


def synthetic_meeting(seed: int) -> list[Observation]:
    rng = random.Random(seed)
    people = [f"dev-{index}" for index in range(12)]
    makers = [maker for maker, _weight in MAKERS]
    weights = [weight for _maker, weight in MAKERS]
    stream: list[Observation] = [CallStateObserved(state="in_call", browser_epoch=1)]
    for index in range(TWO_HOURS_OF_EVENTS):
        [make] = rng.choices(makers, weights=weights)
        stream.append(make(rng, index, rng.choice(people)))
    return stream


def replay(stream: list[Observation]) -> SessionState:
    kernel = SessionKernel(session_id="meeting-9", attempt_id="attempt-1", limits=LIMITS)
    mailbox = Mailbox(capacity=256)
    for index, observation in enumerate(stream):
        mailbox.offer(
            SessionEvent(
                event_id=f"e{index}",
                session_id="meeting-9",
                attempt_id="attempt-1",
                source="stt",
                received_at=NOW,
                observation=observation,
            )
        )
        if index % 200 == 0:
            while (event := mailbox.take_nowait()) is not None:
                kernel.note_mailbox(mailbox)
                kernel.accept(event)
    while (event := mailbox.take_nowait()) is not None:
        kernel.accept(event)
    return kernel.state


def test_a_two_hour_meeting_stays_bounded_and_replays_identically() -> None:
    stream = synthetic_meeting(seed=7)

    first = replay(stream)
    second = replay(stream)

    assert first == second
    assert len(first.dialogue.lines) <= LIMITS.dialogue_lines
    assert len(first.dialogue.interims) <= LIMITS.interims
    assert len(first.world.tracks) <= 12
    assert len(first.world.endpoints) <= 13
    assert first.seq > TWO_HOURS_OF_EVENTS * 0.9
    fact = presence(first)
    if fact.plausible_max is not None:
        assert fact.complete
