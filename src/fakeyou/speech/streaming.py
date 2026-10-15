from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx2

from fakeyou.observability.journal import Journal, JournalTiming
from fakeyou.observability.provider_faults import classify_exception

TTS_HTTP_TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
TTS_REQUEST_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class SpeechTag:
    provider: str
    sentence_index: int | None = None
    transcript_id: str | None = None

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"provider": self.provider}
        if self.sentence_index is not None:
            fields["sentence_index"] = self.sentence_index
        if self.transcript_id is not None:
            fields["transcript_id"] = self.transcript_id
        return fields


def _record_first_pcm(journal: Journal, timing: JournalTiming, tag: SpeechTag) -> None:
    timing.fields["first_chunk_ms"] = timing.elapsed_ms
    pcm: dict[str, Any] = {
        "request_id": timing.fields["request_id"],
        "first_chunk_ms": timing.fields["first_chunk_ms"],
        "bytes_out": timing.fields["bytes_out"],
        **tag.fields(),
    }
    journal.record("synthesis_pcm_emitted", source="tts", **pcm)


async def journaled_stream(
    journal: Journal | None,
    text: str,
    open_stream: Callable[[str], AsyncGenerator[bytes]],
    tag: SpeechTag,
) -> AsyncGenerator[bytes]:
    if not text.strip():
        raise ValueError("cannot synthesize blank text")
    journal = journal or Journal("", echo=False)
    tags: dict[str, Any] = {"chars": len(text), "streaming": True, **tag.fields()}
    with journal.timed(
        started="synthesis_started",
        finished="synthesis_finished",
        failed="synthesis_failed",
        source="tts",
        **tags,
    ) as timing:
        timing.fields.update(bytes_out=0, first_chunk_ms=None)
        try:
            async with aclosing(open_stream(text)) as chunks:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    timing.fields["bytes_out"] += len(chunk)
                    if timing.fields["first_chunk_ms"] is None:
                        _record_first_pcm(journal, timing, tag)
                    yield chunk
        except Exception as error:
            fault = classify_exception("tts", error)
            if fault is error:
                raise
            raise fault from error
