import asyncio
import base64
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import websockets
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from fakeyou.config.settings import ElevenLabsTTS
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech import elevenlabs, websocket
from fakeyou.speech.contract import SpeechSegment
from fakeyou.speech.elevenlabs import (
    ElevenLabsSpeechSynthesizer,
    connect_elevenlabs,
    socket_url,
)
from fakeyou.speech.websocket import SpeechSocket
from tests._persistence_fakes import RecordingJournal

SETTINGS = ElevenLabsTTS(voice="voice-abc", model="eleven_flash_v2_5")
MULTILINGUAL = ElevenLabsTTS(voice="voice-abc")


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

    def audio(self, context: str, pcm: bytes, alignment: dict[str, Any] | None = None) -> None:
        self.push(
            audio=base64.b64encode(pcm).decode(),
            contextId=context,
            alignment=alignment,
            normalizedAlignment=None,
        )

    def final(self, context: str) -> None:
        self.push(isFinal=True, contextId=context)


class Opener:
    def __init__(self, *sockets: FakeSocket) -> None:
        self._sockets = list(sockets)
        self.languages: list[str | None] = []
        self.opening = asyncio.Event()

    async def __call__(self, language: str | None) -> SpeechSocket:
        self.languages.append(language)
        self.opening.set()
        return self._sockets.pop(0)


def synthesizer(
    opener: Opener,
    journal: RecordingJournal | None = None,
    settings: ElevenLabsTTS = SETTINGS,
) -> ElevenLabsSpeechSynthesizer:
    return ElevenLabsSpeechSynthesizer(
        api_key="k", settings=settings, language="id", journal=journal, opener=opener
    )


def spelled(text: str, starts_ms: Sequence[int], duration_ms: int) -> dict[str, Any]:
    return {
        "chars": list(text),
        "charStartTimesMs": list(starts_ms),
        "charDurationsMs": [duration_ms] * len(text),
    }


async def sent(socket: FakeSocket, count: int) -> list[dict[str, Any]]:
    async with asyncio.timeout(2):
        while len(socket.sent) < count:
            socket.arrived.clear()
            await socket.arrived.wait()
    return socket.sent


async def drained(segment: SpeechSegment) -> bytes:
    async with asyncio.timeout(2):
        return b"".join([chunk async for chunk in segment])


def test_elevenlabs_requires_an_explicit_voice() -> None:
    with pytest.raises(ValidationError):
        ElevenLabsTTS.model_validate({})


def test_the_socket_asks_for_pcm_with_alignment_in_the_turn_language() -> None:
    url = urlsplit(socket_url(SETTINGS, "id"))

    assert (url.scheme, url.netloc, url.path) == (
        "wss",
        "api.elevenlabs.io",
        "/v1/text-to-speech/voice-abc/multi-stream-input",
    )
    assert parse_qs(url.query) == {
        "model_id": ["eleven_flash_v2_5"],
        "output_format": ["pcm_24000"],
        "sync_alignment": ["true"],
        "inactivity_timeout": ["180"],
        "language_code": ["id"],
    }


def test_a_model_that_ignores_language_is_not_told_one() -> None:
    query = parse_qs(urlsplit(socket_url(MULTILINGUAL, None)).query)

    assert "language_code" not in query
    assert query["model_id"] == ["eleven_multilingual_v2"]


async def test_a_model_that_ignores_language_shares_one_socket() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener, settings=MULTILINGUAL)

    voice.open_turn(language="id").speak("Good morning.")
    voice.open_turn(language="en").speak("Hello.")
    await sent(socket, 6)

    assert opener.languages == [None]
    await voice.aclose()


async def test_the_key_travels_in_the_handshake_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    async def connect(url: str, **kwargs: Any) -> FakeSocket:
        calls.append((url, kwargs["additional_headers"]))
        return FakeSocket()

    monkeypatch.setattr(websockets, "connect", connect)

    await connect_elevenlabs(SETTINGS, "eleven-key", "en")

    [(url, headers)] = calls
    assert headers == {"xi-api-key": "eleven-key"}
    assert "eleven-key" not in url
    assert "language_code=en" in url


