# Katalog error dan exit code

Halaman ini dihasilkan dari katalog di `src/fakeyou/catalog/errors.py` oleh `fakeyou.catalog.error_catalog`. Jangan mengedit halaman ini secara manual. Regenerasi dengan `uv run python -m fakeyou.catalog.error_catalog > docs/reference/error-catalog.md`. `tests/test_error_catalog.py` gagal jika halaman ini berbeda dari hasil generator, jadi perubahan katalog harus ikut diregenerasi.

## Aturan

- Setiap error yang sampai ke pengguna berupa `FakeYouError` dengan kode dari `ErrorCode`. Pesan dirender dari template di katalog, dan exit code diambil dari spesifikasi kode yang sama.
- Kode berbentuk `domain.nama` dan dianggap bagian dari kontrak. Mengubah kode atau template adalah breaking change dan harus dicatat. Nama anggota enum mengikuti nilainya, misalnya `config.unknown_key` menjadi `CONFIG_UNKNOWN_KEY`.
- Pesan tidak menampilkan nilai secret, isi brief, atau data biometrik. Placeholder `{...}` hanya berisi identitas sesi, path, nama field, atau alasan teknis yang sudah disanitasi.
- `ValueError` dan `TypeError` yang tersisa adalah pelanggaran kontrak pemanggilan internal, atau validasi yang diwajibkan framework (pydantic dan argparse). Pelanggaran internal tidak dipetakan ke kode katalog dan tampil sebagai traceback. `ValidationError` dari konfigurasi dipetakan ke `CONFIG_INVALID`, dan `ValidationError` dari argumen perintah dipetakan ke `INPUT_INVALID`.

## Exit code

| Nilai | Nama | Arti |
| --- | --- | --- |
| 0 | SUCCESS | Berhasil. |
| 1 | FAILURE | Gagal umum. Dipakai jika kode tidak menyebut exit lain. |
| 2 | INPUT_REJECTED | Input pengguna ditolak sebelum operasi dimulai: flag, argumen, atau file. |
| 3 | INFRA_FAILURE | Infrastruktur atau provider tidak tersedia. |
| 4 | EVAL_GATE_FAILED | Gate evaluasi tidak lulus. |
| 5 | CALIBRATION_REJECTED | Artefak kalibrasi ditolak oleh gate dev. |

## Alasan berhenti meeting

Setiap alasan terminal meeting dipetakan ke exit code berikut. `join` memakai tabel ini untuk menentukan exit code hasil akhirnya.

| Alasan terminal | Exit |
| --- | --- |
| `infra_failure` | INFRA_FAILURE |
| `browser_failure` | FAILURE |
| `audio_failure` | FAILURE |
| `conversation_failure` | FAILURE |
| `stopped_by_owner` | SUCCESS |
| `left_by_owner` | SUCCESS |
| `denied_by_host` | SUCCESS |
| `removed_by_host` | SUCCESS |
| `ended_by_host` | SUCCESS |
| `blocked_by_platform` | SUCCESS |
| `lobby_timeout_reached` | SUCCESS |
| `duration_limit_reached` | SUCCESS |
| `alone_limit_reached` | SUCCESS |

## Daftar kode

Kolom Exit menunjukkan exit code saat error ini menghentikan CLI.

### input

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `input.invalid` | `INPUT_INVALID` | INPUT_REJECTED | `invalid input: {reason}` |

### database

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `database.url_missing` | `DATABASE_URL_MISSING` | INPUT_REJECTED | `Set FAKEYOU_DATABASE__URL in .env before using this command.` |
| `database.url_scheme` | `DATABASE_URL_SCHEME` | INPUT_REJECTED | `FAKEYOU_DATABASE__URL must start with postgresql:// or postgresql+psycopg://.` |
| `database.unreachable` | `DATABASE_UNREACHABLE` | INFRA_FAILURE | `PostgreSQL is unreachable; check FAKEYOU_DATABASE__URL and that the postgres service is up.` |
| `database.schema_outdated` | `DATABASE_SCHEMA_OUTDATED` | FAILURE | `` database schema is at {found}, expected {expected}; run `fakeyou db upgrade` `` |
| `database.schema_drift` | `DATABASE_SCHEMA_DRIFT` | FAILURE | `voiceprints drifted from the migrated schema: {differences}` |
| `database.schema_unversioned` | `DATABASE_SCHEMA_UNVERSIONED` | FAILURE | `` voiceprints exists without migration history; check that it matches the baseline and run `fakeyou db stamp` to adopt it `` |
| `database.stamp_refused` | `DATABASE_STAMP_REFUSED` | FAILURE | `voiceprints does not match the baseline migration: {differences}; refusing to record it as applied` |
| `database.schema_empty` | `DATABASE_SCHEMA_EMPTY` | FAILURE | `` database has no migrations applied, expected {expected}; run `fakeyou db upgrade` `` |

