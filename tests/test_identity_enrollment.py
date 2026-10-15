from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict, Unpack
from uuid import UUID

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.settings import Settings
from fakeyou.identity.enrollment import (
    DEFAULT_LIVENESS_GATE,
    MIN_ENROLL_CLIPS,
    PROVISIONAL_SNR_FLOOR_DB,
    VOICEPRINT_MIN_RMS,
    EnrollmentAccepted,
    EnrollmentOutcome,
    EnrollmentRejected,
    EnrollSubject,
    LivenessGate,
    VoiceprintDeps,
    digit_words,
    digits_only,
    enroll_voiceprint,
    issue_challenge,
)
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import InMemoryVoiceprints
from tests._voiceprint_support import (
    CHALLENGE_DIGITS,
    CHALLENGE_ID,
    ScriptedEmbedder,
    saved,
    write_speechlike_clip,
    write_tone_clip,
)

ENGLISH_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)


class SpokenChallenge(TypedDict, total=False):
    challenge: str
    transcript: str


def gated_enroll(
    name: str,
    clips: list[str],
    spontaneous: list[str],
    *,
    settings: Settings,
    deps: VoiceprintDeps,
    **spoken: Unpack[SpokenChallenge],
) -> EnrollmentOutcome:
    gate = LivenessGate(
        CHALLENGE_ID,
        spoken.get("challenge", CHALLENGE_DIGITS),
        spoken.get("transcript", CHALLENGE_DIGITS),
        "Maya",
        tuple(spontaneous),
    )
    return enroll_voiceprint(
        EnrollSubject(name, negative=False), clips, settings=settings, deps=deps, gate=gate
    )


def spoken_word(digit: str) -> str:
    table = digit_words()
    return next(
        word
        for word, mapped in table.items()
        if mapped == digit and word not in ENGLISH_NUMBER_WORDS
    )


def spoken_phrase(digits: str) -> str:
    return " ".join(spoken_word(digit) for digit in digits)


def test_enroll_three_valid_clips_stores_record_and_journals(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    outcome = enroll_voiceprint(
        EnrollSubject("alice", negative=False),
        [str(path) for path in clips],
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
    )

    [utterances] = embedder.calls
    assert len(utterances) == 3
    assert all(len(utterance) == 16000 * 3 for utterance in utterances)
    assert outcome == EnrollmentAccepted(
        person_id="alice", n_utterances=3, dim=2, is_negative=False
    )
    record = saved(store, "alice")
    assert record.n_utterances == 3
    assert record.policy_version == "v1"
    assert record.dim == 2
    assert record.embedding == (1.0, 0.0)
    assert record.is_negative is False
    assert record.consent_ts.tzinfo is not None
    [row] = journal.events("voiceprint_recorded")
    assert row["person_id"] == "alice"
    assert row["n_utterances"] == 3
    assert row["is_negative"] is False
    assert "embedding" not in row


def test_enroll_with_negative_flag_marks_record(settings: Settings, tmp_path: Path) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(0.0, 1.0)])

    enroll_voiceprint(
        EnrollSubject("twin-voice", negative=True),
        [str(path) for path in clips],
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
    )

    assert saved(store, "twin-voice").is_negative is True
    [row] = journal.events("voiceprint_recorded")
    assert row["is_negative"] is True


def test_enroll_with_two_clips_raises_without_side_effects(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(2)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == "enroll needs at least 3 clips, got 2"
    assert store.load_all() == []
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_silent_clip_raises(settings: Settings, tmp_path: Path) -> None:
    assert VOICEPRINT_MIN_RMS == 0.01
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", amplitude=0.0),
    ]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_CLIP_SILENT)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_SILENT
    assert str(caught.value) == "clip is silent: clip-2.wav rms 0.0000 below 0.01"
    assert store.load_all() == []
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_short_clip_raises(settings: Settings, tmp_path: Path) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", seconds=1.0),
    ]
    store = InMemoryVoiceprints()

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_CLIP_TOO_SHORT)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT
    assert str(caught.value) == (
        "clip too short: clip-2.wav holds 1.00s of audio, need at least 3.0s"
    )
    assert store.load_all() == []


