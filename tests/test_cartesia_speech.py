import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any, override

import pytest
import websockets
from pydantic import SecretStr
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from fakeyou.config.settings import CartesiaTTS
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech.cartesia import CartesiaSpeechSynthesizer, connect_cartesia, socket_url
from fakeyou.speech.contract import SpeechSegment
from fakeyou.speech.fallback import SpeechWithFallback
from fakeyou.speech.websocket import SpeechSocket
from tests._persistence_fakes import RecordingJournal
from tests._prepared_support import SentenceSynth

SETTINGS = CartesiaTTS(api_key=SecretStr("k"), model="sonic-3", voice="voice-1")


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.closed = False
        self.arrived = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))
        self.arrived.set()

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, **message: Any) -> None:
        self.inbox.put_nowait(json.dumps(message))

    def chunk(self, context: str, flush_id: int, pcm: bytes) -> None:
        self.push(
            type="chunk",
            context_id=context,
            flush_id=flush_id,
            data=base64.b64encode(pcm).decode(),
            done=False,
        )


class Opener:
    def __init__(self, *sockets: FakeSocket) -> None:
        self._sockets = list(sockets)
        self.opened = 0
        self.opening = asyncio.Event()

    async def __call__(self) -> SpeechSocket:
        self.opened += 1
        self.opening.set()
        return self._sockets.pop(0)


def synthesizer(
    opener: Opener, journal: RecordingJournal | None = None
) -> CartesiaSpeechSynthesizer:
    return CartesiaSpeechSynthesizer(
        api_key="k", settings=SETTINGS, language="id", journal=journal, opener=opener
    )


async def sent(socket: FakeSocket, count: int) -> list[dict[str, Any]]:
    async with asyncio.timeout(2):
        while len(socket.sent) < count:
            socket.arrived.clear()
            await socket.arrived.wait()
    return socket.sent


async def drained(segment: SpeechSegment) -> bytes:
    async with asyncio.timeout(2):
        return b"".join([chunk async for chunk in segment])


async def test_the_sentences_of_a_turn_share_one_context_and_continue() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")

    first = turn.speak("Hello everyone.", sentence_index=0)
    second = turn.speak(" Two things today.", sentence_index=1)
    await turn.finish()
    one, two, end = await sent(socket, 3)

    assert one["context_id"] == two["context_id"] == end["context_id"]
    assert (one["transcript"], one["continue"], one["flush"]) == ("Hello everyone.", True, True)
    assert (two["transcript"], two["continue"], two["flush"]) == (" Two things today.", True, True)
    assert (end["transcript"], end["continue"]) == ("", False)
    assert one["voice"] == {"mode": "id", "id": "voice-1"}
    assert (one["model_id"], one["language"]) == ("sonic-3", "id")
    assert (one["max_buffer_delay_ms"], one["add_timestamps"]) == (0, True)
    assert one["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24000,
    }
    context = one["context_id"]
    socket.chunk(context, 0, b"\x01\x00" * 4)
    socket.push(type="flush_done", context_id=context, flush_id=0, done=False)
    socket.chunk(context, 1, b"\x02\x00" * 2)
    socket.push(type="flush_done", context_id=context, flush_id=1, done=False)
    socket.push(type="done", context_id=context, done=True)

    assert await drained(first) == b"\x01\x00" * 4
    assert await drained(second) == b"\x02\x00" * 2
    await voice.aclose()


async def test_word_timings_are_relative_to_each_sentence() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    first = turn.speak("Hello.", sentence_index=0)
    second = turn.speak(" Two.", sentence_index=1)
    [one, _] = await sent(socket, 2)
    context = one["context_id"]

    socket.push(
        type="timestamps",
        context_id=context,
        done=False,
        word_timestamps={"words": ["Hello."], "start": [0.2], "end": [0.5]},
    )
    socket.chunk(context, 0, b"\x00" * 48000)
    socket.push(type="flush_done", context_id=context, flush_id=0, done=False)
    socket.push(
        type="timestamps",
        context_id=context,
        done=False,
        word_timestamps={"words": ["Two."], "start": [1.25], "end": [1.5]},
    )
    socket.chunk(context, 1, b"\x00" * 4800)
    socket.push(type="flush_done", context_id=context, flush_id=1, done=False)
    await drained(first)
    await drained(second)

    assert [(w.text, w.start, w.end) for w in first.words or ()] == [("Hello.", 0.2, 0.5)]
    [word] = second.words or ()
    assert (word.text, word.start, word.end) == pytest.approx(("Two.", 0.25, 0.5))
    await voice.aclose()