### config

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `config.secret_missing` | `CONFIG_SECRET_MISSING` | INPUT_REJECTED | `Set {name} in .env before using this command.` |
| `config.visual_base_url_required` | `CONFIG_VISUAL_BASE_URL_REQUIRED` | INPUT_REJECTED | `visual provider openai_compatible requires FAKEYOU_VISUAL__BASE_URL` |
| `config.worker_database_url_missing` | `CONFIG_WORKER_DATABASE_URL_MISSING` | INPUT_REJECTED | `Set FAKEYOU_COMPOSE_WORKER_DATABASE_URL in .env; worker containers keep checkpoints in Postgres.` |
| `config.embedding_dim_mismatch` | `CONFIG_EMBEDDING_DIM_MISMATCH` | INPUT_REJECTED | `FAKEYOU_MEMORY__EMBEDDER__DIM must equal EMBEDDING_DIM loaded by Graphiti; export EMBEDDING_DIM before startup` |
| `config.unknown_key` | `CONFIG_UNKNOWN_KEY` | INPUT_REJECTED | `Unknown configuration key: {name}` |
| `config.invalid` | `CONFIG_INVALID` | INPUT_REJECTED | `invalid configuration: {reason}` |

### voiceprint

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `voiceprint.model_path_missing` | `VOICEPRINT_MODEL_PATH_MISSING` | INPUT_REJECTED | `Set FAKEYOU_IDENTITY__VOICEPRINT_MODEL_PATH in .env before enabling voiceprints.` |
| `voiceprint.calibration_invalid` | `VOICEPRINT_CALIBRATION_INVALID` | INPUT_REJECTED | `Set FAKEYOU_IDENTITY__VOICEPRINT_CALIBRATION_PATH in .env to a valid calibration artifact before enabling voiceprints.` |
| `voiceprint.clip_unreadable` | `VOICEPRINT_CLIP_UNREADABLE` | INPUT_REJECTED | `clip cannot be read: {label}` |
| `voiceprint.audio_format_unsupported` | `VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED` | INPUT_REJECTED | `unsupported audio format in {label}: need 16 kHz mono 16-bit WAV` |
| `voiceprint.enrollment_gate_incomplete` | `VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE` | INPUT_REJECTED | `gated enrollment needs --challenge-id, --challenge, --transcript, and --owner-name` |
| `voiceprint.not_enrolled` | `VOICEPRINT_NOT_ENROLLED` | INPUT_REJECTED | `no voiceprint enrolled for "{name}"` |
| `voiceprint.enroll_too_few_clips` | `VOICEPRINT_ENROLL_TOO_FEW_CLIPS` | INPUT_REJECTED | `enroll needs at least {minimum} clips, got {count}` |
| `voiceprint.clip_too_short` | `VOICEPRINT_CLIP_TOO_SHORT` | INPUT_REJECTED | `clip too short: {label} holds {seconds:.2f}s of audio, need at least {minimum:.1f}s` |
| `voiceprint.clip_silent` | `VOICEPRINT_CLIP_SILENT` | INPUT_REJECTED | `clip is silent: {label} rms {level:.4f} below {threshold}` |
| `voiceprint.model_digest_mismatch` | `VOICEPRINT_MODEL_DIGEST_MISMATCH` | INPUT_REJECTED | `sha256 mismatch for model file {name}: expected {expected} got {digest}` |
| `voiceprint.extra_missing` | `VOICEPRINT_EXTRA_MISSING` | FAILURE | `voiceprint support needs the voiceprint extra: pip install "fakeyou[voiceprint]"` |

### chat

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `chat.thread_invalid` | `CHAT_THREAD_INVALID` | INPUT_REJECTED | `--thread must be 1-128 characters using letters, numbers, '_' or '-'` |

