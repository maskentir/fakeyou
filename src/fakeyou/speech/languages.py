from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpeechLanguage:
    code: str
    display: str
    english: str


SPEECH_LANGUAGES = {
    "id": SpeechLanguage("id", "Bahasa Indonesia", "Indonesian"),
    "en": SpeechLanguage("en", "English", "English"),
}
