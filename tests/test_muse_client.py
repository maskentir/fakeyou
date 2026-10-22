import asyncio
import json
import os
import time
import wave
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings, require_secret
from fakeyou.foundation.participation import same_voice
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.journal import Journal
from fakeyou.transcription.contract import SpeechSinks, SttConnectionLost, TranscriptionError
from fakeyou.transcription.muse import (
    MUSE_MAX_SESSION_SECONDS,
    MuseClient,
    MuseDeps,
    MuseOptions,
)

RATE = 16000
SECOND = RATE * 2
FAST = MuseOptions(
    handshake_timeout_seconds=1.0,
    keepalive_idle_seconds=0.05,
    keepalive_chunk_seconds=0.02,
)
AUDIO = b"\x01\x00" * 160


class RecordingJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def fields_of(self, event: str) -> dict[str, Any]:
        return next(fields for name, fields in self.events if name == event)


@dataclass
class FakeSocket:
    ack: dict[str, Any] = field(default_factory=lambda: {"sessionId": "session-1"})
    sent: list[str | bytes] = field(default_factory=list)
    alive: bool = True
    hang_on_handshake: bool = False
    _incoming: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)

    async def send(self, message: str | bytes) -> None:
        if not self.alive:
            raise TranscriptionError("socket closed")
        self.sent.append(message)

    async def recv(self) -> str:
        if self.hang_on_handshake:
            await asyncio.Event().wait()
        return json.dumps(self.ack)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.alive = False
        self._incoming.put_nowait(None)

    def push(self, message: dict[str, Any]) -> None:
        self._incoming.put_nowait(json.dumps(message))

    def end(self) -> None:
        self._incoming.put_nowait(None)

    def audio_sent(self) -> list[bytes]:
        return [item for item in self.sent if isinstance(item, bytes)]

    def text_sent(self) -> list[dict[str, Any]]:
        return [json.loads(item) for item in self.sent if isinstance(item, str)]


class Harness:
    def __init__(
        self,
        sockets: Sequence[FakeSocket],
        *,
        options: MuseOptions = FAST,
        on_final_error: BaseException | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []
        self.finals: list[TranscriptUpdate] = []
        self.interims: list[TranscriptUpdate] = []
        self.lost: list[TranscriptionError] = []
        self.journal = RecordingJournal()
        self._on_final_error = on_final_error
        self.client = MuseClient(
            api_key="meta-secret-key",
            options=options,
            sinks=SpeechSinks(
                on_update=self._final,
                on_interim=self._interim,
                on_lost=self.lost.append,
            ),
            deps=MuseDeps(journal=cast(Journal, self.journal), open_socket=self._open, clock=clock),
        )

    async def _open(self, url: str) -> FakeSocket:
        self.urls.append(url)
        return self.sockets.pop(0)

    async def _final(self, update: TranscriptUpdate) -> None:
        if self._on_final_error is not None:
            error, self._on_final_error = self._on_final_error, None
            raise error
        self.finals.append(update)

    async def _interim(self, update: TranscriptUpdate) -> None:
        self.interims.append(update)


async def _settle() -> None:
    await asyncio.sleep(0.03)


def _turn(
    turn: int,
    *,
    labels: Sequence[str] = (),
    text: str = "Good morning.",
    partials: Sequence[str] = (),
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "speechStart", "turnId": turn, "audioProcessedMs": 0}]
    events += [{"type": "transcript", "final": False, "transcript": p} for p in partials]
    events += [{"type": "speaker", "label": label, "audioProcessedMs": 0} for label in labels]
    events.append({"type": "speechEnd", "turnId": turn, "audioProcessedMs": 0})
    events.append(
        {"type": "speechComplete", "turnId": turn, "transcript": f" {text} ", "audioProcessedMs": 0}
    )
    return events


async def test_handshake_asks_for_diarization_with_cumulative_partials_and_english_bias() -> None:
    socket = FakeSocket()
    harness = Harness([socket], options=MuseOptions(language="en", mode="DIARIZATION"))

    await harness.client.start()
    await harness.client.close()

    handshake = socket.text_sent()[0]
    assert handshake["mode"] == "DIARIZATION"
    assert handshake["partialMode"] == "CUMULATIVE"
    assert handshake["audioEncoding"] == "PCM_16KHZ"
    assert handshake["languageBias"] == ["English"]
    assert handshake["emitAudioProgress"] is False
    assert handshake["authorization"] == {"accessToken": "Bearer meta-secret-key"}


async def test_handshake_names_indonesian_for_indonesian_sessions() -> None:
    socket = FakeSocket()
    harness = Harness([socket], options=MuseOptions(language="id"))

    await harness.client.start()
    await harness.client.close()

    assert socket.text_sent()[0]["languageBias"] == ["Indonesian"]


