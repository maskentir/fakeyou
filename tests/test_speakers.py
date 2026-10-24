from fakeyou.foundation.speakers import (
    UNIDENTIFIED_PARTICIPANT,
    speaker_name,
    voice_participant_id,
)


def test_every_participant_gets_one_spoken_name() -> None:
    assert speaker_name("unknown") == UNIDENTIFIED_PARTICIPANT
    assert speaker_name(voice_participant_id("B")) == "Speaker B with unverified identity"
    assert speaker_name("speaker-b", "Maya") == "Speaker B, shown as Maya, unverified"
    assert speaker_name("spaces/x/devices/7") == "spaces/x/devices/7"