### meeting

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `meeting.link_invalid` | `MEETING_LINK_INVALID` | INPUT_REJECTED | `link must be an https://meet.google.com/ meeting URL` |
| `meeting.lobby_timeout_not_positive` | `MEETING_LOBBY_TIMEOUT_NOT_POSITIVE` | INPUT_REJECTED | `--lobby-timeout must be positive` |
| `meeting.duration_not_positive` | `MEETING_DURATION_NOT_POSITIVE` | INPUT_REJECTED | `--duration must be positive` |
| `meeting.speak_requires_audio` | `MEETING_SPEAK_REQUIRES_AUDIO` | INPUT_REJECTED | `--speak requires --audio and nonempty text` |
| `meeting.converse_requires_audio` | `MEETING_CONVERSE_REQUIRES_AUDIO` | INPUT_REJECTED | `--converse requires --audio` |
| `meeting.memory_requires_converse` | `MEETING_MEMORY_REQUIRES_CONVERSE` | INPUT_REJECTED | `--memory requires --converse` |
| `meeting.persona_requires_converse` | `MEETING_PERSONA_REQUIRES_CONVERSE` | INPUT_REJECTED | `--persona requires --converse` |
| `meeting.memory_requires_persona` | `MEETING_MEMORY_REQUIRES_PERSONA` | INPUT_REJECTED | `--memory requires --persona` |
| `meeting.brief_requires_converse` | `MEETING_BRIEF_REQUIRES_CONVERSE` | INPUT_REJECTED | `--brief requires --converse` |
| `meeting.brief_requires_persona` | `MEETING_BRIEF_REQUIRES_PERSONA` | INPUT_REJECTED | `--brief requires --persona` |
| `meeting.brief_requires_meeting_id` | `MEETING_BRIEF_REQUIRES_MEETING_ID` | INPUT_REJECTED | `--brief requires an explicit --meeting-id` |
| `meeting.wake_phrase_requires_converse` | `MEETING_WAKE_PHRASE_REQUIRES_CONVERSE` | INPUT_REJECTED | `--wake-phrase requires --converse` |
| `meeting.barge_in_requires_converse` | `MEETING_BARGE_IN_REQUIRES_CONVERSE` | INPUT_REJECTED | `--barge-in requires --converse` |
| `meeting.participant_policy_requires_aliases` | `MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES` | INPUT_REJECTED | `participant policy requires --wake-phrase aliases` |
| `meeting.persona_language_mismatch` | `MEETING_PERSONA_LANGUAGE_MISMATCH` | INPUT_REJECTED | `persona language must match speech.language` |
| `meeting.memory_requires_recall_or_ingest` | `MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST` | INPUT_REJECTED | `--memory requires recall_meeting_context or ingest_transcripts in the persona` |
| `meeting.memory_not_allowed_brief_only` | `MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY` | INPUT_REJECTED | `--memory is not allowed with a brief_only meeting brief` |
| `meeting.audio_requires_worker` | `MEETING_AUDIO_REQUIRES_WORKER` | INPUT_REJECTED | `--audio requires the Linux audio worker; use docker compose run meeting-worker` |
| `meeting.brief_unreadable` | `MEETING_BRIEF_UNREADABLE` | INPUT_REJECTED | `--brief cannot be read: {reason}` |
| `meeting.brief_scope_mismatch` | `MEETING_BRIEF_SCOPE_MISMATCH` | INPUT_REJECTED | `meeting brief owner_id, twin_id, and meeting_id must match the session` |
| `meeting.brief_too_large` | `MEETING_BRIEF_TOO_LARGE` | INPUT_REJECTED | `meeting brief must be at most {limit} bytes` |
| `meeting.brief_schema_invalid` | `MEETING_BRIEF_SCHEMA_INVALID` | INPUT_REJECTED | `--brief is invalid: {count} validation error(s): {kinds}` |
| `meeting.started_with_brief` | `MEETING_STARTED_WITH_BRIEF` | INPUT_REJECTED | `meeting {meeting_id} was started with --brief; rejoin with the same --brief and --persona` |
| `meeting.context_differs` | `MEETING_CONTEXT_DIFFERS` | INPUT_REJECTED | `meeting context differs from --brief or --persona; use a new meeting ID` |
| `meeting.context_unreadable` | `MEETING_CONTEXT_UNREADABLE` | INPUT_REJECTED | `meeting context is unreadable or corrupt at {path}; use a new meeting ID` |
| `meeting.context_too_large` | `MEETING_CONTEXT_TOO_LARGE` | INPUT_REJECTED | `meeting context must be at most {limit} bytes` |
| `meeting.context_not_written` | `MEETING_CONTEXT_NOT_WRITTEN` | FAILURE | `meeting context not written: {reason}` |
| `meeting.has_conversation_without_context` | `MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT` | INPUT_REJECTED | `meeting {meeting_id} already has conversation data without a meeting context; use a new meeting ID for --brief` |
| `meeting.unfinished_session` | `MEETING_UNFINISHED_SESSION` | INPUT_REJECTED | `meeting {meeting_id} has an unfinished session; rejoin with --resume` |
| `meeting.already_ended` | `MEETING_ALREADY_ENDED` | INPUT_REJECTED | `meeting {meeting_id} already ended; use a new meeting ID` |
| `meeting.conversation_checkpoint_uncheckable` | `MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE` | FAILURE | `conversation checkpoint cannot be checked: {reason}` |
| `meeting.lifecycle_checkpoint_uncheckable` | `MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE` | FAILURE | `lifecycle checkpoint cannot be checked: {reason}` |
| `meeting.checkpoint_identity_mismatch` | `MEETING_CHECKPOINT_IDENTITY_MISMATCH` | INPUT_REJECTED | `checkpoint identity does not match the requested session` |
| `meeting.cannot_resume_phase` | `MEETING_CANNOT_RESUME_PHASE` | INPUT_REJECTED | `cannot resume meeting {meeting_id} in phase {phase}` |
| `meeting.brief_requires_profile` | `MEETING_BRIEF_REQUIRES_PROFILE` | INPUT_REJECTED | `a meeting brief requires a persona profile` |