async def test_each_connection_gets_its_own_session_id() -> None:
    first, second = FakeSocket(), FakeSocket(ack={"sessionId": "session-2"})
    harness = Harness([first, second])

    await harness.client.start()
    await harness.client.reconnect_when_idle()
    await harness.client.close()

    assert len(harness.urls) == 2
    assert harness.urls[0] != harness.urls[1]
    assert all("?sessionId=stream-" in url for url in harness.urls)


async def test_rejected_handshake_raises_and_closes_the_socket() -> None:
    socket = FakeSocket(ack={"message": "invalid key"})
    harness = Harness([socket])

    with pytest.raises(TranscriptionError, match="rejected"):
        await harness.client.start()

    assert socket.alive is False
    assert harness.client.ready is False


async def test_silent_handshake_times_out_as_a_transcription_error() -> None:
    socket = FakeSocket(hang_on_handshake=True)
    harness = Harness([socket], options=MuseOptions(handshake_timeout_seconds=0.05))

    with pytest.raises(TranscriptionError, match="timed out"):
        await harness.client.start()


async def test_turn_becomes_one_final_with_its_speaker_label() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "speechStart", "turnId": 0, "audioProcessedMs": 220})
    socket.push(
        {"type": "transcript", "final": False, "transcript": " Good", "audioProcessedMs": 1040}
    )
    socket.push({"type": "speaker", "label": "A", "audioProcessedMs": 4480})
    socket.push({"type": "speechEnd", "turnId": 0, "audioProcessedMs": 3980})
    socket.push(
        {
            "type": "speechComplete",
            "turnId": 0,
            "transcript": " Good morning. ",
            "audioProcessedMs": 3980,
        }
    )
    await _settle()

    assert [update.text for update in harness.finals] == ["Good morning."]
    assert harness.finals[0].final is True
    assert harness.finals[0].voice_clusters == ("A",)
    assert [update.text for update in harness.interims] == ["Good"]
    assert harness.interims[0].final is False
    await harness.client.close()


async def test_second_turn_carries_only_its_own_label() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    for event in _turn(0, labels=["A"], text="First.") + _turn(1, labels=["B"], text="Second."):
        socket.push(event)
    await _settle()

    assert [update.voice_clusters for update in harness.finals] == [("A",), ("B",)]
    await harness.client.close()


async def test_speaker_change_inside_one_turn_keeps_every_label_in_order() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    for event in _turn(0, labels=["A", "B", "A"], text="Mixed."):
        socket.push(event)
    await _settle()

    assert harness.finals[0].voice_clusters == ("A", "B")
    await harness.client.close()


async def test_label_arriving_after_its_turn_closed_is_journaled_not_attached() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    for event in _turn(0, text="Done."):
        socket.push(event)
    socket.push({"type": "speaker", "label": "B", "audioProcessedMs": 9000})
    await _settle()

    assert harness.finals[0].voice_clusters == ()
    assert "stt_speaker_dropped" in harness.journal.names()
    await harness.client.close()


async def test_blank_turn_emits_no_final_and_is_journaled() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "speechStart", "turnId": 0, "audioProcessedMs": 0})
    socket.push({"type": "speechComplete", "turnId": 0, "transcript": "   ", "audioProcessedMs": 0})
    await _settle()

    assert harness.finals == []
    assert "stt_turn_skipped" in harness.journal.names()
    await harness.client.close()


async def test_empty_final_flush_at_end_of_session_is_ignored() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "transcript", "final": True, "transcript": "", "audioProcessedMs": 26560})
    await _settle()

    assert harness.finals == []
    assert harness.lost == []
    await harness.client.close()


async def test_remote_error_marks_the_socket_dead_and_reports_it() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "error", "message": "quota exceeded"})
    await _settle()

    assert harness.client.socket_dead is True
    assert [type(error) for error in harness.lost] == [TranscriptionError]
    assert "quota exceeded" in str(harness.lost[0])


async def test_socket_ending_without_a_close_request_is_reported_as_lost() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.end()
    await _settle()

    assert harness.client.socket_dead is True
    assert len(harness.lost) == 1


async def test_keepalive_sends_silence_only_while_capture_is_idle() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    await asyncio.sleep(0.3)
    silence = socket.audio_sent()
    assert len(silence) >= 3
    assert all(chunk and set(chunk) == {0} for chunk in silence)

    socket.sent.clear()
    for _ in range(30):
        harness.client.send_audio(AUDIO)
        await asyncio.sleep(0.01)
    during = socket.audio_sent()
    assert during
    assert all(chunk == AUDIO for chunk in during)
    assert "stt_keepalive_started" in harness.journal.names()
    await harness.client.close()


