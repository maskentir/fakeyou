from dataclasses import dataclass
from enum import StrEnum

from fakeyou.catalog.commands import Command
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.exit_codes import ExitCode


class ErrorCode(StrEnum):
    INPUT_INVALID = "input.invalid"
    SESSION_ID_INVALID = "session.id_invalid"
    DATABASE_URL_MISSING = "database.url_missing"
    DATABASE_URL_SCHEME = "database.url_scheme"
    DATABASE_UNREACHABLE = "database.unreachable"
    DATABASE_SCHEMA_OUTDATED = "database.schema_outdated"
    DATABASE_SCHEMA_DRIFT = "database.schema_drift"
    DATABASE_SCHEMA_UNVERSIONED = "database.schema_unversioned"
    DATABASE_STAMP_REFUSED = "database.stamp_refused"
    CONFIG_SECRET_MISSING = "config.secret_missing"
    CONFIG_PERCEPTION_BASE_URL_REQUIRED = "config.perception_base_url_required"
    CONFIG_EMBEDDING_DIM_MISMATCH = "config.embedding_dim_mismatch"
    CONFIG_UNKNOWN_KEY = "config.unknown_key"
    CONFIG_INVALID = "config.invalid"
    VOICEPRINT_MODEL_PATH_MISSING = "voiceprint.model_path_missing"
    VOICEPRINT_CALIBRATION_INVALID = "voiceprint.calibration_invalid"
    VOICEPRINT_CLIP_UNREADABLE = "voiceprint.clip_unreadable"
    VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED = "voiceprint.audio_format_unsupported"
    VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE = "voiceprint.enrollment_gate_incomplete"
    VOICEPRINT_NOT_ENROLLED = "voiceprint.not_enrolled"
    VOICEPRINT_ENROLL_TOO_FEW_CLIPS = "voiceprint.enroll_too_few_clips"
    VOICEPRINT_CLIP_TOO_SHORT = "voiceprint.clip_too_short"
    VOICEPRINT_CLIP_SILENT = "voiceprint.clip_silent"
    VOICEPRINT_MODEL_DIGEST_MISMATCH = "voiceprint.model_digest_mismatch"
    VOICEPRINT_EXTRA_MISSING = "voiceprint.extra_missing"
    CHAT_THREAD_INVALID = "chat.thread_invalid"
    CHAT_FAILED = "chat.failed"
    MEET_CHAT_TOO_LONG = "meet_chat.too_long"
    MEET_CHAT_RATE_LIMITED = "meet_chat.rate_limited"
    MEET_CHAT_EVIDENCE_REJECTED = "meet_chat.evidence_rejected"
    MEETING_LINK_INVALID = "meeting.link_invalid"
    MEETING_LOBBY_TIMEOUT_NOT_POSITIVE = "meeting.lobby_timeout_not_positive"
    MEETING_DURATION_NOT_POSITIVE = "meeting.duration_not_positive"
    MEETING_SPEAK_REQUIRES_AUDIO = "meeting.speak_requires_audio"
    MEETING_CONVERSE_REQUIRES_AUDIO = "meeting.converse_requires_audio"
    MEETING_MEMORY_REQUIRES_CONVERSE = "meeting.memory_requires_converse"
    MEETING_PERSONA_REQUIRES_CONVERSE = "meeting.persona_requires_converse"
    MEETING_MEMORY_REQUIRES_PERSONA = "meeting.memory_requires_persona"
    MEETING_BRIEF_REQUIRES_CONVERSE = "meeting.brief_requires_converse"
    MEETING_BRIEF_REQUIRES_PERSONA = "meeting.brief_requires_persona"
    MEETING_BRIEF_REQUIRES_MEETING_ID = "meeting.brief_requires_meeting_id"
    MEETING_WAKE_PHRASE_REQUIRES_CONVERSE = "meeting.wake_phrase_requires_converse"
    MEETING_BARGE_IN_REQUIRES_CONVERSE = "meeting.barge_in_requires_converse"
    MEETING_PERSONA_LANGUAGE_MISMATCH = "meeting.persona_language_mismatch"
    MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST = "meeting.memory_requires_recall_or_ingest"
    MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY = "meeting.memory_not_allowed_brief_only"
    MEETING_AUDIO_REQUIRES_WORKER = "meeting.audio_requires_worker"
    MEETING_BRIEF_UNREADABLE = "meeting.brief_unreadable"
    MEETING_BRIEF_SCOPE_MISMATCH = "meeting.brief_scope_mismatch"
    MEETING_BRIEF_TOO_LARGE = "meeting.brief_too_large"
    MEETING_BRIEF_SCHEMA_INVALID = "meeting.brief_schema_invalid"
    MEETING_STARTED_WITH_BRIEF = "meeting.started_with_brief"
    MEETING_CONTEXT_DIFFERS = "meeting.context_differs"
    MEETING_CONTEXT_UNREADABLE = "meeting.context_unreadable"
    MEETING_CONTEXT_TOO_LARGE = "meeting.context_too_large"
    MEETING_CONTEXT_NOT_WRITTEN = "meeting.context_not_written"
    MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT = "meeting.has_conversation_without_context"
    MEETING_UNFINISHED_SESSION = "meeting.unfinished_session"
    MEETING_ALREADY_ENDED = "meeting.already_ended"
    MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE = "meeting.conversation_checkpoint_uncheckable"
    MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE = "meeting.lifecycle_checkpoint_uncheckable"
    MEETING_CHECKPOINT_IDENTITY_MISMATCH = "meeting.checkpoint_identity_mismatch"
    MEETING_CANNOT_RESUME_PHASE = "meeting.cannot_resume_phase"
    BROWSER_PROFILE_IN_USE = "browser.profile_in_use"
    RECORD_LOCKED = "record.locked"
    RECORD_LOCK_CORRUPT = "record.lock_corrupt"
    LEASE_LOCKED = "lease.locked"
    LEASE_LOST = "lease.lost"
    INGESTION_STATUS_CORRUPT = "ingestion.status_corrupt"
    ACTION_LEDGER_LOCKED = "action_ledger.locked"
    MEMORY_PERSONA_UNREADABLE = "memory.persona_unreadable"
    MEMORY_SOURCE_AT_NAIVE = "memory.source_at_naive"
    MEMORY_SOURCE_AT_FUTURE = "memory.source_at_future"
    MEMORY_SOURCE_AT_INVALID = "memory.source_at_invalid"
    MEMORY_NOTE_TEXT_BLANK = "memory.note_text_blank"
    MEMORY_EPISODE_NOT_UUID = "memory.episode_not_uuid"
    MEMORY_RECORD_MISSING = "memory.record_missing"
    MEMORY_INGESTION_GROUP_MISMATCH = "memory.ingestion_group_mismatch"
    MEMORY_FORGET_TARGET_REQUIRED = "memory.forget_target_required"
    MEMORY_KEEP_RECORD_REQUIRES_MEETING = "memory.keep_record_requires_meeting"
    MEMORY_FORGET_INCOMPLETE = "memory.forget_incomplete"
    MEMORY_FORGET_FILES_INCOMPLETE = "memory.forget_files_incomplete"
    NOTE_CORRECTION_TARGET_INACTIVE = "note.correction_target_inactive"
    NOTE_NOT_PENDING = "note.not_pending"
    NOTE_CORRECTION_CONCURRENT = "note.correction_concurrent"
    NOTE_ID_IN_USE = "note.id_in_use"
    NOTE_ALREADY_SUPERSEDED = "note.already_superseded"
    NOTE_PAYLOAD_MISMATCH = "note.payload_mismatch"
    DEBRIEF_OWNERSHIP_UNVERIFIABLE = "debrief.ownership_unverifiable"
    DEBRIEF_SCOPE_MISMATCH = "debrief.scope_mismatch"
    DEBRIEF_RECORD_MISSING = "debrief.record_missing"
    DEBRIEF_PUBLICATION_RECOVERY = "debrief.publication_recovery"
    DEBRIEF_CACHED_SCOPE_MISMATCH = "debrief.cached_scope_mismatch"
    DEBRIEF_SOURCES_CHANGED = "debrief.sources_changed"
    DEBRIEF_FAILED = "debrief.failed"
    DEBRIEF_LABELS_INVALID = "debrief.labels_invalid"
    CALIBRATE_TRIAL_NOT_OBJECT = "calibrate.trial_not_object"
    CALIBRATE_TRIAL_SPEAKER_MISSING = "calibrate.trial_speaker_missing"
    CALIBRATE_TRIAL_LABEL_INVALID = "calibrate.trial_label_invalid"
    CALIBRATE_TRIAL_SCORES_INVALID = "calibrate.trial_scores_invalid"
    CALIBRATE_TRIAL_FLAGS_INVALID = "calibrate.trial_flags_invalid"
    CALIBRATE_JOURNAL_UNREADABLE = "calibrate.journal_unreadable"
    CALIBRATE_JOURNAL_LINE_INVALID = "calibrate.journal_line_invalid"
    CALIBRATE_MODEL_DIGEST_MISSING = "calibrate.model_digest_missing"
    CALIBRATE_DEV_TRIALS_INCOMPLETE = "calibrate.dev_trials_incomplete"
    CALIBRATE_COHORT_NO_SPREAD = "calibrate.cohort_no_spread"
    CALIBRATE_RELIABILITY_EXCEEDS_BOUND = "calibrate.reliability_exceeds_bound"
    CALIBRATE_NO_OPERATING_POINT = "calibrate.no_operating_point"
    CALIBRATE_THETA_OUTSIDE_VALLEY = "calibrate.theta_outside_valley"
    CALIBRATE_MARGINS_NO_VALLEY = "calibrate.margins_no_valley"
    CALIBRATE_MARGIN_FLOOR_OUTSIDE_VALLEY = "calibrate.margin_floor_outside_valley"
    CALIBRATE_ARTIFACT_NOT_WRITTEN = "calibrate.artifact_not_written"
    AUDIO_RETENTION_DAYS_NOT_POSITIVE = "audio.retention_days_not_positive"
    AUDIO_ROOT_MISSING = "audio.root_missing"
    AUDIO_PURGE_FAILED = "audio.purge_failed"
    VISUAL_RETENTION_DAYS_NOT_POSITIVE = "visual.retention_days_not_positive"
    VISUAL_ROOT_MISSING = "visual.root_missing"
    VISUAL_PURGE_FAILED = "visual.purge_failed"
    DIAGNOSTICS_JOURNAL_MISSING = "diagnostics.journal_missing"
    DIAGNOSTICS_REPORT_FAILED = "diagnostics.report_failed"
    DIARIZE_INPUT_UNREADABLE = "diarize.input_unreadable"
    DIARIZE_CORRECTION_MISSING = "diarize.correction_missing"
    DIARIZE_TRANSCRIPTION_FAILED = "diarize.transcription_failed"
    DIARIZE_TRANSCRIPTION_RESPONSE_INVALID = "diarize.transcription_response_invalid"
    DIARIZE_SIDECAR_INVALID = "diarize.sidecar_invalid"
    DIARIZE_SCHEMA_UNSUPPORTED = "diarize.schema_unsupported"
    DIARIZE_TURNS_INVALID = "diarize.turns_invalid"
    DIARIZE_STALE = "diarize.stale"
    DOCKER_UNAVAILABLE = "docker.unavailable"
    OPERATION_INTERRUPTED = "operation.interrupted"
    INTERNAL_FAILURE = "internal.failure"
    PROVIDER_UNAVAILABLE = "provider.unavailable"
    MEMORY_UNAVAILABLE = "memory.unavailable"
    DIARIZE_SLOT_METADATA_INVALID = "diarize.slot_metadata_invalid"
    DIARIZE_SLOT_MISALIGNED = "diarize.slot_misaligned"
    DIARIZE_SLOT_NOT_AT_ZERO = "diarize.slot_not_at_zero"
    DIARIZE_SPEAKER_LABEL_INVALID = "diarize.speaker_label_invalid"
    INGESTION_EPISODE_NAME_INVALID = "ingestion.episode_name_invalid"
    ACTION_LEDGER_UNREADABLE = "action_ledger.unreadable"
    ACTION_LEDGER_MEETING_MISMATCH = "action_ledger.meeting_mismatch"
    ACTION_LEDGER_INTENT_UNKNOWN = "action_ledger.intent_unknown"
    BROWSER_DEVTOOLS_UNAVAILABLE = "browser.devtools_unavailable"
    BROWSER_MCP_MISSING = "browser.mcp_missing"
    BROWSER_NODE_MISSING = "browser.node_missing"
    BROWSER_TOOLS_FAILED = "browser.tools_failed"
    DATABASE_SCHEMA_EMPTY = "database.schema_empty"
    DATABASE_SCHEMA_HEADS_INVALID = "database.schema_heads_invalid"
    MEETING_BRIEF_REQUIRES_PROFILE = "meeting.brief_requires_profile"
    NOTE_GROUP_ID_INVALID = "note.group_id_invalid"
    NOTE_ID_NOT_UUID = "note.id_not_uuid"
    NOTE_TIME_NOT_AWARE = "note.time_not_aware"
    NOTE_STORED_FIELD_INVALID = "note.stored_field_invalid"
    NOTE_STORED_STATUS_INVALID = "note.stored_status_invalid"
    NOTE_STORED_IDENTITY_INVALID = "note.stored_identity_invalid"
    NOTE_RECORD_MISSING = "note.record_missing"
    NOTE_CORRECTION_TARGET_UNKNOWN = "note.correction_target_unknown"
    NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE = "note.correction_target_not_owner_note"
    NOTE_CORRECTION_NOT_NEWER = "note.correction_not_newer"
    NOTE_TARGET_SNAPSHOT_MISSING = "note.target_snapshot_missing"
    NOTE_EPISODE_MISSING = "note.episode_missing"
    NOTE_EPISODE_NOT_IN_MEMORY = "note.episode_not_in_memory"
    NOTE_NO_READABLE_FACTS = "note.no_readable_facts"
    NOTE_NO_USABLE_FACTS = "note.no_usable_facts"
    NOTE_BODY_BLANK = "note.body_blank"
    NOTE_SOURCE_AT_FUTURE = "note.source_at_future"
    NOTE_EPISODE_UUID_BLANK = "note.episode_uuid_blank"
    NOTE_HAS_PENDING_CORRECTION = "note.has_pending_correction"
    NOTE_IN_CORRECTION_CHAIN = "note.in_correction_chain"
    DEBRIEF_SOURCE_TIME_NOT_AWARE = "debrief.source_time_not_aware"
    DEBRIEF_RECORD_UNREADABLE = "debrief.record_unreadable"
    DEBRIEF_RECORD_INVALID_LINE = "debrief.record_invalid_line"
    DEBRIEF_TRANSCRIPT_ID_BLANK = "debrief.transcript_id_blank"
    DEBRIEF_TRANSCRIPT_CONTRADICTORY = "debrief.transcript_contradictory"
    DEBRIEF_VISUAL_ID_BLANK = "debrief.visual_id_blank"
    DEBRIEF_VISUAL_CONTRADICTORY = "debrief.visual_contradictory"
    DEBRIEF_NO_SPEECH_SOURCES = "debrief.no_speech_sources"
    DEBRIEF_CONTEXT_UNREADABLE = "debrief.context_unreadable"
    DEBRIEF_CONTEXT_TOO_LARGE = "debrief.context_too_large"
    DEBRIEF_CONTEXT_INVALID = "debrief.context_invalid"
    DEBRIEF_CHUNK_LIMITS_INVALID = "debrief.chunk_limits_invalid"
    DEBRIEF_NO_SOURCES = "debrief.no_sources"
    DEBRIEF_SOURCE_EXCEEDS_CHUNK = "debrief.source_exceeds_chunk"
    DEBRIEF_TOO_MANY_CHUNKS = "debrief.too_many_chunks"
    DEBRIEF_EVIDENCE_MISSING = "debrief.evidence_missing"
    DEBRIEF_EVIDENCE_INVALID = "debrief.evidence_invalid"
    DEBRIEF_DRAFT_INVALID = "debrief.draft_invalid"
    DEBRIEF_CLAIM_NOT_IN_QUOTES = "debrief.claim_not_in_quotes"
    DEBRIEF_FINDING_NOT_IN_BRIEF = "debrief.finding_not_in_brief"
    DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT = "debrief.addressed_needs_participant"
    DEBRIEF_NATIVE_RESPONSE_ABSENT = "debrief.native_response_absent"
    DEBRIEF_NATIVE_SCHEMA_INVALID = "debrief.native_schema_invalid"
    DEBRIEF_MERGED_TOO_MANY_ITEMS = "debrief.merged_too_many_items"
    DEBRIEF_MERGED_FINDING_TOO_MANY_REFS = "debrief.merged_finding_too_many_refs"
    DEBRIEF_MERGED_TOO_MANY_FINDINGS = "debrief.merged_too_many_findings"
    DEBRIEF_REPORT_SOURCE_METADATA_ABSENT = "debrief.report_source_metadata_absent"
    DEBRIEF_CANONICAL_INVALID = "debrief.canonical_invalid"
    DEBRIEF_SOURCE_COUNT_MISMATCH = "debrief.source_count_mismatch"
    DEBRIEF_BRIEF_FINDINGS_INCOMPLETE = "debrief.brief_findings_incomplete"
    DEBRIEF_EVIDENCE_SOURCE_ID_INVALID = "debrief.evidence_source_id_invalid"
    DEBRIEF_EVIDENCE_METADATA_DIFFERS = "debrief.evidence_metadata_differs"
    DEBRIEF_CITED_METADATA_ABSENT = "debrief.cited_metadata_absent"
    DEBRIEF_SOURCE_HASH_CHANGED = "debrief.source_hash_changed"
    PERSONA_IDENTITY_MISMATCH = "persona.identity_mismatch"
    PERCEPTION_PROVIDER_UNSUPPORTED = "perception.provider_unsupported"
    API_UNAUTHORIZED = "api.unauthorized"
    API_FORBIDDEN = "api.forbidden"
    API_NOT_FOUND = "api.not_found"
    API_RATE_LIMITED = "api.rate_limited"
    API_UNREACHABLE = "api.unreachable"
    API_TOKEN_MISSING = "api.token_missing"
    API_JOB_FAILED = "api.job_failed"
    WORKER_JOB_KEYS_MISSING = "worker.job_keys_missing"
    API_JOB_TIMEOUT = "api.job_timeout"
    API_STREAM_CLOSED = "api.stream_closed"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    template: str
    exit_code: ExitCode = ExitCode.FAILURE


CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INPUT_INVALID: ErrorSpec("invalid input: {reason}", ExitCode.INPUT_REJECTED),
    ErrorCode.SESSION_ID_INVALID: ErrorSpec(
        "invalid {field}: {value!r}; use 1-128 letters, numbers, '_' or '-', "
        "starting with a letter or number",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.DATABASE_URL_MISSING: ErrorSpec(
        f"Set {ConfigKey.DATABASE_URL} in .env before using this command.", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DATABASE_URL_SCHEME: ErrorSpec(
        f"{ConfigKey.DATABASE_URL} must start with postgresql:// or postgresql+psycopg://.",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.DATABASE_UNREACHABLE: ErrorSpec(
        f"PostgreSQL is unreachable; check {ConfigKey.DATABASE_URL} "
        "and that the postgres service is up.",
        ExitCode.INFRA_FAILURE,
    ),
    ErrorCode.DATABASE_SCHEMA_OUTDATED: ErrorSpec(
        "database schema is at {found}, expected {expected}; run `" + Command.DB_UPGRADE + "`"
    ),
    ErrorCode.DATABASE_SCHEMA_DRIFT: ErrorSpec(
        "voiceprints drifted from the migrated schema: {differences}"
    ),
    ErrorCode.DATABASE_SCHEMA_UNVERSIONED: ErrorSpec(
        "voiceprints exists without migration history; check that it matches the baseline "
        "and run `" + Command.DB_STAMP + "` to adopt it"
    ),
    ErrorCode.DATABASE_STAMP_REFUSED: ErrorSpec(
        "voiceprints does not match the baseline migration: {differences}; "
        "refusing to record it as applied"
    ),
    ErrorCode.CONFIG_SECRET_MISSING: ErrorSpec(
        "Set {name} in .env before using this command.", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED: ErrorSpec(
        "perception provider openai_compatible requires " + ConfigKey.PERCEPTION_BASE_URL,
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.CONFIG_EMBEDDING_DIM_MISMATCH: ErrorSpec(
        f"{ConfigKey.MEMORY_EMBEDDER_DIM} must equal EMBEDDING_DIM loaded by Graphiti; "
        "export EMBEDDING_DIM before startup",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.CONFIG_UNKNOWN_KEY: ErrorSpec(
        "Unknown configuration key: {name}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CONFIG_INVALID: ErrorSpec("invalid configuration: {reason}", ExitCode.INPUT_REJECTED),
    ErrorCode.VOICEPRINT_MODEL_PATH_MISSING: ErrorSpec(
        f"Set {ConfigKey.VOICEPRINT_MODEL_PATH} in .env before enabling voiceprints.",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_CALIBRATION_INVALID: ErrorSpec(
        f"Set {ConfigKey.VOICEPRINT_CALIBRATION_PATH} in .env "
        "to a valid calibration artifact before enabling voiceprints.",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_CLIP_UNREADABLE: ErrorSpec(
        "clip cannot be read: {label}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED: ErrorSpec(
        "unsupported audio format in {label}: need 16 kHz mono 16-bit WAV",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE: ErrorSpec(
        "gated enrollment needs --challenge-id, --challenge, --transcript, and --owner-name",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_NOT_ENROLLED: ErrorSpec(
        'no voiceprint enrolled for "{name}"', ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS: ErrorSpec(
        "enroll needs at least {minimum} clips, got {count}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VOICEPRINT_CLIP_TOO_SHORT: ErrorSpec(
        "clip too short: {label} holds {seconds:.2f}s of audio, need at least {minimum:.1f}s",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_CLIP_SILENT: ErrorSpec(
        "clip is silent: {label} rms {level:.4f} below {threshold}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VOICEPRINT_MODEL_DIGEST_MISMATCH: ErrorSpec(
        "sha256 mismatch for model file {name}: expected {expected} got {digest}",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.VOICEPRINT_EXTRA_MISSING: ErrorSpec(
        'voiceprint support needs the voiceprint extra: pip install "fakeyou[voiceprint]"'
    ),
    ErrorCode.CHAT_THREAD_INVALID: ErrorSpec(
        "--thread must be 1-128 characters using letters, numbers, '_' or '-'",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.CHAT_FAILED: ErrorSpec("chat failed: {reason}"),
    ErrorCode.MEET_CHAT_TOO_LONG: ErrorSpec(
        "meet chat message must be {limit} characters or fewer", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEET_CHAT_RATE_LIMITED: ErrorSpec(
        "meet chat allows {limit} messages per minute", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEET_CHAT_EVIDENCE_REJECTED: ErrorSpec(
        "meet chat message must not contain memory evidence text", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_LINK_INVALID: ErrorSpec(
        "link must be an https://meet.google.com/ meeting URL", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE: ErrorSpec(
        "--lobby-timeout must be positive", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_DURATION_NOT_POSITIVE: ErrorSpec(
        "--duration must be positive", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO: ErrorSpec(
        "--speak requires --audio and nonempty text", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO: ErrorSpec(
        "--converse requires --audio", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE: ErrorSpec(
        "--memory requires --converse", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE: ErrorSpec(
        "--persona requires --converse", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA: ErrorSpec(
        "--memory requires --persona", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE: ErrorSpec(
        "--brief requires --converse", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA: ErrorSpec(
        "--brief requires --persona", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID: ErrorSpec(
        "--brief requires an explicit --meeting-id", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE: ErrorSpec(
        "--wake-phrase requires --converse", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE: ErrorSpec(
        "--barge-in requires --converse", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH: ErrorSpec(
        "persona language must match speech.language", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST: ErrorSpec(
        "--memory requires recall_meeting_context or ingest_transcripts in the persona",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY: ErrorSpec(
        "--memory is not allowed with a brief_only meeting brief", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_AUDIO_REQUIRES_WORKER: ErrorSpec(
        "--audio requires the Linux audio worker; use " + Command.COMPOSE_WORKER_SESSIONS,
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_BRIEF_UNREADABLE: ErrorSpec(
        "--brief cannot be read: {reason}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH: ErrorSpec(
        "meeting brief owner_id, twin_id, and meeting_id must match the session",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_BRIEF_TOO_LARGE: ErrorSpec(
        "meeting brief must be at most {limit} bytes", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_BRIEF_SCHEMA_INVALID: ErrorSpec(
        "--brief is invalid: {count} validation error(s): {kinds}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_STARTED_WITH_BRIEF: ErrorSpec(
        "meeting {meeting_id} was started with --brief; rejoin with the same --brief and --persona",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_CONTEXT_DIFFERS: ErrorSpec(
        "meeting context differs from --brief or --persona; use a new meeting ID",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_CONTEXT_UNREADABLE: ErrorSpec(
        "meeting context is unreadable or corrupt at {path}; use a new meeting ID",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_CONTEXT_TOO_LARGE: ErrorSpec(
        "meeting context must be at most {limit} bytes", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_CONTEXT_NOT_WRITTEN: ErrorSpec("meeting context not written: {reason}"),
    ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT: ErrorSpec(
        "meeting {meeting_id} already has conversation data without a meeting context; "
        "use a new meeting ID for --brief",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_UNFINISHED_SESSION: ErrorSpec(
        "meeting {meeting_id} has an unfinished session; rejoin with --resume",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.MEETING_ALREADY_ENDED: ErrorSpec(
        "meeting {meeting_id} already ended; use a new meeting ID", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE: ErrorSpec(
        "conversation checkpoint cannot be checked: {reason}"
    ),
    ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE: ErrorSpec(
        "lifecycle checkpoint cannot be checked: {reason}"
    ),
    ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH: ErrorSpec(
        "checkpoint identity does not match the requested session", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEETING_CANNOT_RESUME_PHASE: ErrorSpec(
        "cannot resume meeting {meeting_id} in phase {phase}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.BROWSER_PROFILE_IN_USE: ErrorSpec("browser profile is in use: {reason}"),
    ErrorCode.RECORD_LOCKED: ErrorSpec("meeting record is locked: {path}"),
    ErrorCode.RECORD_LOCK_CORRUPT: ErrorSpec(
        "meeting record lock is corrupt: {path}; inspect it first"
    ),
    ErrorCode.LEASE_LOCKED: ErrorSpec("lease is locked: {path}"),
    ErrorCode.LEASE_LOST: ErrorSpec(
        "lease on {path} was lost while held; the work it guarded may be incomplete"
    ),
    ErrorCode.INGESTION_STATUS_CORRUPT: ErrorSpec(
        "ingestion status {path} is corrupt: {reason}; inspect it before retrying"
    ),
    ErrorCode.ACTION_LEDGER_LOCKED: ErrorSpec("action ledger is locked: {path}"),
    ErrorCode.MEMORY_PERSONA_UNREADABLE: ErrorSpec(
        "--persona cannot be read: {reason}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_SOURCE_AT_NAIVE: ErrorSpec(
        "--source-at must carry a timezone", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_SOURCE_AT_FUTURE: ErrorSpec(
        "--source-at must not be in the future", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_NOTE_TEXT_BLANK: ErrorSpec(
        "note text must not be blank", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_SOURCE_AT_INVALID: ErrorSpec(
        "must be an ISO 8601 datetime with a timezone: {value}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_EPISODE_NOT_UUID: ErrorSpec(
        "--episode must be a UUID", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_RECORD_MISSING: ErrorSpec(
        "no meeting record for {meeting_id} at {path}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH: ErrorSpec(
        "persona group does not match the ingestion status group", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_FORGET_TARGET_REQUIRED: ErrorSpec(
        "memory-forget requires exactly one of --meeting-id or --episode", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING: ErrorSpec(
        "--keep-record only applies to --meeting-id", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.MEMORY_FORGET_INCOMPLETE: ErrorSpec("episode {uuid} survived removal during forget"),
    ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE: ErrorSpec(
        "meeting file {path} could not be removed during forget: {reason}"
    ),
    ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE: ErrorSpec("correction target is no longer active"),
    ErrorCode.NOTE_NOT_PENDING: ErrorSpec("owner note is no longer pending"),
    ErrorCode.NOTE_CORRECTION_CONCURRENT: ErrorSpec(
        "concurrent correction already replaced this note"
    ),
    ErrorCode.NOTE_ID_IN_USE: ErrorSpec("note id already in use"),
    ErrorCode.NOTE_ALREADY_SUPERSEDED: ErrorSpec("owner note was already superseded"),
    ErrorCode.NOTE_PAYLOAD_MISMATCH: ErrorSpec("note id already in use with different payload"),
    ErrorCode.DEBRIEF_OWNERSHIP_UNVERIFIABLE: ErrorSpec(
        "record ownership cannot be verified without context or ingestion status"
    ),
    ErrorCode.DEBRIEF_SCOPE_MISMATCH: ErrorSpec(
        "meeting record scope does not match the operator persona"
    ),
    ErrorCode.DEBRIEF_RECORD_MISSING: ErrorSpec(
        "no meeting record is available for debrief", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DEBRIEF_PUBLICATION_RECOVERY: ErrorSpec(
        "canonical debrief requires publication recovery"
    ),
    ErrorCode.DEBRIEF_CACHED_SCOPE_MISMATCH: ErrorSpec(
        "cached debrief scope does not match the operator persona"
    ),
    ErrorCode.DEBRIEF_SOURCES_CHANGED: ErrorSpec(
        "meeting sources changed during debrief generation"
    ),
    ErrorCode.DEBRIEF_FAILED: ErrorSpec("debrief failed: {reason}"),
    ErrorCode.DEBRIEF_LABELS_INVALID: ErrorSpec("invalid debrief labels"),
    ErrorCode.CALIBRATE_TRIAL_NOT_OBJECT: ErrorSpec(
        "trial entry is not an object in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_TRIAL_SPEAKER_MISSING: ErrorSpec(
        "trial entry lacks speaker_id in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_TRIAL_LABEL_INVALID: ErrorSpec(
        "trial entry has bad label in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_TRIAL_SCORES_INVALID: ErrorSpec(
        "trial entry has bad scores in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_TRIAL_FLAGS_INVALID: ErrorSpec(
        "trial entry has bad flags in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_JOURNAL_UNREADABLE: ErrorSpec(
        "journal cannot be read: {path}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_JOURNAL_LINE_INVALID: ErrorSpec(
        "journal line is not JSON in {origin}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_MODEL_DIGEST_MISSING: ErrorSpec(
        "model version holds no sha256 digest: {model_version}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.CALIBRATE_DEV_TRIALS_INCOMPLETE: ErrorSpec(
        "dev journals need both target and nontarget trials", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_COHORT_NO_SPREAD: ErrorSpec(
        "dev cohort has no spread", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_RELIABILITY_EXCEEDS_BOUND: ErrorSpec(
        "dev reliability exceeds bound", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_NO_OPERATING_POINT: ErrorSpec(
        "dev scores show no operating point", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_THETA_OUTSIDE_VALLEY: ErrorSpec(
        "theta sits outside a margin valley", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_MARGINS_NO_VALLEY: ErrorSpec(
        "dev margins show no valley", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_MARGIN_FLOOR_OUTSIDE_VALLEY: ErrorSpec(
        "margin floor sits outside a margin valley", ExitCode.CALIBRATION_REJECTED
    ),
    ErrorCode.CALIBRATE_ARTIFACT_NOT_WRITTEN: ErrorSpec("artifact cannot be written: {path}"),
    ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE: ErrorSpec(
        "--days must be positive", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.AUDIO_ROOT_MISSING: ErrorSpec(
        "audio root is missing: {path}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.AUDIO_PURGE_FAILED: ErrorSpec("audio purge failed: {reason}"),
    ErrorCode.VISUAL_RETENTION_DAYS_NOT_POSITIVE: ErrorSpec(
        "--days must be positive", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VISUAL_ROOT_MISSING: ErrorSpec(
        "visual root is missing: {path}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.VISUAL_PURGE_FAILED: ErrorSpec("visual purge failed: {reason}"),
    ErrorCode.DIAGNOSTICS_JOURNAL_MISSING: ErrorSpec(
        "no journal for meeting {meeting_id} at {path}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DIAGNOSTICS_REPORT_FAILED: ErrorSpec("session report failed: {reason}"),
    ErrorCode.DIARIZE_INPUT_UNREADABLE: ErrorSpec(
        "diarization input cannot be read: {reason}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DIARIZE_CORRECTION_MISSING: ErrorSpec(
        "no diarization correction for meeting: {meeting_id}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DIARIZE_TRANSCRIPTION_FAILED: ErrorSpec("transcription request failed: {reason}"),
    ErrorCode.DIARIZE_TRANSCRIPTION_RESPONSE_INVALID: ErrorSpec(
        "transcription response parse failed"
    ),
    ErrorCode.DIARIZE_SIDECAR_INVALID: ErrorSpec("diarization sidecar is invalid"),
    ErrorCode.DIARIZE_SCHEMA_UNSUPPORTED: ErrorSpec("diarization sidecar schema is unsupported"),
    ErrorCode.DIARIZE_TURNS_INVALID: ErrorSpec("diarization sidecar turns are invalid"),
    ErrorCode.DIARIZE_STALE: ErrorSpec(
        "diarization sidecar is stale: record changed since correction",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.DOCKER_UNAVAILABLE: ErrorSpec(
        "docker is not available: {reason}", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.OPERATION_INTERRUPTED: ErrorSpec("operation interrupted"),
    ErrorCode.INTERNAL_FAILURE: ErrorSpec("internal failure", ExitCode.FAILURE),
    ErrorCode.PROVIDER_UNAVAILABLE: ErrorSpec(
        "provider unavailable: {reason}", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.MEMORY_UNAVAILABLE: ErrorSpec("memory unavailable: {reason}", ExitCode.INFRA_FAILURE),
    ErrorCode.DIARIZE_SLOT_METADATA_INVALID: ErrorSpec(
        "slot alignment metadata is invalid: {path}"
    ),
    ErrorCode.DIARIZE_SLOT_MISALIGNED: ErrorSpec(
        "slot audio is misaligned for diarization: {path}"
    ),
    ErrorCode.DIARIZE_SLOT_NOT_AT_ZERO: ErrorSpec(
        "slot audio does not start at zero for diarization: {path}"
    ),
    ErrorCode.DIARIZE_SPEAKER_LABEL_INVALID: ErrorSpec(
        "speaker label must be a single token without whitespace"
    ),
    ErrorCode.INGESTION_EPISODE_NAME_INVALID: ErrorSpec(
        "invalid episode name for meeting {meeting_id}; expected {meeting_id}:w<index>; run "
        + Command.MEMORY_FORGET
        + " --meeting-id with --keep-record before ingesting again"
    ),
    ErrorCode.ACTION_LEDGER_UNREADABLE: ErrorSpec("unreadable action ledger: {path}"),
    ErrorCode.ACTION_LEDGER_MEETING_MISMATCH: ErrorSpec("action ledger meeting mismatch: {path}"),
    ErrorCode.ACTION_LEDGER_INTENT_UNKNOWN: ErrorSpec("unknown action intent: {action_id}"),
    ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE: ErrorSpec(
        "Chrome DevTools endpoint is unavailable: {reason}"
    ),
    ErrorCode.BROWSER_MCP_MISSING: ErrorSpec(
        "Playwright MCP is missing. Run " + Command.NPM_CI + " from the project directory."
    ),
    ErrorCode.BROWSER_NODE_MISSING: ErrorSpec(
        "node is not available: {reason}", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.BROWSER_TOOLS_FAILED: ErrorSpec("browser tools failed: {reason}"),
    ErrorCode.DATABASE_SCHEMA_EMPTY: ErrorSpec(
        "database has no migrations applied, expected {expected}; run `" + Command.DB_UPGRADE + "`"
    ),
    ErrorCode.DATABASE_SCHEMA_HEADS_INVALID: ErrorSpec(
        "expected exactly one migration head, found {count}"
    ),
    ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE: ErrorSpec(
        "a meeting brief requires a persona profile", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_ID_NOT_UUID: ErrorSpec("must be a UUID: {value}", ExitCode.INPUT_REJECTED),
    ErrorCode.NOTE_GROUP_ID_INVALID: ErrorSpec(
        "group_id must be 1-128 characters using letters, numbers, '_' or '-'",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.NOTE_TIME_NOT_AWARE: ErrorSpec("{name} must carry timezone", ExitCode.INPUT_REJECTED),
    ErrorCode.NOTE_STORED_FIELD_INVALID: ErrorSpec("stored note holds invalid {key}"),
    ErrorCode.NOTE_STORED_STATUS_INVALID: ErrorSpec("stored note holds invalid status"),
    ErrorCode.NOTE_STORED_IDENTITY_INVALID: ErrorSpec("stored note holds invalid identity"),
    ErrorCode.NOTE_RECORD_MISSING: ErrorSpec("published note record is missing"),
    ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN: ErrorSpec(
        "unknown correction target", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE: ErrorSpec(
        "correction target must be an owner note", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_CORRECTION_NOT_NEWER: ErrorSpec(
        "correction source time must be newer than its target", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_TARGET_SNAPSHOT_MISSING: ErrorSpec("correction target snapshot is missing"),
    ErrorCode.NOTE_EPISODE_MISSING: ErrorSpec("published note episode is missing"),
    ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY: ErrorSpec(
        "episode is not in this persona's memory", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_NO_READABLE_FACTS: ErrorSpec("published note has no readable facts"),
    ErrorCode.NOTE_NO_USABLE_FACTS: ErrorSpec("owner note extraction produced no usable facts"),
    ErrorCode.NOTE_BODY_BLANK: ErrorSpec("owner note must not be blank", ExitCode.INPUT_REJECTED),
    ErrorCode.NOTE_SOURCE_AT_FUTURE: ErrorSpec(
        "source_at must not be in the future", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_EPISODE_UUID_BLANK: ErrorSpec(
        "episode uuid must not be blank", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.NOTE_HAS_PENDING_CORRECTION: ErrorSpec(
        "owner note has a pending correction; forget the pending note first",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.NOTE_IN_CORRECTION_CHAIN: ErrorSpec(
        "owner note is part of a correction chain; "
        "chain deletion needs a dedicated retention milestone",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.DEBRIEF_SOURCE_TIME_NOT_AWARE: ErrorSpec("source timestamp must be timezone aware"),
    ErrorCode.DEBRIEF_RECORD_UNREADABLE: ErrorSpec("unable to read meeting record"),
    ErrorCode.DEBRIEF_RECORD_INVALID_LINE: ErrorSpec("invalid meeting record at line {line}"),
    ErrorCode.DEBRIEF_TRANSCRIPT_ID_BLANK: ErrorSpec("transcript source id must not be blank"),
    ErrorCode.DEBRIEF_TRANSCRIPT_CONTRADICTORY: ErrorSpec(
        "contradictory duplicate transcript source"
    ),
    ErrorCode.DEBRIEF_VISUAL_ID_BLANK: ErrorSpec("visual source id must not be blank"),
    ErrorCode.DEBRIEF_VISUAL_CONTRADICTORY: ErrorSpec("contradictory duplicate visual source"),
    ErrorCode.DEBRIEF_NO_SPEECH_SOURCES: ErrorSpec(
        "meeting record contains no usable speech sources"
    ),
    ErrorCode.DEBRIEF_CONTEXT_UNREADABLE: ErrorSpec(
        "unable to read meeting context", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE: ErrorSpec(
        "meeting context must be at most {limit} bytes", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DEBRIEF_CONTEXT_INVALID: ErrorSpec(
        "invalid meeting context", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.DEBRIEF_CHUNK_LIMITS_INVALID: ErrorSpec("invalid debrief chunk limits"),
    ErrorCode.DEBRIEF_NO_SOURCES: ErrorSpec("no debrief sources to chunk"),
    ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK: ErrorSpec(
        "speech source exceeds the chunk character limit"
    ),
    ErrorCode.DEBRIEF_TOO_MANY_CHUNKS: ErrorSpec("meeting record exceeds the 20 chunk limit"),
    ErrorCode.DEBRIEF_EVIDENCE_MISSING: ErrorSpec("missing evidence"),
    ErrorCode.DEBRIEF_EVIDENCE_INVALID: ErrorSpec("invalid evidence reference"),
    ErrorCode.DEBRIEF_DRAFT_INVALID: ErrorSpec("invalid debrief draft"),
    ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES: ErrorSpec("item claim is not in its evidence quotes"),
    ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF: ErrorSpec(
        "finding does not belong to the meeting brief"
    ),
    ErrorCode.DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT: ErrorSpec(
        "addressed finding needs participant evidence"
    ),
    ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT: ErrorSpec(
        "native debrief structured response is absent"
    ),
    ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID: ErrorSpec("invalid native debrief schema"),
    ErrorCode.DEBRIEF_MERGED_TOO_MANY_ITEMS: ErrorSpec("merged debrief exceeds 100 items"),
    ErrorCode.DEBRIEF_MERGED_FINDING_TOO_MANY_REFS: ErrorSpec(
        "merged finding exceeds 10 evidence references"
    ),
    ErrorCode.DEBRIEF_MERGED_TOO_MANY_FINDINGS: ErrorSpec("merged debrief exceeds 40 findings"),
    ErrorCode.DEBRIEF_REPORT_SOURCE_METADATA_ABSENT: ErrorSpec(
        "report evidence source metadata is absent"
    ),
    ErrorCode.DEBRIEF_CANONICAL_INVALID: ErrorSpec("canonical debrief is missing or invalid"),
    ErrorCode.DEBRIEF_SOURCE_COUNT_MISMATCH: ErrorSpec(
        "debrief source count differs from the snapshot"
    ),
    ErrorCode.DEBRIEF_BRIEF_FINDINGS_INCOMPLETE: ErrorSpec("debrief brief findings are incomplete"),
    ErrorCode.DEBRIEF_EVIDENCE_SOURCE_ID_INVALID: ErrorSpec(
        "debrief evidence source ID is invalid"
    ),
    ErrorCode.DEBRIEF_EVIDENCE_METADATA_DIFFERS: ErrorSpec(
        "debrief evidence source metadata differs"
    ),
    ErrorCode.DEBRIEF_CITED_METADATA_ABSENT: ErrorSpec(
        "debrief cited evidence source metadata is absent"
    ),
    ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED: ErrorSpec("debrief source or context hash changed"),
    ErrorCode.PERSONA_IDENTITY_MISMATCH: ErrorSpec(
        "persona owner_id and twin_id must match the session identity", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.PERCEPTION_PROVIDER_UNSUPPORTED: ErrorSpec(
        "unsupported perception provider: {provider}", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.API_UNAUTHORIZED: ErrorSpec(
        "missing or invalid API credentials", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.API_FORBIDDEN: ErrorSpec(
        "API credentials cannot access this operation", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.API_NOT_FOUND: ErrorSpec("resource not found", ExitCode.INPUT_REJECTED),
    ErrorCode.API_RATE_LIMITED: ErrorSpec(
        "too many requests; retry after the rate limit window", ExitCode.INPUT_REJECTED
    ),
    ErrorCode.API_UNREACHABLE: ErrorSpec(
        "api unreachable at {url}: {reason}", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.API_JOB_FAILED: ErrorSpec("{kind} job {job_id} did not succeed", ExitCode.FAILURE),
    ErrorCode.WORKER_JOB_KEYS_MISSING: ErrorSpec(
        "{scope} was started with model keys in its request; resubmit the job with the same "
        "X-Fakeyou-Key-* headers",
        ExitCode.INPUT_REJECTED,
    ),
    ErrorCode.API_JOB_TIMEOUT: ErrorSpec(
        "{kind} job {job_id} did not finish within {seconds} seconds", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.API_STREAM_CLOSED: ErrorSpec(
        "event stream for {meeting_id} closed before the session ended", ExitCode.INFRA_FAILURE
    ),
    ErrorCode.API_TOKEN_MISSING: ErrorSpec(
        "{variable} is not set; the command needs that API credential", ExitCode.INPUT_REJECTED
    ),
}


def render_error(code: ErrorCode, **fields: object) -> str:
    return CATALOG[code].template.format(**fields)


class FakeYouError(Exception):
    def __init__(self, code: ErrorCode, **fields: object) -> None:
        self.code = code
        self.exit_code = CATALOG[code].exit_code
        super().__init__(render_error(code, **fields))