### browser

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `browser.profile_in_use` | `BROWSER_PROFILE_IN_USE` | FAILURE | `browser profile is in use: {reason}` |
| `browser.devtools_unavailable` | `BROWSER_DEVTOOLS_UNAVAILABLE` | FAILURE | `Chrome DevTools endpoint is unavailable: {reason}` |
| `browser.mcp_missing` | `BROWSER_MCP_MISSING` | FAILURE | `Playwright MCP is missing. Run npm ci from the project directory.` |

### record

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `record.locked` | `RECORD_LOCKED` | FAILURE | `meeting record is locked: {path}` |
| `record.lock_corrupt` | `RECORD_LOCK_CORRUPT` | FAILURE | `meeting record lock is corrupt: {path}; inspect it first` |

### lease

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `lease.locked` | `LEASE_LOCKED` | FAILURE | `lease is locked: {path}` |

### action_ledger

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `action_ledger.locked` | `ACTION_LEDGER_LOCKED` | FAILURE | `action ledger is locked: {path}` |
| `action_ledger.unreadable` | `ACTION_LEDGER_UNREADABLE` | FAILURE | `unreadable action ledger: {path}` |
| `action_ledger.meeting_mismatch` | `ACTION_LEDGER_MEETING_MISMATCH` | FAILURE | `action ledger meeting mismatch: {path}` |
| `action_ledger.intent_unknown` | `ACTION_LEDGER_INTENT_UNKNOWN` | FAILURE | `unknown action intent: {action_id}` |

### memory

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `memory.persona_unreadable` | `MEMORY_PERSONA_UNREADABLE` | INPUT_REJECTED | `--persona cannot be read: {reason}` |
| `memory.source_at_naive` | `MEMORY_SOURCE_AT_NAIVE` | INPUT_REJECTED | `--source-at must carry a timezone` |
| `memory.source_at_future` | `MEMORY_SOURCE_AT_FUTURE` | INPUT_REJECTED | `--source-at must not be in the future` |
| `memory.source_at_invalid` | `MEMORY_SOURCE_AT_INVALID` | INPUT_REJECTED | `must be an ISO 8601 datetime with a timezone: {value}` |
| `memory.note_text_blank` | `MEMORY_NOTE_TEXT_BLANK` | INPUT_REJECTED | `note text must not be blank` |
| `memory.episode_not_uuid` | `MEMORY_EPISODE_NOT_UUID` | INPUT_REJECTED | `--episode must be a UUID` |
| `memory.record_missing` | `MEMORY_RECORD_MISSING` | INPUT_REJECTED | `no meeting record for {meeting_id} at {path}` |
| `memory.ingestion_group_mismatch` | `MEMORY_INGESTION_GROUP_MISMATCH` | INPUT_REJECTED | `persona group does not match the ingestion status group` |
| `memory.forget_target_required` | `MEMORY_FORGET_TARGET_REQUIRED` | INPUT_REJECTED | `memory-forget requires exactly one of --meeting or --episode` |
| `memory.keep_record_requires_meeting` | `MEMORY_KEEP_RECORD_REQUIRES_MEETING` | INPUT_REJECTED | `--keep-record only applies to --meeting` |
| `memory.unavailable` | `MEMORY_UNAVAILABLE` | INFRA_FAILURE | `memory unavailable: {reason}` |