async def test_keepalive_fills_the_real_time_deficit_not_just_a_fixed_chunk() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    await asyncio.sleep(0.5)

    sent = sum(len(chunk) for chunk in socket.audio_sent())
    assert sent >= 32000 * 0.5 - 640
    await harness.client.close()


async def test_audio_fed_before_the_session_is_ready_is_sent_in_order() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    first, second = b"\x01\x00" * 100, b"\x02\x00" * 100

    harness.client.send_audio(first)
    harness.client.send_audio(second)
    await harness.client.start()
    await _settle()

    assert socket.audio_sent()[:2] == [first, second]
    await harness.client.close()


async def test_backlog_is_capped_at_four_seconds_and_drops_the_oldest() -> None:
    socket = FakeSocket()
    harness = Harness([socket])

    for index in range(5):
        harness.client.send_audio(bytes([index + 1]) * SECOND)
    await harness.client.start()
    await _settle()

    sent = socket.audio_sent()
    assert sum(len(chunk) for chunk in sent) == 4 * SECOND
    assert sent[0] == bytes([2]) * SECOND
    assert harness.journal.fields_of("stt_audio_dropped")["dropped_ms"] == 1000.0
    await harness.client.close()


async def test_close_tells_muse_the_stream_ended_before_closing() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    await harness.client.close()

    assert socket.text_sent()[-1] == {"type": "endStream"}
    assert socket.alive is False


async def test_reconnect_opens_a_new_socket_and_keeps_audio_that_waited() -> None:
    first = FakeSocket()
    second = FakeSocket(ack={"sessionId": "session-2"})
    harness = Harness([first, second])
    await harness.client.start()

    first.end()
    await _settle()
    assert harness.client.socket_dead is True

    harness.client.send_audio(b"\x07\x00" * 50)
    await harness.client.reconnect_when_idle()
    await _settle()

    assert harness.client.socket_dead is False
    assert second.audio_sent() == [b"\x07\x00" * 50]
    await harness.client.close()


async def test_failed_reconnect_leaves_the_socket_marked_dead() -> None:
    harness = Harness([FakeSocket(), FakeSocket(ack={"message": "busy"})])
    await harness.client.start()
    assert harness.client.socket_dead is False

    with pytest.raises(TranscriptionError, match="rejected the session"):
        await harness.client.reconnect_when_idle()

    assert harness.client.socket_dead is True
    await harness.client.close()


async def test_turn_recording_reports_interim_count_and_latency() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "speechStart", "turnId": 0, "audioProcessedMs": 0})
    socket.push({"type": "transcript", "final": False, "transcript": "Good", "audioProcessedMs": 1})
    socket.push(
        {"type": "transcript", "final": False, "transcript": "Good day", "audioProcessedMs": 2}
    )
    for event in _turn(0, text="Good day.")[-2:]:
        socket.push(event)
    await _settle()

    recorded = harness.journal.fields_of("stt_turn_recorded")
    assert recorded["interims"] == 2
    assert recorded["first_interim_to_final_ms"] >= 0
    await harness.client.close()


async def test_unknown_message_types_are_journaled_and_do_not_break_the_stream() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "mystery", "detail": 1})
    for event in _turn(0, text="Still here."):
        socket.push(event)
    await _settle()

    assert "stt_message_skipped" in harness.journal.names()
    assert [update.text for update in harness.finals] == ["Still here."]
    await harness.client.close()


async def test_consumer_failure_is_contained_and_the_next_turn_still_arrives() -> None:
    socket = FakeSocket()
    harness = Harness([socket], on_final_error=RuntimeError("consumer broke"))
    await harness.client.start()

    for event in _turn(0, text="First.") + _turn(1, text="Second."):
        socket.push(event)
    await _settle()

    assert [update.text for update in harness.finals] == ["Second."]
    assert harness.client.socket_dead is False
    await harness.client.close()


@pytest.mark.live
async def test_muse_diarization_splits_turns_and_labels_voices_live() -> None:
    audio_name = os.environ.get("MUSE_DIARIZATION_AUDIO")
    if not audio_name:
        pytest.skip("set MUSE_DIARIZATION_AUDIO to a 16 kHz mono WAV with two voices")
    settings = Settings()
    try:
        api_key = require_secret(settings.stt.api_key, ConfigKey.STT_API_KEY)
    except FakeYouError as error:
        pytest.skip(str(error))
    with wave.open(audio_name, "rb") as source:
        assert source.getframerate() == 16000
        assert source.getnchannels() == 1
        pcm = source.readframes(source.getnframes())

    finals: list[TranscriptUpdate] = []

    async def collect(update: TranscriptUpdate) -> None:
        finals.append(update)

    async def ignore(update: TranscriptUpdate) -> None:
        del update

    client = MuseClient(
        api_key=api_key,
        options=MuseOptions(
            language="en", mode="DIARIZATION", model=settings.stt.model, url=settings.stt.url
        ),
        sinks=SpeechSinks(on_update=collect, on_interim=ignore, on_lost=lambda _error: None),
    )
    await client.start()
    step = 1280 * 2
    deadline = time.monotonic()
    for offset in range(0, len(pcm), step):
        client.send_audio(pcm[offset : offset + step])
        deadline += 0.08
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
    await asyncio.sleep(8)
    await client.close()

    assert len(finals) == 4
    labels = [update.voice_clusters for update in finals]
    assert all(len(cluster) == 1 for cluster in labels)
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    assert labels[0] != labels[1]
    assert "migration" in finals[0].text.lower()
    assert "blockers" in finals[2].text.lower()