async def test_each_sentence_speaks_in_its_own_context_closed_after_its_text() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")

    first = turn.speak("Hello everyone.", sentence_index=0)
    second = turn.speak(" Two things today.", sentence_index=1)
    opening, text, closing, *rest = await sent(socket, 6)

    context = opening["context_id"]
    assert opening == {"text": " ", "context_id": context}
    assert text == {"text": "Hello everyone. ", "context_id": context, "flush": True}
    assert closing == {"context_id": context, "close_context": True}
    other = rest[0]["context_id"]
    assert other != context
    assert rest[1] == {"text": "Two things today. ", "context_id": other, "flush": True}
    socket.audio(other, b"\x02\x00" * 2)
    socket.audio(context, b"\x01\x00" * 4)
    socket.final(other)
    socket.final(context)

    assert await drained(first) == b"\x01\x00" * 4
    assert await drained(second) == b"\x02\x00" * 2
    await voice.aclose()


async def test_snake_case_markers_are_understood() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]

    socket.push(audio=base64.b64encode(b"\x01\x00").decode(), context_id=context)
    socket.push(is_final=True, context_id=context)

    assert await drained(segment) == b"\x01\x00"
    await voice.aclose()


async def test_words_are_spelled_from_characters_across_chunks() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hi there.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]
    half_second = b"\x00\x00" * 12000

    socket.audio(context, half_second, spelled("Hi th", [0, 100, 200, 300, 400], 100))
    socket.audio(context, half_second, spelled("ere.", [0, 100, 200, 300], 100))
    socket.final(context)
    await drained(segment)

    spoken = [(word.text, round(word.start, 3), round(word.end, 3)) for word in segment.words or ()]
    assert spoken == [("Hi", 0.0, 0.2), ("there.", 0.3, 0.9)]
    await voice.aclose()


async def test_cancel_closes_delivered_contexts_and_drops_late_audio() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn(transcript_id="t1")
    segment = turn.speak("Hello there.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]
    socket.audio(context, b"\x01\x00")
    heard = await asyncio.wait_for(anext(aiter(segment)), timeout=2)

    await turn.cancel()
    await sent(socket, 4)
    socket.audio(context, b"\x09\x00")

    assert socket.sent[-1] == {"context_id": context, "close_context": True}
    assert heard == b"\x01\x00"
    assert await drained(segment) == b""
    await voice.aclose()


async def test_a_sentence_cancelled_before_it_was_sent_never_reaches_the_socket() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)
    turn = voice.open_turn()
    segment = turn.speak("Hello there.")

    await turn.cancel()

    assert await drained(segment) == b""
    assert (opener.languages, socket.sent) == ([], [])
    await voice.aclose()


async def test_an_open_socket_never_hears_about_a_context_it_was_not_sent() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    voice.open_turn().speak("First.")
    await sent(socket, 3)
    turn = voice.open_turn()
    segment = turn.speak("Second.")

    await turn.cancel()
    assert await drained(segment) == b""
    follow = voice.open_turn().speak("Third.")
    [*_, third, _, _] = await sent(socket, 6)

    assert len(socket.sent) == 6
    assert third == {"text": " ", "context_id": third["context_id"]}
    await follow.aclose()
    await voice.aclose()


async def test_each_language_gets_its_own_socket() -> None:
    indonesian, english = FakeSocket(), FakeSocket()
    opener = Opener(indonesian, english)
    voice = synthesizer(opener)

    voice.open_turn(language="id").speak("Good morning.")
    await sent(indonesian, 3)
    voice.open_turn(language="en").speak("Hello.")
    await sent(english, 3)

    assert opener.languages == ["id", "en"]
    await voice.aclose()