async def test_an_error_message_fails_the_sentence_with_a_typed_fault() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    [one] = await sent(socket, 1)

    socket.push(
        type="error",
        context_id=one["context_id"],
        status_code=401,
        title="Unauthorized",
        message="invalid api key",
        done=True,
    )

    with pytest.raises(ProviderFault) as caught:
        await drained(segment)
    assert caught.value.kind == "unauthorized"
    await voice.aclose()


async def test_cancel_drops_the_audio_that_keeps_arriving() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    segment = turn.speak("A long sentence.", sentence_index=0)
    [one] = await sent(socket, 1)
    context = one["context_id"]
    socket.chunk(context, 0, b"\x01\x00")
    assert await asyncio.wait_for(anext(segment), timeout=2) == b"\x01\x00"

    await turn.cancel()
    socket.chunk(context, 0, b"\x02\x00")
    _, cancel = await sent(socket, 2)

    assert cancel == {"context_id": context, "cancel": True}
    assert await drained(segment) == b""
    await voice.aclose()


async def test_a_lost_socket_fails_the_open_sentences_and_the_next_one_reconnects() -> None:
    lost, fresh = FakeSocket(), FakeSocket()
    opener = Opener(lost, fresh)
    voice = synthesizer(opener)
    turn = voice.open_turn(transcript_id="t1")
    first = turn.speak("Hello.", sentence_index=0)
    [one] = await sent(lost, 1)

    lost.inbox.put_nowait(OSError("connection reset"))
    with pytest.raises(ProviderFault) as caught:
        await drained(first)
    assert caught.value.kind == "unavailable"

    second = turn.speak(" Two.", sentence_index=1)
    [two] = await sent(fresh, 1)
    assert opener.opened == 2
    assert two["context_id"] != one["context_id"]
    fresh.chunk(two["context_id"], 0, b"\x03\x00")
    fresh.push(type="flush_done", context_id=two["context_id"], flush_id=0, done=False)
    assert await drained(second) == b"\x03\x00"
    await voice.aclose()


async def test_a_failed_handshake_fails_the_sentence() -> None:
    async def refuse() -> SpeechSocket:
        raise ProviderFault("tts", "unauthorized")

    voice = CartesiaSpeechSynthesizer(api_key="k", settings=SETTINGS, opener=refuse)

    with pytest.raises(ProviderFault) as caught:
        await drained(voice.open_turn().speak("Hello."))
    assert caught.value.kind == "unauthorized"
    await voice.aclose()


async def test_each_sentence_is_journaled_as_a_synthesis() -> None:
    socket = FakeSocket()
    journal = RecordingJournal()
    voice = synthesizer(Opener(socket), journal)
    segment = voice.open_turn(transcript_id="t1").speak("Hello.", sentence_index=0)
    [one] = await sent(socket, 1)
    socket.chunk(one["context_id"], 0, b"\x01\x00" * 3)
    socket.push(type="flush_done", context_id=one["context_id"], flush_id=0, done=False)

    await drained(segment)

    [started] = journal.events("synthesis_started")
    [finished] = journal.events("synthesis_finished")
    [first_pcm] = journal.events("synthesis_pcm_emitted")
    assert (started["provider"], started["transcript_id"], started["sentence_index"]) == (
        "cartesia",
        "t1",
        0,
    )
    assert finished["bytes_out"] == 6
    assert first_pcm["first_chunk_ms"] is not None
    await voice.aclose()


async def test_the_socket_opens_before_the_first_sentence() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)
    assert opener.opened == 0

    voice.warm()
    await asyncio.wait_for(opener.opening.wait(), timeout=2)

    assert socket.sent == []
    await voice.aclose()
    assert socket.closed


def test_the_socket_url_follows_the_configured_base_url() -> None:
    assert socket_url("https://api.cartesia.ai") == (
        "wss://api.cartesia.ai/tts/websocket?cartesia_version=2026-03-01"
    )
    assert socket_url("http://localhost:8080").startswith("ws://localhost:8080/tts/websocket")