### note

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `note.correction_target_inactive` | `NOTE_CORRECTION_TARGET_INACTIVE` | FAILURE | `correction target is no longer active` |
| `note.not_pending` | `NOTE_NOT_PENDING` | FAILURE | `owner note is no longer pending` |
| `note.correction_concurrent` | `NOTE_CORRECTION_CONCURRENT` | FAILURE | `concurrent correction already replaced this note` |
| `note.id_in_use` | `NOTE_ID_IN_USE` | FAILURE | `note id already in use` |
| `note.already_superseded` | `NOTE_ALREADY_SUPERSEDED` | FAILURE | `owner note was already superseded` |
| `note.payload_mismatch` | `NOTE_PAYLOAD_MISMATCH` | FAILURE | `note id already in use with different payload` |
| `note.group_id_invalid` | `NOTE_GROUP_ID_INVALID` | INPUT_REJECTED | `group_id must be 1-128 characters using letters, numbers, '_' or '-'` |
| `note.id_not_uuid` | `NOTE_ID_NOT_UUID` | INPUT_REJECTED | `must be a UUID: {value}` |
| `note.time_not_aware` | `NOTE_TIME_NOT_AWARE` | INPUT_REJECTED | `{name} must carry timezone` |
| `note.stored_field_invalid` | `NOTE_STORED_FIELD_INVALID` | FAILURE | `stored note holds invalid {key}` |
| `note.stored_status_invalid` | `NOTE_STORED_STATUS_INVALID` | FAILURE | `stored note holds invalid status` |
| `note.stored_identity_invalid` | `NOTE_STORED_IDENTITY_INVALID` | FAILURE | `stored note holds invalid identity` |
| `note.record_missing` | `NOTE_RECORD_MISSING` | FAILURE | `published note record is missing` |
| `note.correction_target_unknown` | `NOTE_CORRECTION_TARGET_UNKNOWN` | INPUT_REJECTED | `unknown correction target` |
| `note.correction_target_not_owner_note` | `NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE` | INPUT_REJECTED | `correction target must be an owner note` |
| `note.correction_not_newer` | `NOTE_CORRECTION_NOT_NEWER` | INPUT_REJECTED | `correction source time must be newer than its target` |
| `note.target_snapshot_missing` | `NOTE_TARGET_SNAPSHOT_MISSING` | FAILURE | `correction target snapshot is missing` |
| `note.episode_missing` | `NOTE_EPISODE_MISSING` | FAILURE | `published note episode is missing` |
| `note.episode_not_in_memory` | `NOTE_EPISODE_NOT_IN_MEMORY` | INPUT_REJECTED | `episode is not in this persona's memory` |
| `note.no_readable_facts` | `NOTE_NO_READABLE_FACTS` | FAILURE | `published note has no readable facts` |
| `note.no_usable_facts` | `NOTE_NO_USABLE_FACTS` | FAILURE | `owner note extraction produced no usable facts` |
| `note.body_blank` | `NOTE_BODY_BLANK` | INPUT_REJECTED | `owner note must not be blank` |
| `note.source_at_future` | `NOTE_SOURCE_AT_FUTURE` | INPUT_REJECTED | `source_at must not be in the future` |
| `note.episode_uuid_blank` | `NOTE_EPISODE_UUID_BLANK` | INPUT_REJECTED | `episode uuid must not be blank` |
| `note.has_pending_correction` | `NOTE_HAS_PENDING_CORRECTION` | INPUT_REJECTED | `owner note has a pending correction; forget the pending note first` |
| `note.in_correction_chain` | `NOTE_IN_CORRECTION_CHAIN` | INPUT_REJECTED | `owner note is part of a correction chain; chain deletion needs a dedicated retention milestone` |

