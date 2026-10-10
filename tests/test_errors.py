import re
import string

import pytest

from fakeyou.catalog.errors import CATALOG, ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode


class Placeholder:
    def __format__(self, format_spec: str) -> str:
        return "value"

    def __repr__(self) -> str:
        return "value"


def _field_names(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def _error(code: ErrorCode) -> FakeYouError:
    fields = {name: Placeholder() for name in _field_names(CATALOG[code].template)}
    return FakeYouError(code, **fields)


def test_catalog_has_exactly_one_entry_per_code() -> None:
    assert set(CATALOG) == set(ErrorCode)


def test_code_values_are_dotted_and_unique() -> None:
    values = [code.value for code in ErrorCode]

    assert len(set(values)) == len(values)
    assert all(re.fullmatch(r"[a-z_]+\.[a-z_]+", value) for value in values)


def test_code_member_names_follow_their_values() -> None:
    assert all(code.name == code.value.replace(".", "_").upper() for code in ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_templates_name_only_plain_fields(code: ErrorCode) -> None:
    names = {
        name
        for _, name, _, _ in string.Formatter().parse(CATALOG[code].template)
        if name is not None
    }

    assert "" not in names
    assert all(name.isidentifier() for name in names)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_every_template_renders_without_leftover_braces(code: ErrorCode) -> None:
    message = str(_error(code))

    assert message
    assert "{" not in message
    assert "}" not in message


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_error_carries_the_exit_code_of_its_spec(code: ErrorCode) -> None:
    error = _error(code)

    assert error.code is code
    assert error.exit_code is CATALOG[code].exit_code
    assert isinstance(error.exit_code, ExitCode)


def test_missing_database_url_names_the_setting() -> None:
    assert str(FakeYouError(ErrorCode.DATABASE_URL_MISSING)) == (
        "Set FAKEYOU_DATABASE__URL in .env before using this command."
    )


def test_schema_outdated_points_at_the_upgrade_command() -> None:
    error = FakeYouError(ErrorCode.DATABASE_SCHEMA_OUTDATED, found="0000", expected="0001")

    assert str(error) == "database schema is at 0000, expected 0001; run `fakeyou db upgrade`"


def test_format_specs_are_applied_by_the_catalog() -> None:
    error = FakeYouError(
        ErrorCode.VOICEPRINT_CLIP_TOO_SHORT, label="a.wav", seconds=0.5, minimum=3.0
    )

    assert str(error) == "clip too short: a.wav holds 0.50s of audio, need at least 3.0s"


def test_conversion_flag_is_applied_by_the_catalog() -> None:
    error = FakeYouError(ErrorCode.WORKER_MEETING_ID_INVALID, meeting_id="a b")

    assert str(error) == "invalid meeting ID: 'a b'"


def test_exit_code_classification_is_deliberate() -> None:
    rejected = {code for code, spec in CATALOG.items() if spec.exit_code is ExitCode.INPUT_REJECTED}

    assert rejected == {
        ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE,
        ErrorCode.AUDIO_ROOT_MISSING,
        ErrorCode.CALIBRATE_JOURNAL_LINE_INVALID,
        ErrorCode.CALIBRATE_JOURNAL_UNREADABLE,
        ErrorCode.CALIBRATE_MODEL_DIGEST_MISSING,
        ErrorCode.CALIBRATE_TRIAL_FLAGS_INVALID,
        ErrorCode.CALIBRATE_TRIAL_LABEL_INVALID,
        ErrorCode.CALIBRATE_TRIAL_NOT_OBJECT,
        ErrorCode.CALIBRATE_TRIAL_SCORES_INVALID,
        ErrorCode.CALIBRATE_TRIAL_SPEAKER_MISSING,
        ErrorCode.CHAT_THREAD_INVALID,
        ErrorCode.CONFIG_EMBEDDING_DIM_MISMATCH,
        ErrorCode.CONFIG_INVALID,
        ErrorCode.CONFIG_SECRET_MISSING,
        ErrorCode.CONFIG_UNKNOWN_KEY,
        ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED,
        ErrorCode.CONFIG_WORKER_DATABASE_URL_MISSING,
        ErrorCode.DATABASE_URL_MISSING,
        ErrorCode.DATABASE_URL_SCHEME,
        ErrorCode.DEBRIEF_CONTEXT_INVALID,
        ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE,
        ErrorCode.DEBRIEF_CONTEXT_UNREADABLE,
        ErrorCode.DEBRIEF_RECORD_MISSING,
        ErrorCode.DIAGNOSTICS_JOURNAL_MISSING,
        ErrorCode.DIARIZE_CORRECTION_MISSING,
        ErrorCode.DIARIZE_INPUT_UNREADABLE,
        ErrorCode.DIARIZE_STALE,
        ErrorCode.INPUT_INVALID,
        ErrorCode.MEETING_ALREADY_ENDED,
        ErrorCode.MEETING_AUDIO_REQUIRES_WORKER,
        ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE,
        ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE,
        ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID,
        ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA,
        ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE,
        ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
        ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH,
        ErrorCode.MEETING_BRIEF_TOO_LARGE,
        ErrorCode.MEETING_BRIEF_UNREADABLE,
        ErrorCode.MEETING_CANNOT_RESUME_PHASE,
        ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH,
        ErrorCode.MEETING_CONTEXT_DIFFERS,
        ErrorCode.MEETING_CONTEXT_TOO_LARGE,
        ErrorCode.MEETING_CONTEXT_UNREADABLE,
        ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO,
        ErrorCode.MEETING_DURATION_NOT_POSITIVE,
        ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT,
        ErrorCode.MEETING_LINK_INVALID,
        ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE,
        ErrorCode.MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY,
        ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE,
        ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA,
        ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST,
        ErrorCode.MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES,
        ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH,
        ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE,
        ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO,
        ErrorCode.MEETING_STARTED_WITH_BRIEF,
        ErrorCode.MEETING_UNFINISHED_SESSION,
        ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE,
        ErrorCode.MEMORY_EPISODE_NOT_UUID,
        ErrorCode.MEMORY_FORGET_TARGET_REQUIRED,
        ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH,
        ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING,
        ErrorCode.MEMORY_NOTE_TEXT_BLANK,
        ErrorCode.MEMORY_PERSONA_UNREADABLE,
        ErrorCode.MEMORY_RECORD_MISSING,
        ErrorCode.MEMORY_SOURCE_AT_FUTURE,
        ErrorCode.MEMORY_SOURCE_AT_INVALID,
        ErrorCode.MEMORY_SOURCE_AT_NAIVE,
        ErrorCode.NOTE_BODY_BLANK,
        ErrorCode.NOTE_CORRECTION_NOT_NEWER,
        ErrorCode.NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE,
        ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN,
        ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY,
        ErrorCode.NOTE_EPISODE_UUID_BLANK,
        ErrorCode.NOTE_GROUP_ID_INVALID,
        ErrorCode.NOTE_HAS_PENDING_CORRECTION,
        ErrorCode.NOTE_ID_NOT_UUID,
        ErrorCode.NOTE_IN_CORRECTION_CHAIN,
        ErrorCode.NOTE_SOURCE_AT_FUTURE,
        ErrorCode.NOTE_TIME_NOT_AWARE,
        ErrorCode.PERSONA_IDENTITY_MISMATCH,
        ErrorCode.VISUAL_PROVIDER_UNSUPPORTED,
        ErrorCode.VISUAL_ROOT_MISSING,
        ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED,
        ErrorCode.VOICEPRINT_CALIBRATION_INVALID,
        ErrorCode.VOICEPRINT_CLIP_SILENT,
        ErrorCode.VOICEPRINT_CLIP_TOO_SHORT,
        ErrorCode.VOICEPRINT_CLIP_UNREADABLE,
        ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE,
        ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS,
        ErrorCode.VOICEPRINT_MODEL_DIGEST_MISMATCH,
        ErrorCode.VOICEPRINT_MODEL_PATH_MISSING,
        ErrorCode.VOICEPRINT_NOT_ENROLLED,
        ErrorCode.WORKER_FILE_MISSING,
        ErrorCode.WORKER_FILE_NOT_VISIBLE,
        ErrorCode.WORKER_INGEST_NO_FINISHED_SESSION,
        ErrorCode.WORKER_MEETING_ID_INVALID,
        ErrorCode.WORKER_NOT_RUNNING,
        ErrorCode.WORKER_TIMEOUT_NEGATIVE,
    }
    assert {code for code, spec in CATALOG.items() if spec.exit_code is ExitCode.INFRA_FAILURE} == {
        ErrorCode.DATABASE_UNREACHABLE,
        ErrorCode.DOCKER_PS_FAILED,
        ErrorCode.DOCKER_UNAVAILABLE,
        ErrorCode.MEMORY_UNAVAILABLE,
        ErrorCode.PROVIDER_UNAVAILABLE,
    }
    assert {
        code for code, spec in CATALOG.items() if spec.exit_code is ExitCode.CALIBRATION_REJECTED
    } == {
        ErrorCode.CALIBRATE_COHORT_NO_SPREAD,
        ErrorCode.CALIBRATE_DEV_TRIALS_INCOMPLETE,
        ErrorCode.CALIBRATE_MARGINS_NO_VALLEY,
        ErrorCode.CALIBRATE_MARGIN_FLOOR_OUTSIDE_VALLEY,
        ErrorCode.CALIBRATE_NO_OPERATING_POINT,
        ErrorCode.CALIBRATE_RELIABILITY_EXCEEDS_BOUND,
        ErrorCode.CALIBRATE_THETA_OUTSIDE_VALLEY,
    }