async def test_muse_speaker_label_arrives_on_final_voice_clusters() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "speechStart", "turnId": 0, "audioProcessedMs": 220})
    socket.push({"type": "speaker", "label": "cluster-a", "audioProcessedMs": 3480})
    socket.push({"type": "speechEnd", "turnId": 0, "audioProcessedMs": 3980})
    socket.push(
        {
            "type": "speechComplete",
            "turnId": 0,
            "transcript": " hello everyone ",
            "audioProcessedMs": 3980,
        }
    )
    await _settle()

    assert len(harness.finals) == 1
    assert harness.finals[0].final is True
    assert harness.finals[0].text == "hello everyone"
    assert harness.finals[0].voice_clusters == ("cluster-a",)
    await harness.client.close()


async def test_voice_cluster_labels_reset_with_new_stt_session() -> None:
    first = FakeSocket()
    second = FakeSocket(ack={"sessionId": "session-2"})
    harness = Harness([first, second])
    await harness.client.start()
    for event in _turn(0, labels=["cluster-a"], text="First."):
        first.push(event)
    await _settle()

    await harness.client.reconnect_when_idle()
    for event in _turn(0, labels=["cluster-a"], text="Second."):
        second.push(event)
    await _settle()

    before, after = (update.voice_clusters for update in harness.finals)
    assert before == ("cluster-a",)
    assert len(after) == 1
    assert same_voice("cluster-a", after) is False
    await harness.client.close()


async def test_label_of_a_turn_cut_by_a_new_session_is_not_inherited() -> None:
    first = FakeSocket()
    second = FakeSocket(ack={"sessionId": "session-2"})
    harness = Harness([first, second])
    await harness.client.start()
    first.push({"type": "speechStart", "turnId": 0, "audioProcessedMs": 0})
    first.push({"type": "speaker", "label": "cluster-a", "audioProcessedMs": 0})
    await _settle()

    await harness.client.reconnect_when_idle()
    second.push(
        {"type": "speechComplete", "turnId": 0, "transcript": " two ", "audioProcessedMs": 0}
    )
    await _settle()

    assert [update.voice_clusters for update in harness.finals] == [()]
    await harness.client.close()


@pytest.mark.parametrize(
    "message",
    [
        "Ingress audio slower than real-time",
        "Audio processing backlog too large",
        "ingress audio slower than realtime",
        "BACKLOG TOO LARGE",
    ],
)
async def test_overload_error_is_a_connection_loss_so_the_session_reconnects(
    message: str,
) -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "error", "message": message})
    await _settle()

    assert harness.client.socket_dead is True
    assert [type(error) for error in harness.lost] == [SttConnectionLost]


async def test_max_session_duration_is_a_connection_loss_so_the_session_reconnects() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "error", "message": "Max session duration reached"})
    await _settle()

    assert harness.client.socket_dead is True
    assert [type(error) for error in harness.lost] == [SttConnectionLost]


async def test_session_expiry_counts_down_from_each_new_session() -> None:
    clock = [1000.0]
    harness = Harness(
        [FakeSocket(), FakeSocket(ack={"sessionId": "session-2"})], clock=lambda: clock[0]
    )
    assert harness.client.session_expires_in(clock[0]) is None

    await harness.client.start()
    clock[0] += 3000.0
    assert harness.client.session_expires_in(clock[0]) == MUSE_MAX_SESSION_SECONDS - 3000.0

    await harness.client.reconnect_when_idle()
    assert harness.client.session_expires_in(clock[0]) == MUSE_MAX_SESSION_SECONDS

    await harness.client.close()
    assert harness.client.session_expires_in(clock[0]) is None


async def test_other_server_errors_are_not_connection_losses() -> None:
    socket = FakeSocket()
    harness = Harness([socket])
    await harness.client.start()

    socket.push({"type": "error", "message": "quota exceeded"})
    await _settle()

    assert [type(error) for error in harness.lost] == [TranscriptionError]