### debrief

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `debrief.ownership_unverifiable` | `DEBRIEF_OWNERSHIP_UNVERIFIABLE` | FAILURE | `record ownership cannot be verified without context or ingestion status` |
| `debrief.scope_mismatch` | `DEBRIEF_SCOPE_MISMATCH` | FAILURE | `meeting record scope does not match the operator persona` |
| `debrief.record_missing` | `DEBRIEF_RECORD_MISSING` | INPUT_REJECTED | `no meeting record is available for debrief` |
| `debrief.publication_recovery` | `DEBRIEF_PUBLICATION_RECOVERY` | FAILURE | `canonical debrief requires publication recovery` |
| `debrief.cached_scope_mismatch` | `DEBRIEF_CACHED_SCOPE_MISMATCH` | FAILURE | `cached debrief scope does not match the operator persona` |
| `debrief.sources_changed` | `DEBRIEF_SOURCES_CHANGED` | FAILURE | `meeting sources changed during debrief generation` |
| `debrief.failed` | `DEBRIEF_FAILED` | FAILURE | `debrief failed: {reason}` |
| `debrief.labels_invalid` | `DEBRIEF_LABELS_INVALID` | FAILURE | `invalid debrief labels` |
| `debrief.source_time_not_aware` | `DEBRIEF_SOURCE_TIME_NOT_AWARE` | FAILURE | `source timestamp must be timezone aware` |
| `debrief.record_unreadable` | `DEBRIEF_RECORD_UNREADABLE` | FAILURE | `unable to read meeting record` |
| `debrief.record_invalid_line` | `DEBRIEF_RECORD_INVALID_LINE` | FAILURE | `invalid meeting record at line {line}` |
| `debrief.transcript_id_blank` | `DEBRIEF_TRANSCRIPT_ID_BLANK` | FAILURE | `transcript source id must not be blank` |
| `debrief.transcript_contradictory` | `DEBRIEF_TRANSCRIPT_CONTRADICTORY` | FAILURE | `contradictory duplicate transcript source` |
| `debrief.visual_id_blank` | `DEBRIEF_VISUAL_ID_BLANK` | FAILURE | `visual source id must not be blank` |
| `debrief.visual_contradictory` | `DEBRIEF_VISUAL_CONTRADICTORY` | FAILURE | `contradictory duplicate visual source` |
| `debrief.no_speech_sources` | `DEBRIEF_NO_SPEECH_SOURCES` | FAILURE | `meeting record contains no usable speech sources` |
| `debrief.context_unreadable` | `DEBRIEF_CONTEXT_UNREADABLE` | INPUT_REJECTED | `unable to read meeting context` |
| `debrief.context_too_large` | `DEBRIEF_CONTEXT_TOO_LARGE` | INPUT_REJECTED | `meeting context must be at most {limit} bytes` |
| `debrief.context_invalid` | `DEBRIEF_CONTEXT_INVALID` | INPUT_REJECTED | `invalid meeting context` |
| `debrief.chunk_limits_invalid` | `DEBRIEF_CHUNK_LIMITS_INVALID` | FAILURE | `invalid debrief chunk limits` |
| `debrief.no_sources` | `DEBRIEF_NO_SOURCES` | FAILURE | `no debrief sources to chunk` |
| `debrief.source_exceeds_chunk` | `DEBRIEF_SOURCE_EXCEEDS_CHUNK` | FAILURE | `speech source exceeds the chunk character limit` |
| `debrief.too_many_chunks` | `DEBRIEF_TOO_MANY_CHUNKS` | FAILURE | `meeting record exceeds the 20 chunk limit` |
| `debrief.evidence_missing` | `DEBRIEF_EVIDENCE_MISSING` | FAILURE | `missing evidence` |
| `debrief.evidence_invalid` | `DEBRIEF_EVIDENCE_INVALID` | FAILURE | `invalid evidence reference` |
| `debrief.draft_invalid` | `DEBRIEF_DRAFT_INVALID` | FAILURE | `invalid debrief draft` |
| `debrief.claim_not_in_quotes` | `DEBRIEF_CLAIM_NOT_IN_QUOTES` | FAILURE | `item claim is not in its evidence quotes` |
| `debrief.finding_not_in_brief` | `DEBRIEF_FINDING_NOT_IN_BRIEF` | FAILURE | `finding does not belong to the meeting brief` |
| `debrief.addressed_needs_participant` | `DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT` | FAILURE | `addressed finding needs participant evidence` |
| `debrief.native_response_absent` | `DEBRIEF_NATIVE_RESPONSE_ABSENT` | FAILURE | `native debrief structured response is absent` |
| `debrief.native_schema_invalid` | `DEBRIEF_NATIVE_SCHEMA_INVALID` | FAILURE | `invalid native debrief schema` |
| `debrief.merged_too_many_items` | `DEBRIEF_MERGED_TOO_MANY_ITEMS` | FAILURE | `merged debrief exceeds 100 items` |
| `debrief.merged_finding_too_many_refs` | `DEBRIEF_MERGED_FINDING_TOO_MANY_REFS` | FAILURE | `merged finding exceeds 10 evidence references` |
| `debrief.merged_too_many_findings` | `DEBRIEF_MERGED_TOO_MANY_FINDINGS` | FAILURE | `merged debrief exceeds 40 findings` |
| `debrief.report_source_metadata_absent` | `DEBRIEF_REPORT_SOURCE_METADATA_ABSENT` | FAILURE | `report evidence source metadata is absent` |
| `debrief.canonical_invalid` | `DEBRIEF_CANONICAL_INVALID` | FAILURE | `canonical debrief is missing or invalid` |
| `debrief.source_count_mismatch` | `DEBRIEF_SOURCE_COUNT_MISMATCH` | FAILURE | `debrief source count differs from the snapshot` |
| `debrief.brief_findings_incomplete` | `DEBRIEF_BRIEF_FINDINGS_INCOMPLETE` | FAILURE | `debrief brief findings are incomplete` |
| `debrief.evidence_source_id_invalid` | `DEBRIEF_EVIDENCE_SOURCE_ID_INVALID` | FAILURE | `debrief evidence source ID is invalid` |
| `debrief.evidence_metadata_differs` | `DEBRIEF_EVIDENCE_METADATA_DIFFERS` | FAILURE | `debrief evidence source metadata differs` |
| `debrief.cited_metadata_absent` | `DEBRIEF_CITED_METADATA_ABSENT` | FAILURE | `debrief cited evidence source metadata is absent` |
| `debrief.source_hash_changed` | `DEBRIEF_SOURCE_HASH_CHANGED` | FAILURE | `debrief source or context hash changed` |