def test_enroll_with_8khz_clip_raises_mentioning_format(settings: Settings, tmp_path: Path) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", rate=8000),
    ]
    store = InMemoryVoiceprints()

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )
    assert store.load_all() == []


def test_enroll_with_non_wav_clip_raises_mentioning_format(
    settings: Settings, tmp_path: Path
) -> None:
    impostor = tmp_path / "clip-2.wav"
    impostor.write_bytes(b"this is not audio")
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        impostor,
    ]

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(
                InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
            ),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )


def test_enroll_with_truncated_clip_raises_mentioning_format(
    settings: Settings, tmp_path: Path
) -> None:
    broken = write_tone_clip(tmp_path / "clip-2.wav")
    raw = broken.read_bytes()
    broken.write_bytes(raw[: len(raw) // 2])
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        broken,
    ]

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED)
    ) as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(
                InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
            ),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )


def test_enroll_with_missing_clip_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        tmp_path / "clip-2.wav",
    ]
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="cannot be read") as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(InMemoryVoiceprints(), journal, ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_UNREADABLE
    assert journal.events("voiceprint_recorded") == []


def test_enroll_empty_clip_rejected_even_with_zero_minimum(
    settings: Settings, tmp_path: Path
) -> None:
    settings.identity.voiceprint_min_seconds = 0.0
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav", seconds=0.0) for index in range(3)]

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_CLIP_TOO_SHORT)
    ) as error:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False), [str(path) for path in clips], settings=settings
        )

    assert error.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT


def test_enroll_without_liveness_gate_stores_empty_owner_name(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(MIN_ENROLL_CLIPS)]
    store = InMemoryVoiceprints()

    enroll_voiceprint(
        EnrollSubject("alice", negative=False),
        [str(path) for path in clips],
        gate=DEFAULT_LIVENESS_GATE,
        settings=settings,
        deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert saved(store, "alice").owner_name == ""


def test_enroll_partial_liveness_args_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="gated enrollment needs") as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            clips,
            settings=settings,
            deps=VoiceprintDeps(InMemoryVoiceprints(), journal, ScriptedEmbedder([(1.0, 0.0)])),
            gate=LivenessGate(challenge_id=CHALLENGE_ID),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE
    assert journal.events() == []


