from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from fakeyou.speech.openai import OpenAISpeechSynthesizer
from fakeyou.speech.streaming import (
    SpeechTag,
)
from tests._persistence_fakes import RecordingJournal


async def test_synthesizer_stream_yields_chunks_with_correlated_journal(
    journal: RecordingJournal, openai_client: Callable[..., AsyncOpenAI]
) -> None:

    lines = journal.lines
    client = openai_client(chunks=[b"\x00" * 64, b"\x01" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert chunks == [b"\x00" * 64, b"\x01" * 64]
    assert len(lines) == 3
    [started] = journal.events("synthesis_started")
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert started["event"] == "synthesis_started"
    assert started["streaming"] is True
    assert started["chars"] == len("Hello.")
    assert first["request_id"] == started["request_id"]
    assert first["bytes_out"] == 64
    assert finished["event"] == "synthesis_finished"
    assert finished["request_id"] == started["request_id"]
    assert finished["chars"] == len("Hello.")
    assert finished["bytes_out"] == 128
    assert finished["duration_ms"] >= 0
    assert finished["first_chunk_ms"] >= 0
    assert finished["streaming"] is True


async def test_synthesizer_stream_failure_raises_typed_fault(
    journal: RecordingJournal, openai_client: Callable[..., AsyncOpenAI]
) -> None:

    from fakeyou.observability.provider_faults import ProviderFault

    lines = journal.lines
    client = openai_client(chunks=[b"\x00" * 64], stream_error=httpx2.ReadTimeout("stalled"))
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        with pytest.raises(ProviderFault) as caught:
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "timeout"
    assert len(lines) == 3
    [first] = journal.events("synthesis_pcm_emitted")
    [failed] = journal.events("synthesis_failed")
    assert first["bytes_out"] == 64
    assert failed["event"] == "synthesis_failed"
    assert failed["kind"] == "timeout"
    assert failed["duration_ms"] >= 0
    assert failed["request_id"] == journal.events()[0]["request_id"]


async def test_first_pcm_is_reported_before_synthesis_finishes(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream: Any = synthesizer.stream("Hello.")
    try:
        assert await anext(stream) == b"\x00\x01"
        [first] = journal.events("synthesis_pcm_emitted")
        assert first["first_chunk_ms"] >= 0
        assert first["bytes_out"] == 2
        assert journal.events("synthesis_finished") == []
    finally:
        await stream.aclose()
        await synthesizer.aclose()


async def test_first_pcm_event_carries_correlation_without_content(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == [b"\x00\x01", b"\x02\x03"]
    [started] = journal.events("synthesis_started")
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    started_request = started["request_id"]
    assert set(first) == {
        "event",
        "meeting_id",
        "at",
        "source",
        "request_id",
        "first_chunk_ms",
        "bytes_out",
        "provider",
    }
    assert first["source"] == "tts"
    assert first["request_id"] == started_request
    assert finished["request_id"] == started_request
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 4
    assert finished["first_chunk_ms"] == first["first_chunk_ms"]


async def test_empty_chunks_are_skipped_before_first_pcm(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"", b"\x00\x01", b""]), journal=journal
    )
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == [b"\x00\x01"]
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 2
    assert finished["first_chunk_ms"] == first["first_chunk_ms"]


async def test_journaled_stream_skips_empty_chunks(journal: RecordingJournal) -> None:
    from fakeyou.speech.streaming import journaled_stream

    async def open_stream(text: str) -> AsyncGenerator[bytes, None]:
        assert text == "Hello."
        yield b""
        yield b"\x00\x01"
        yield b""

    chunks = [
        chunk async for chunk in journaled_stream(journal, "Hello.", open_stream, SpeechTag("test"))
    ]

    assert chunks == [b"\x00\x01"]
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 2


async def test_empty_stream_records_no_first_pcm(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(client=openai_client(chunks=[b"", b""]), journal=journal)
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == []
    assert journal.events("synthesis_pcm_emitted") == []
    [finished] = journal.events("synthesis_finished")
    assert finished["bytes_out"] == 0
    assert finished["first_chunk_ms"] is None


async def test_failure_before_first_pcm_records_no_first_pcm(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(
        api_key="k",
        client=openai_client(chunks=[], stream_error=httpx2.ReadTimeout("stalled")),
        journal=journal,
    )
    try:
        with pytest.raises(ProviderFault):
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert journal.events("synthesis_pcm_emitted") == []
    assert len(journal.events("synthesis_failed")) == 1


async def test_failure_after_first_pcm_keeps_the_first_pcm_proof(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(
        api_key="k",
        client=openai_client(chunks=[b"\x00\x01"], stream_error=httpx2.ReadTimeout("stalled")),
        journal=journal,
    )
    try:
        with pytest.raises(ProviderFault):
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    [first] = journal.events("synthesis_pcm_emitted")
    [failed] = journal.events("synthesis_failed")
    assert first["bytes_out"] == 2
    assert failed["request_id"] == first["request_id"]
    assert journal.events("synthesis_finished") == []


async def test_cancelled_stream_after_first_pcm_keeps_the_first_pcm_proof(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream: Any = synthesizer.stream("Hello.")
    try:
        assert await anext(stream) == b"\x00\x01"
    finally:
        await stream.aclose()
        await synthesizer.aclose()
    [first] = journal.events("synthesis_pcm_emitted")
    assert first["bytes_out"] == 2
    assert journal.events("synthesis_finished") == []


async def test_synthesizer_tags_sentence_synthesis_with_its_index(
    journal: RecordingJournal, openai_client: Callable[..., AsyncOpenAI]
) -> None:
    client = openai_client(chunks=[b"\x00" * 64, b"\x01" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        first = [chunk async for chunk in synthesizer.stream("First sentence.", sentence_index=0)]
        second = [chunk async for chunk in synthesizer.stream("Second sentence.", sentence_index=1)]
    finally:
        await synthesizer.aclose()

    assert first == [b"\x00" * 64, b"\x01" * 64]
    assert second == [b"\x00" * 64, b"\x01" * 64]
    assert [row["sentence_index"] for row in journal.events("synthesis_started")] == [0, 1]
    assert [row["sentence_index"] for row in journal.events("synthesis_pcm_emitted")] == [0, 1]
    assert [row["sentence_index"] for row in journal.events("synthesis_finished")] == [0, 1]
    assert [row["chars"] for row in journal.events("synthesis_started")] == [
        len("First sentence."),
        len("Second sentence."),
    ]


async def test_synthesizer_without_sentence_index_omits_the_tag(
    journal: RecordingJournal, openai_client: Callable[..., AsyncOpenAI]
) -> None:
    client = openai_client(chunks=[b"\x00" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert chunks == [b"\x00" * 64]
    for row in journal.events():
        assert "sentence_index" not in row


async def test_synthesis_tags_transcript_id_on_all_events(
    openai_client: Callable[..., AsyncOpenAI], journal: RecordingJournal
) -> None:
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream: Any = synthesizer.stream("Hello.", transcript_id="t9")
    try:
        assert await anext(stream) == b"\x00\x01"
        [started] = journal.events("synthesis_started")
        [first] = journal.events("synthesis_pcm_emitted")
        assert started["transcript_id"] == "t9"
        assert first["transcript_id"] == "t9"
    finally:
        await stream.aclose()
        await synthesizer.aclose()


async def test_failed_synthesis_records_the_provider_status(journal: RecordingJournal) -> None:
    from fakeyou.observability.provider_faults import ProviderFault
    from fakeyou.speech.streaming import journaled_stream

    async def open_stream(text: str) -> AsyncGenerator[bytes, None]:
        assert text == "Hello."
        raise ProviderFault("tts", "unavailable", retryable=True)
        yield b""

    with pytest.raises(ProviderFault):
        [_ async for _ in journaled_stream(journal, "Hello.", open_stream, SpeechTag("test"))]

    [failed] = journal.events("synthesis_failed")
    assert failed["provider_status"] is None