async def test_warm_opens_the_default_language_socket() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)

    voice.warm()
    await asyncio.wait_for(opener.opening.wait(), timeout=2)

    assert opener.languages == ["id"]
    await voice.aclose()
    assert socket.closed


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("You have insufficient quota to complete this request.", "payment_required"),
        ("Invalid API key", "unauthorized"),
        ("max_active_conversations: Maximum simultaneous contexts exceeded (5)", "rate_limited"),
    ],
)
async def test_a_provider_error_fails_the_waiting_sentence(message: str, kind: str) -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 3)

    socket.push(message=message)

    with pytest.raises(ProviderFault) as caught:
        await drained(segment)
    assert caught.value.kind == kind
    await voice.aclose()


async def test_a_policy_close_is_a_typed_fault() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 3)

    socket.inbox.put_nowait(ConnectionClosedError(Close(1008, "invalid api key"), None))

    with pytest.raises(ProviderFault) as caught:
        await drained(segment)
    assert caught.value.kind == "unauthorized"
    await voice.aclose()


async def test_a_lost_socket_fails_the_sentence_and_the_next_one_reconnects() -> None:
    first_socket, second_socket = FakeSocket(), FakeSocket()
    opener = Opener(first_socket, second_socket)
    voice = synthesizer(opener)
    turn = voice.open_turn()
    lost = turn.speak("Hello.")
    await sent(first_socket, 3)

    first_socket.inbox.put_nowait(ConnectionClosedError(None, None))
    with pytest.raises(ProviderFault) as caught:
        await drained(lost)
    retried = turn.speak("Hello again.")
    [opening, *_] = await sent(second_socket, 3)
    second_socket.audio(opening["context_id"], b"\x01\x00")
    second_socket.final(opening["context_id"])

    assert caught.value.kind == "unavailable"
    assert await drained(retried) == b"\x01\x00"
    assert opener.languages == ["id", "id"]
    await voice.aclose()


async def test_an_error_for_a_closed_context_leaves_the_others_alone() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn()
    finished = turn.speak("Done.")
    [opening, *_] = await sent(socket, 3)
    socket.final(opening["context_id"])
    await drained(finished)
    alive = turn.speak("Still here.")
    [*_, reopening, _, _] = await sent(socket, 6)

    socket.push(message="context not found", contextId=opening["context_id"])
    socket.audio(reopening["context_id"], b"\x01\x00")
    socket.final(reopening["context_id"])

    assert await drained(alive) == b"\x01\x00"
    await voice.aclose()


async def test_blank_text_is_never_sent() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)

    with pytest.raises(ValueError, match="blank text"):
        await drained(voice.open_turn().speak(" "))
    assert opener.languages == []
    await voice.aclose()


async def test_close_fails_the_sentences_still_waiting() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 3)

    await voice.aclose()

    with pytest.raises(ProviderFault):
        await drained(segment)
    assert socket.closed


async def test_each_sentence_is_journaled_as_an_elevenlabs_synthesis() -> None:
    socket = FakeSocket()
    journal = RecordingJournal()
    voice = synthesizer(Opener(socket), journal)
    segment = voice.open_turn(transcript_id="t1").speak("Hello.", sentence_index=0)
    [opening, *_] = await sent(socket, 3)
    socket.audio(opening["context_id"], b"\x01\x00")
    socket.final(opening["context_id"])

    await drained(segment)

    [started] = journal.events("synthesis_started")
    assert (started["provider"], started["transcript_id"]) == ("elevenlabs", "t1")
    assert journal.events("synthesis_finished")
    await voice.aclose()


async def test_a_sentence_without_a_final_marker_ends_once_its_audio_goes_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(elevenlabs, "QUIET_END_SECONDS", 0.05)
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    [opening, *_] = await sent(socket, 3)

    socket.audio(opening["context_id"], b"\x01\x00")

    assert await drained(segment) == b"\x01\x00"
    await voice.aclose()