async def test_closing_fails_the_sentences_still_waiting() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 1)

    await voice.aclose()

    with pytest.raises(ProviderFault):
        await drained(segment)


async def test_the_live_message_order_is_split_into_sentences() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    first = turn.speak("Hello.", sentence_index=0)
    second = turn.speak(" Two.", sentence_index=1)
    await turn.finish()
    [one, _, _] = await sent(socket, 3)
    context = one["context_id"]

    def stamp(word: str, start: float, end: float) -> None:
        socket.push(
            type="timestamps",
            context_id=context,
            done=False,
            word_timestamps={"words": [word], "start": [start], "end": [end]},
        )

    stamp("Hello.", 0.1, 0.4)
    socket.chunk(context, 1, b"\x01\x00" * 2)
    socket.chunk(context, 1, b"\x01\x00" * 2)
    socket.push(type="flush_done", context_id=context, flush_id=1, flush_done=True, done=False)
    stamp("Two.", 0.2, 0.3)
    socket.chunk(context, 2, b"\x02\x00")
    socket.push(type="flush_done", context_id=context, flush_id=2, flush_done=True, done=False)
    socket.push(type="flush_done", context_id=context, flush_id=3, flush_done=True, done=False)
    socket.push(type="done", context_id=context, done=True)

    assert await drained(first) == b"\x01\x00" * 4
    assert await drained(second) == b"\x02\x00"
    assert [w.text for w in first.words or ()] == ["Hello."]
    [word] = second.words or ()
    assert word.text == "Two."
    assert word.start == pytest.approx(0.2 - 8 / 48000)
    await voice.aclose()


async def test_sentences_read_in_parallel_each_get_their_own_audio() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    first = turn.speak("Hello.", sentence_index=0)
    second = turn.speak(" Two.", sentence_index=1)
    reading = asyncio.gather(drained(first), drained(second))
    [one, _] = await sent(socket, 2)
    context = one["context_id"]

    socket.chunk(context, 0, b"\x01\x00")
    socket.push(type="flush_done", context_id=context, flush_id=0, done=False)
    socket.chunk(context, 1, b"\x02\x00")
    socket.push(type="flush_done", context_id=context, flush_id=1, done=False)

    assert list(await reading) == [b"\x01\x00", b"\x02\x00"]
    await voice.aclose()


async def test_a_malformed_message_fails_its_context_and_keeps_the_socket() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)
    broken = voice.open_turn(transcript_id="t1").speak("Hello.")
    [one] = await sent(socket, 1)

    socket.push(
        type="timestamps",
        context_id=one["context_id"],
        done=False,
        word_timestamps={"words": ["Hello."], "start": [None], "end": [0.4]},
    )
    socket.inbox.put_nowait("[1, 2]")
    with pytest.raises(ProviderFault) as caught:
        await drained(broken)
    assert caught.value.kind == "unavailable"

    healthy = voice.open_turn(transcript_id="t2").speak("Again.")
    [_, two] = await sent(socket, 2)
    socket.chunk(two["context_id"], 0, b"\x05\x00")
    socket.push(type="flush_done", context_id=two["context_id"], flush_id=0, done=False)
    assert await drained(healthy) == b"\x05\x00"
    assert opener.opened == 1
    await voice.aclose()


async def test_a_flush_done_for_a_later_sentence_ends_the_earlier_ones() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    first = turn.speak("Hello.", sentence_index=0)
    second = turn.speak(" Two.", sentence_index=1)
    [one, _] = await sent(socket, 2)
    context = one["context_id"]

    socket.chunk(context, 0, b"\x01\x00")
    socket.chunk(context, 1, b"\x02\x00")
    socket.push(type="flush_done", context_id=context, flush_id=1, done=False)

    assert await drained(first) == b"\x01\x00"
    assert await drained(second) == b"\x02\x00"
    await voice.aclose()


async def test_an_error_without_a_context_fails_the_sentences_on_that_socket() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 1)

    socket.push(type="error", status_code=429, error="too many requests")

    with pytest.raises(ProviderFault) as caught:
        await drained(segment)
    assert caught.value.kind == "rate_limited"
    assert "too many requests" in str(caught.value)
    await voice.aclose()


class FailingSend(FakeSocket):
    @override
    async def send(self, message: str) -> None:
        raise OSError(f"broken pipe while sending {len(message)} bytes")