### calibrate

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `calibrate.trial_not_object` | `CALIBRATE_TRIAL_NOT_OBJECT` | INPUT_REJECTED | `trial entry is not an object in {origin}` |
| `calibrate.trial_speaker_missing` | `CALIBRATE_TRIAL_SPEAKER_MISSING` | INPUT_REJECTED | `trial entry lacks speaker_id in {origin}` |
| `calibrate.trial_label_invalid` | `CALIBRATE_TRIAL_LABEL_INVALID` | INPUT_REJECTED | `trial entry has bad label in {origin}` |
| `calibrate.trial_scores_invalid` | `CALIBRATE_TRIAL_SCORES_INVALID` | INPUT_REJECTED | `trial entry has bad scores in {origin}` |
| `calibrate.trial_flags_invalid` | `CALIBRATE_TRIAL_FLAGS_INVALID` | INPUT_REJECTED | `trial entry has bad flags in {origin}` |
| `calibrate.journal_unreadable` | `CALIBRATE_JOURNAL_UNREADABLE` | INPUT_REJECTED | `journal cannot be read: {path}` |
| `calibrate.journal_line_invalid` | `CALIBRATE_JOURNAL_LINE_INVALID` | INPUT_REJECTED | `journal line is not JSON in {origin}` |
| `calibrate.model_digest_missing` | `CALIBRATE_MODEL_DIGEST_MISSING` | INPUT_REJECTED | `model version holds no sha256 digest: {model_version}` |
| `calibrate.dev_trials_incomplete` | `CALIBRATE_DEV_TRIALS_INCOMPLETE` | CALIBRATION_REJECTED | `dev journals need both target and nontarget trials` |
| `calibrate.cohort_no_spread` | `CALIBRATE_COHORT_NO_SPREAD` | CALIBRATION_REJECTED | `dev cohort has no spread` |
| `calibrate.reliability_exceeds_bound` | `CALIBRATE_RELIABILITY_EXCEEDS_BOUND` | CALIBRATION_REJECTED | `dev reliability exceeds bound` |
| `calibrate.no_operating_point` | `CALIBRATE_NO_OPERATING_POINT` | CALIBRATION_REJECTED | `dev scores show no operating point` |
| `calibrate.theta_outside_valley` | `CALIBRATE_THETA_OUTSIDE_VALLEY` | CALIBRATION_REJECTED | `theta sits outside a margin valley` |
| `calibrate.margins_no_valley` | `CALIBRATE_MARGINS_NO_VALLEY` | CALIBRATION_REJECTED | `dev margins show no valley` |
| `calibrate.margin_floor_outside_valley` | `CALIBRATE_MARGIN_FLOOR_OUTSIDE_VALLEY` | CALIBRATION_REJECTED | `margin floor sits outside a margin valley` |
| `calibrate.artifact_not_written` | `CALIBRATE_ARTIFACT_NOT_WRITTEN` | FAILURE | `artifact cannot be written: {path}` |

### audio

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `audio.retention_days_not_positive` | `AUDIO_RETENTION_DAYS_NOT_POSITIVE` | INPUT_REJECTED | `--days must be positive` |
| `audio.root_missing` | `AUDIO_ROOT_MISSING` | INPUT_REJECTED | `audio root is missing: {path}` |

### visual

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `visual.root_missing` | `VISUAL_ROOT_MISSING` | INPUT_REJECTED | `visual root is missing: {path}` |
| `visual.provider_unsupported` | `VISUAL_PROVIDER_UNSUPPORTED` | INPUT_REJECTED | `unsupported visual provider: {provider}` |

### diagnostics

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `diagnostics.journal_missing` | `DIAGNOSTICS_JOURNAL_MISSING` | INPUT_REJECTED | `no journal for meeting {meeting_id} at {path}` |

