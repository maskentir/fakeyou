from collections.abc import AsyncIterator

from fakeyou.speech.contract import PreparedSpeech


class FakePreparedSpeech:
    def __init__(
        self,
        text: str,
        language: str | None,
        sentence_index: int | None,
        transcript_id: str | None,
    ) -> None:
        self._text = text
        self._language = language
        self._sentence_index = sentence_index
        self._transcript_id = transcript_id
        self.closed = False

    @property
    def text(self) -> str:
        return self._text

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def sentence_index(self) -> int | None:
        return self._sentence_index

    @property
    def transcript_id(self) -> str | None:
        return self._transcript_id

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class PreparedSpeaker:
    async def speak(
        self,
        text: str,
        /,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        raise NotImplementedError

    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech:
        return FakePreparedSpeech(text, language, sentence_index, transcript_id)

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        await self.speak(
            prepared.text,
            sentence_index=prepared.sentence_index,
            transcript_id=prepared.transcript_id,
            language=prepared.language,
        )


class PreparedSink:
    async def prepare(self, sentence: str, language: str) -> PreparedSpeech | None:
        return FakePreparedSpeech(sentence, language, None, None)