async def test_broken_timings_drop_the_words_but_keep_the_audio() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]

    socket.audio(
        context,
        b"\x01\x00",
        {"chars": ["H", "i"], "charStartTimesMs": ["x", "y"], "charDurationsMs": [1, 1]},
    )
    socket.audio(context, b"\x02\x00", spelled("Hi", [0, 50], 50))
    socket.final(context)

    assert await drained(segment) == b"\x01\x00\x02\x00"
    assert [word.text for word in segment.words or ()] == ["Hi"]
    await voice.aclose()


async def test_normalized_spelling_never_becomes_the_heard_words() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("At 25.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]

    socket.push(
        audio=base64.b64encode(b"\x01\x00").decode(),
        contextId=context,
        alignment=None,
        normalizedAlignment=spelled("twenty", [0, 1, 2, 3, 4, 5], 1),
    )
    socket.final(context)
    await drained(segment)

    assert segment.words == []
    await voice.aclose()


async def test_a_sentence_left_behind_closes_its_context() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello there.")
    [opening, *_] = await sent(socket, 3)
    context = opening["context_id"]
    socket.audio(context, b"\x01\x00")
    await asyncio.wait_for(anext(aiter(segment)), timeout=2)

    await segment.aclose()
    await sent(socket, 4)

    assert socket.sent[-1] == {"context_id": context, "close_context": True}
    await voice.aclose()


async def test_a_finished_turn_can_still_be_cancelled() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn()
    segment = turn.speak("Hello there.")
    [opening, *_] = await sent(socket, 3)

    await turn.finish()
    await turn.cancel()
    await sent(socket, 4)

    assert socket.sent[-1] == {"context_id": opening["context_id"], "close_context": True}
    assert await drained(segment) == b""
    await voice.aclose()


async def test_a_closed_voice_opens_no_new_socket() -> None:
    socket = FakeSocket()
    opener = Opener(socket)
    voice = synthesizer(opener)
    await voice.aclose()

    with pytest.raises(ProviderFault):
        await drained(voice.open_turn().speak("Too late."))
    assert opener.languages == []


async def test_an_error_for_a_live_context_fails_only_that_sentence() -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    turn = voice.open_turn()
    failing = turn.speak("One.")
    healthy = turn.speak("Two.")
    one, *_, two_text, _ = await sent(socket, 6)
    two = two_text["context_id"]

    socket.push(message="Invalid API key", contextId=one["context_id"])
    socket.audio(two, b"\x02\x00")
    socket.final(two)

    with pytest.raises(ProviderFault) as caught:
        await drained(failing)
    assert caught.value.kind == "unauthorized"
    assert await drained(healthy) == b"\x02\x00"
    await voice.aclose()


async def test_a_socket_the_server_retires_is_redialled_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket, "REDIAL_AFTER_SECONDS", 0.0)
    first, second = FakeSocket(), FakeSocket()
    opener = Opener(first, second)
    voice = synthesizer(opener)
    voice.warm()
    await asyncio.wait_for(opener.opening.wait(), timeout=2)
    opener.opening.clear()

    first.inbox.put_nowait(ConnectionClosedError(Close(1000, "idle"), None))
    await asyncio.wait_for(opener.opening.wait(), timeout=2)

    assert opener.languages == ["id", "id"]
    await voice.aclose()
    assert second.closed


@pytest.mark.parametrize(
    "message",
    [
        "You do not have permission to use this voice.",
        "Too many characters in the request.",
    ],
)
async def test_unrelated_wording_is_not_mistaken_for_a_lasting_fault(message: str) -> None:
    socket = FakeSocket()
    voice = synthesizer(Opener(socket))
    segment = voice.open_turn().speak("Hello.")
    await sent(socket, 3)

    socket.push(message=message)

    with pytest.raises(ProviderFault) as caught:
        await drained(segment)
    assert caught.value.kind not in ("unauthorized", "rate_limited", "payment_required")
    await voice.aclose()