async def test_a_first_send_on_a_dead_socket_moves_to_a_fresh_one() -> None:
    stale, fresh = FailingSend(), FakeSocket()
    opener = Opener(stale, fresh)
    voice = synthesizer(opener)
    alive = voice.open_turn(transcript_id="t1").speak("Hello.")

    [one] = await sent(fresh, 1)
    stale.inbox.put_nowait(OSError("reset by peer"))
    fresh.chunk(one["context_id"], 0, b"\x06\x00")
    fresh.push(type="flush_done", context_id=one["context_id"], flush_id=0, done=False)

    assert await drained(alive) == b"\x06\x00"
    assert (opener.opened, stale.closed) == (2, True)
    await voice.aclose()


async def test_a_sentence_whose_send_fails_twice_fails() -> None:
    voice = synthesizer(Opener(FailingSend(), FailingSend()))
    lost = voice.open_turn(transcript_id="t1").speak("Hello.")

    with pytest.raises(ProviderFault) as caught:
        await drained(lost)
    assert caught.value.kind == "unavailable"
    await voice.aclose()


async def test_an_error_for_a_finished_context_leaves_the_others_alone() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    first = voice.open_turn(transcript_id="t1")
    done = first.speak("Done.")
    [one] = await sent(socket, 1)
    socket.chunk(one["context_id"], 0, b"\x01\x00")
    socket.push(type="done", context_id=one["context_id"], done=True)
    await drained(done)
    alive = voice.open_turn(transcript_id="t2").speak("Alive.")
    [_, two] = await sent(socket, 2)

    socket.push(type="error", context_id=one["context_id"], status_code=400, message="gone")
    socket.chunk(two["context_id"], 0, b"\x02\x00")
    socket.push(type="done", context_id=two["context_id"], done=True)

    assert await drained(alive) == b"\x02\x00"
    await voice.aclose()


async def test_a_turn_cancelled_before_its_text_was_sent_sends_no_cancel() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    voice.open_turn(transcript_id="t1").speak("First.")
    await sent(socket, 1)
    turn = voice.open_turn(transcript_id="t2")
    segment = turn.speak("Second.")

    await turn.cancel()
    assert await drained(segment) == b""
    voice.open_turn(transcript_id="t3").speak("Third.")
    [_, third] = await sent(socket, 2)

    assert "cancel" not in third
    assert len(socket.sent) == 2
    await voice.aclose()


async def test_an_offline_provider_is_dialled_once_per_failed_context() -> None:
    attempts = 0

    async def offline() -> SpeechSocket:
        nonlocal attempts
        attempts += 1
        raise ProviderFault("tts", "unavailable")

    voice = CartesiaSpeechSynthesizer(api_key="k", settings=SETTINGS, opener=offline)
    turn = voice.open_turn(transcript_id="t1")
    segments = [turn.speak(text, sentence_index=index) for index, text in enumerate("ABC")]
    await turn.finish()

    for segment in segments:
        with pytest.raises(ProviderFault):
            await drained(segment)
    assert attempts == 1
    await voice.aclose()


async def test_a_blank_sentence_is_never_sent() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))

    with pytest.raises(ValueError, match="blank"):
        await drained(voice.open_turn().speak("   "))

    assert socket.sent == []
    await voice.aclose()


async def test_a_refused_handshake_is_a_typed_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> SpeechSocket:
        raise InvalidStatus(Response(401, "Unauthorized", Headers()))

    monkeypatch.setattr(websockets, "connect", refuse)

    with pytest.raises(ProviderFault) as caught:
        await connect_cartesia("wss://example.test", "k")
    assert caught.value.kind == "unauthorized"


async def test_a_websocket_fault_moves_the_turn_to_the_fallback() -> None:
    socket = FakeSocket()
    primary = synthesizer(Opener(socket))
    backup = FallbackVoice()
    turn = SpeechWithFallback(primary, backup).open_turn(transcript_id="t1")
    segment = turn.speak("Hello.", sentence_index=0)
    reading = asyncio.ensure_future(drained(segment))
    [one] = await sent(socket, 1)

    socket.push(type="error", context_id=one["context_id"], status_code=503, message="busy")

    assert await reading == b"backup"
    await primary.aclose()


class FallbackVoice(SentenceSynth):
    @override
    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del text, sentence_index, transcript_id, language
        yield b"backup"

    async def aclose(self) -> None:
        return None
