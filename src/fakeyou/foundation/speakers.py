from string import ascii_lowercase

TWIN_SPEAKER = "Digital twin"
UNKNOWN_PARTICIPANT = "unknown"
UNIDENTIFIED_PARTICIPANT = "Speaker with unverified identity"
VOICE_SPEAKER_SUFFIX = " with unverified identity"
VOICE_PARTICIPANT_PREFIX = "speaker-"


def voice_participant_id(label: str) -> str:
    return VOICE_PARTICIPANT_PREFIX + label.lower()


def voice_speaker_name(participant_id: str, display_claim: str | None = None) -> str | None:
    label = participant_id.removeprefix(VOICE_PARTICIPANT_PREFIX)
    if label == participant_id or not label or not set(label) <= set(ascii_lowercase):
        return None
    name = f"Speaker {label.upper()}"
    if display_claim is None:
        return name
    return f"{name}, shown as {display_claim}, unverified"


def speaker_name(participant_id: str, display_claim: str | None = None) -> str:
    if participant_id == UNKNOWN_PARTICIPANT:
        return UNIDENTIFIED_PARTICIPANT
    voice = voice_speaker_name(participant_id, display_claim)
    if voice is None:
        return participant_id
    return voice if display_claim is not None else voice + VOICE_SPEAKER_SUFFIX