def test_enroll_owner_name_without_challenge_flags_raises(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="gated enrollment needs") as caught:
        enroll_voiceprint(
            EnrollSubject("alice", negative=False),
            clips,
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
            gate=LivenessGate(owner_name="Maya"),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE
    assert store.load_all() == []
    assert journal.events() == []


def test_challenge_issues_unique_eight_digit_codes() -> None:
    first = issue_challenge()
    second = issue_challenge()

    assert re.fullmatch(r"[0-9]{8}", first.challenge) is not None
    assert re.fullmatch(r"[0-9]{8}", second.challenge) is not None
    UUID(first.challenge_id)
    UUID(second.challenge_id)
    assert (first.challenge, first.challenge_id) != (second.challenge, second.challenge_id)


def test_enroll_matching_challenge_accepts_with_owner_and_journals_acceptance(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert outcome == EnrollmentAccepted(
        person_id="alice",
        n_utterances=3,
        dim=2,
        is_negative=False,
        challenge_id=CHALLENGE_ID,
        owner_name="Maya",
    )
    record = saved(store, "alice")
    assert record.owner_name == "Maya"
    assert record.n_utterances == 3
    [row] = journal.events("enrollment_accepted")
    assert row["person_id"] == "alice"
    assert row["challenge_id"] == CHALLENGE_ID
    assert row["snr_db"] >= PROVISIONAL_SNR_FLOOR_DB
    assert row["seconds"] >= 30.0
    assert row["segments"] == {"read": 2, "spontaneous": 1}
    assert "transcript" not in row
    assert CHALLENGE_DIGITS not in str(row)
    assert len(journal.events("voiceprint_recorded")) == 1
    assert journal.events("enrollment_rejected") == []


def test_enroll_transcript_two_edits_away_rejects_without_store(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [
        str(write_speechlike_clip(tmp_path / f"read-{index}.wav", seconds=3.0))
        for index in range(2)
    ]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", seconds=3.0))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
        transcript="12345699",
    )

    assert isinstance(outcome, EnrollmentRejected)
    assert outcome.challenge_id == CHALLENGE_ID
    assert outcome.reason
    assert store.load_all() == []
    assert embedder.calls == []
    [row] = journal.events("enrollment_rejected")
    assert row["challenge_id"] == CHALLENGE_ID
    assert journal.events("enrollment_accepted") == []
    assert journal.events("voiceprint_recorded") == []


def test_enroll_short_total_duration_rejects(settings: Settings, tmp_path: Path) -> None:
    clips = [
        str(write_speechlike_clip(tmp_path / f"read-{index}.wav", seconds=3.0))
        for index in range(2)
    ]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", seconds=3.0))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert isinstance(outcome, EnrollmentRejected)
    assert "30" in outcome.reason
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["seconds"] < 30.0
    assert row["segments"] == {"read": 2, "spontaneous": 1}


def test_enroll_low_snr_rejects(settings: Settings, tmp_path: Path) -> None:
    assert PROVISIONAL_SNR_FLOOR_DB == 10.0
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", noise=0.45))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert isinstance(outcome, EnrollmentRejected)
    assert "snr" in outcome.reason.lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["snr_db"] < PROVISIONAL_SNR_FLOOR_DB


def test_enroll_without_spontaneous_rejects(settings: Settings, tmp_path: Path) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        [],
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert isinstance(outcome, EnrollmentRejected)
    assert "spontaneous" in outcome.reason.lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["segments"] == {"read": 3, "spontaneous": 0}


def test_enroll_single_edit_accepts(settings: Settings, tmp_path: Path) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        transcript="12345679",
    )

    assert isinstance(outcome, EnrollmentAccepted)
    assert saved(store, "alice").owner_name == "Maya"
    assert len(journal.events("enrollment_accepted")) == 1


def test_enroll_empty_normalized_challenge_rejects_without_store(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        challenge="no digits here",
        transcript="nothing spoken",
    )

    assert isinstance(outcome, EnrollmentRejected)
    assert "digit" in outcome.reason.lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["challenge_id"] == CHALLENGE_ID
    assert journal.events("enrollment_accepted") == []


def test_enroll_worded_transcript_accepts(settings: Settings, tmp_path: Path) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    outcome = gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        transcript=spoken_phrase(CHALLENGE_DIGITS),
    )

    assert isinstance(outcome, EnrollmentAccepted)
    assert saved(store, "alice").owner_name == "Maya"
    assert len(journal.events("enrollment_accepted")) == 1


def test_digits_only_maps_spelled_out_words_to_digits() -> None:
    spoken = spoken_phrase("216")

    assert not any(char.isdigit() for char in spoken)
    assert digits_only(spoken) == "216"


def test_digits_only_mixes_digits_and_words() -> None:
    assert digits_only(f"1 {spoken_word('2')}, three!") == "123"


def test_digits_only_drops_unknown_words() -> None:
    assert digits_only("abc def 7 ghi") == "7"
    assert digits_only("please call two now") == "2"


def test_digits_only_empty_holds_no_digits() -> None:
    assert digits_only("") == ""
    assert digits_only("... !!!") == ""
    assert digits_only("nothing here") == ""


def test_digit_words_asset_maps_english_digits() -> None:
    table = digit_words()

    assert len(table) == 20
    for digit, word in enumerate(ENGLISH_NUMBER_WORDS):
        assert table[word] == str(digit)


def test_digit_words_asset_maps_each_digit_in_second_locale() -> None:
    table = digit_words()

    for digit in "0123456789":
        others = [
            word
            for word, mapped in table.items()
            if mapped == digit and word not in ENGLISH_NUMBER_WORDS
        ]
        assert len(others) == 1
        assert others[0].isascii()
        assert others[0].isalpha()


def test_digit_words_missing_file_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"

    assert digit_words(path=missing) == {}
    assert digits_only("two three", path=missing) == ""
    assert digits_only("42", path=missing) == "42"


def test_digit_words_broken_file_fails_closed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    listed = tmp_path / "listed.json"
    listed.write_text('["zero"]')

    assert digit_words(path=broken) == {}
    assert digit_words(path=listed) == {}