### diarize

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `diarize.input_unreadable` | `DIARIZE_INPUT_UNREADABLE` | INPUT_REJECTED | `diarization input cannot be read: {reason}` |
| `diarize.correction_missing` | `DIARIZE_CORRECTION_MISSING` | INPUT_REJECTED | `no diarization correction for meeting: {meeting_id}` |
| `diarize.transcription_failed` | `DIARIZE_TRANSCRIPTION_FAILED` | FAILURE | `transcription request failed: {reason}` |
| `diarize.transcription_response_invalid` | `DIARIZE_TRANSCRIPTION_RESPONSE_INVALID` | FAILURE | `transcription response parse failed` |
| `diarize.sidecar_invalid` | `DIARIZE_SIDECAR_INVALID` | FAILURE | `diarization sidecar is invalid` |
| `diarize.schema_unsupported` | `DIARIZE_SCHEMA_UNSUPPORTED` | FAILURE | `diarization sidecar schema is unsupported` |
| `diarize.turns_invalid` | `DIARIZE_TURNS_INVALID` | FAILURE | `diarization sidecar turns are invalid` |
| `diarize.stale` | `DIARIZE_STALE` | INPUT_REJECTED | `diarization sidecar is stale: record changed since correction` |
| `diarize.slot_metadata_invalid` | `DIARIZE_SLOT_METADATA_INVALID` | FAILURE | `slot alignment metadata is invalid: {path}` |
| `diarize.slot_misaligned` | `DIARIZE_SLOT_MISALIGNED` | FAILURE | `slot audio is misaligned for diarization: {path}` |
| `diarize.slot_not_at_zero` | `DIARIZE_SLOT_NOT_AT_ZERO` | FAILURE | `slot audio does not start at zero for diarization: {path}` |
| `diarize.speaker_label_invalid` | `DIARIZE_SPEAKER_LABEL_INVALID` | FAILURE | `speaker label must be a single token without whitespace` |

### worker

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `worker.meeting_id_invalid` | `WORKER_MEETING_ID_INVALID` | INPUT_REJECTED | `invalid meeting ID: {meeting_id!r}` |
| `worker.already_running` | `WORKER_ALREADY_RUNNING` | FAILURE | `worker for meeting {meeting_id} is already running` |
| `worker.still_running` | `WORKER_STILL_RUNNING` | FAILURE | `worker for meeting {meeting_id} is still running` |
| `worker.ingest_already_running` | `WORKER_INGEST_ALREADY_RUNNING` | FAILURE | `ingest job for meeting {meeting_id} is already running` |
| `worker.start_failed` | `WORKER_START_FAILED` | FAILURE | `worker start failed: {stderr}` |
| `worker.start_no_container` | `WORKER_START_NO_CONTAINER` | FAILURE | `worker start produced no container id` |
| `worker.timeout_negative` | `WORKER_TIMEOUT_NEGATIVE` | INPUT_REJECTED | `--timeout must not be negative` |
| `worker.not_running` | `WORKER_NOT_RUNNING` | INPUT_REJECTED | `no running worker for meeting {meeting_id}` |
| `worker.ingest_no_finished_session` | `WORKER_INGEST_NO_FINISHED_SESSION` | INPUT_REJECTED | `meeting {meeting_id} has no finished session to ingest` |
| `worker.ingest_interrupted` | `WORKER_INGEST_INTERRUPTED` | FAILURE | `ingest interrupted for meeting {meeting_id}` |
| `worker.file_missing` | `WORKER_FILE_MISSING` | INPUT_REJECTED | `file not found: {path}` |
| `worker.file_not_visible` | `WORKER_FILE_NOT_VISIBLE` | INPUT_REJECTED | `file is not visible inside the worker container: {path}` |

### docker

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `docker.ps_failed` | `DOCKER_PS_FAILED` | INFRA_FAILURE | `docker ps failed: {stderr}` |
| `docker.unavailable` | `DOCKER_UNAVAILABLE` | INFRA_FAILURE | `docker is not available: {reason}` |

### operation

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `operation.interrupted` | `OPERATION_INTERRUPTED` | FAILURE | `operation interrupted` |

### provider

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `provider.unavailable` | `PROVIDER_UNAVAILABLE` | INFRA_FAILURE | `provider unavailable: {reason}` |

### ingestion

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `ingestion.episode_name_invalid` | `INGESTION_EPISODE_NAME_INVALID` | FAILURE | `invalid episode name for meeting {meeting_id}; expected {meeting_id}:w<index>; run fakeyou memory-forget --meeting with --keep-record before ingesting again` |

### persona

| Kode | Anggota | Exit | Pesan |
| --- | --- | --- | --- |
| `persona.identity_mismatch` | `PERSONA_IDENTITY_MISMATCH` | INPUT_REJECTED | `persona owner_id and twin_id must match the session identity` |
