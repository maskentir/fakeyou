# Variabel environment

Halaman ini dihasilkan dari `Settings` dan `COMPOSE_ONLY_KEYS` oleh `fakeyou.config.env_catalog`. Jangan mengedit halaman ini secara manual. Regenerasi dengan `uv run python -m fakeyou.config.env_catalog > docs/reference/environment.md`. `tests/test_env_catalog.py` gagal jika halaman ini berbeda dari hasil generator.

## Aturan

- `Settings` dibaca dari environment dengan prefix `FAKEYOU_` dan pemisah `__` untuk nesting; misalnya `FAKEYOU_REPLY__API_KEY` mengisi `settings.reply.api_key`.
- File `.env` didukung; `.env.example` mendokumentasikan setiap setting dan kelengkapannya diuji oleh `tests/test_config.py`.
- Nilai secret tidak pernah tampil di log atau laporan; kolom Default pada tabel di bawah menyamarkan nilai `SecretStr`.
- Kunci compose-only hanya dipakai `compose.yaml` atau perintah `worker`, dan tidak ada padanannya di `Settings`.

## Variabel Settings

Setiap leaf pada model `Settings` beserta nama environment yang diturunkannya. Bagian dengan beberapa varian (misalnya peran `reply`) menggabungkan field yang sama; baris yang hanya ada di varian tertentu mencantumkan variannya.

### identity

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_IDENTITY__OWNER_ID` | `identity` | `fakeyou.config.identifiers.SessionId` | `local_owner` |
| `FAKEYOU_IDENTITY__TWIN_ID` | `identity` | `fakeyou.config.identifiers.SessionId` | `local_twin` |
| `FAKEYOU_IDENTITY__TWIN_NAME` | `identity` | `str` | `Fakeyou Twin` |
| `FAKEYOU_IDENTITY__VOICEPRINT_CALIBRATION_PATH` | `identity` | `str` | - |
| `FAKEYOU_IDENTITY__VOICEPRINT_ENABLED` | `identity` | `bool` | false |
| `FAKEYOU_IDENTITY__VOICEPRINT_MIN_MARGIN` | `identity` | `float` | `0.15` |
| `FAKEYOU_IDENTITY__VOICEPRINT_MIN_SECONDS` | `identity` | `float` | `3.0` |
| `FAKEYOU_IDENTITY__VOICEPRINT_MODEL_PATH` | `identity` | `str` | - |
| `FAKEYOU_IDENTITY__VOICEPRINT_MODEL_SHA256` | `identity` | `str` | `c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5` |
| `FAKEYOU_IDENTITY__VOICEPRINT_POLICY_VERSION` | `identity` | `str` | `v1` |
| `FAKEYOU_IDENTITY__VOICEPRINT_RETENTION_DAYS` | `identity` | `int` | `90` |
| `FAKEYOU_IDENTITY__VOICEPRINT_THRESHOLD` | `identity` | `float` | `0.6` |
| `FAKEYOU_IDENTITY__VOICEPRINT_TIMEOUT_SECONDS` | `identity` | `float` | `5.0` |

### paths

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_PATHS__BROWSER_OUTPUT_DIR` | `paths` | `Path` | `.runtime/browser` |
| `FAKEYOU_PATHS__BROWSER_PROFILE_DIR` | `paths` | `Path` | `.runtime/profiles` |
| `FAKEYOU_PATHS__JOURNAL_DIR` | `paths` | `Path` | `.runtime/journals` |
| `FAKEYOU_PATHS__RECORD_DIR` | `paths` | `Path` | `.runtime/meetings` |

### database

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_DATABASE__CONNECT_TIMEOUT_SECONDS` | `database` | `float` | `5.0` |
| `FAKEYOU_DATABASE__KEEPALIVE_IDLE_SECONDS` | `database` | `float` | `30.0` |
| `FAKEYOU_DATABASE__SEND_TIMEOUT_SECONDS` | `database` | `float` | `5.0` |
| `FAKEYOU_DATABASE__STATEMENT_TIMEOUT_SECONDS` | `database` | `float` | `30.0` |
| `FAKEYOU_DATABASE__URL` | `database` | `SecretStr, None` | - |

### speech

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_SPEECH__LANGUAGE` | `speech` | `Literal['id', 'en']` | `id` |

### reply

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_REPLY__API_KEY` | `reply` | `SecretStr, None` | - |
| `FAKEYOU_REPLY__BASE_URL (CompatibleReply)` | `reply` | `str` | wajib |
| `FAKEYOU_REPLY__BASE_URL (AnthropicReply)` | `reply` | `str, None` | `https://api.anthropic.com` |
| `FAKEYOU_REPLY__BASE_URL (OpenAIReply)` | `reply` | `str, None` | `https://api.openai.com/v1` |
| `FAKEYOU_REPLY__BASE_URL (GoogleReply)` | `reply` | `str, None` | `https://generativelanguage.googleapis.com` |
| `FAKEYOU_REPLY__EXTRA_BODY (CompatibleReply)` | `reply` | `dict[str, Any], None` | - |
| `FAKEYOU_REPLY__MAX_RETRIES` | `reply` | `int` | `2` |
| `FAKEYOU_REPLY__MODEL (AnthropicReply)` | `reply` | `str` | `claude-haiku-4-5-20251001` |
| `FAKEYOU_REPLY__MODEL (GoogleReply)` | `reply` | `str` | `gemini-3.8-flash` |
| `FAKEYOU_REPLY__MODEL (CompatibleReply)` | `reply` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_REPLY__MODEL (OpenAIReply)` | `reply` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_REPLY__PROVIDER` | `reply` | `Literal['anthropic', 'google', 'openai', 'openai_compatible']` | `openai` |
| `FAKEYOU_REPLY__REASONING_EFFORT (GoogleReply)` | `reply` | `Literal['low', 'medium', 'high'], None` | `low` |
| `FAKEYOU_REPLY__REASONING_EFFORT (CompatibleReply)` | `reply` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_REPLY__REASONING_EFFORT (OpenAIReply)` | `reply` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_REPLY__TIMEOUT_SECONDS` | `reply` | `float` | `15.0` |

### reply_fallback

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_REPLY_FALLBACK__API_KEY` | `reply_fallback` | `SecretStr, None` | - |
| `FAKEYOU_REPLY_FALLBACK__BASE_URL (CompatibleReply)` | `reply_fallback` | `str` | wajib |
| `FAKEYOU_REPLY_FALLBACK__BASE_URL (AnthropicReply)` | `reply_fallback` | `str, None` | `https://api.anthropic.com` |
| `FAKEYOU_REPLY_FALLBACK__BASE_URL (OpenAIReply)` | `reply_fallback` | `str, None` | `https://api.openai.com/v1` |
| `FAKEYOU_REPLY_FALLBACK__BASE_URL (GoogleReply)` | `reply_fallback` | `str, None` | `https://generativelanguage.googleapis.com` |
| `FAKEYOU_REPLY_FALLBACK__EXTRA_BODY (CompatibleReply)` | `reply_fallback` | `dict[str, Any], None` | - |
| `FAKEYOU_REPLY_FALLBACK__MAX_RETRIES` | `reply_fallback` | `int` | `2` |
| `FAKEYOU_REPLY_FALLBACK__MODEL (AnthropicReply)` | `reply_fallback` | `str` | `claude-haiku-4-5-20251001` |
| `FAKEYOU_REPLY_FALLBACK__MODEL (GoogleReply)` | `reply_fallback` | `str` | `gemini-3.8-flash` |
| `FAKEYOU_REPLY_FALLBACK__MODEL (CompatibleReply)` | `reply_fallback` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_REPLY_FALLBACK__MODEL (OpenAIReply)` | `reply_fallback` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_REPLY_FALLBACK__PROVIDER` | `reply_fallback` | `Literal['anthropic', 'google', 'openai', 'openai_compatible']` | - |
| `FAKEYOU_REPLY_FALLBACK__REASONING_EFFORT (GoogleReply)` | `reply_fallback` | `Literal['low', 'medium', 'high'], None` | `low` |
| `FAKEYOU_REPLY_FALLBACK__REASONING_EFFORT (CompatibleReply)` | `reply_fallback` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_REPLY_FALLBACK__REASONING_EFFORT (OpenAIReply)` | `reply_fallback` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_REPLY_FALLBACK__TIMEOUT_SECONDS` | `reply_fallback` | `float` | `15.0` |

### debrief

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_DEBRIEF__API_KEY` | `debrief` | `SecretStr, None` | - |
| `FAKEYOU_DEBRIEF__BASE_URL (CompatibleReply)` | `debrief` | `str` | wajib |
| `FAKEYOU_DEBRIEF__BASE_URL (AnthropicReply)` | `debrief` | `str, None` | `https://api.anthropic.com` |
| `FAKEYOU_DEBRIEF__BASE_URL (OpenAIReply)` | `debrief` | `str, None` | `https://api.openai.com/v1` |
| `FAKEYOU_DEBRIEF__BASE_URL (GoogleReply)` | `debrief` | `str, None` | `https://generativelanguage.googleapis.com` |
| `FAKEYOU_DEBRIEF__EXTRA_BODY (CompatibleReply)` | `debrief` | `dict[str, Any], None` | - |
| `FAKEYOU_DEBRIEF__MAX_RETRIES` | `debrief` | `int` | `2` |
| `FAKEYOU_DEBRIEF__MODEL (AnthropicReply)` | `debrief` | `str` | `claude-haiku-4-5-20251001` |
| `FAKEYOU_DEBRIEF__MODEL (GoogleReply)` | `debrief` | `str` | `gemini-3.8-flash` |
| `FAKEYOU_DEBRIEF__MODEL (CompatibleReply)` | `debrief` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_DEBRIEF__MODEL (OpenAIReply)` | `debrief` | `str` | `gpt-4.1-mini` |
| `FAKEYOU_DEBRIEF__PROVIDER` | `debrief` | `Literal['anthropic', 'google', 'openai', 'openai_compatible']` | - |
| `FAKEYOU_DEBRIEF__REASONING_EFFORT (GoogleReply)` | `debrief` | `Literal['low', 'medium', 'high'], None` | `low` |
| `FAKEYOU_DEBRIEF__REASONING_EFFORT (CompatibleReply)` | `debrief` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_DEBRIEF__REASONING_EFFORT (OpenAIReply)` | `debrief` | `Literal['minimal', 'low', 'medium', 'high'], None` | - |
| `FAKEYOU_DEBRIEF__TIMEOUT_SECONDS` | `debrief` | `float` | `15.0` |

### tts

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_TTS__API_KEY` | `tts` | `SecretStr, None` | - |
| `FAKEYOU_TTS__BASE_URL (CartesiaTTS)` | `tts` | `str` | `https://api.cartesia.ai` |
| `FAKEYOU_TTS__BASE_URL (ElevenLabsTTS)` | `tts` | `str` | `https://api.elevenlabs.io` |
| `FAKEYOU_TTS__BASE_URL (MiniMaxTTS)` | `tts` | `str` | `https://api.minimax.io/v1/t2a_v2` |
| `FAKEYOU_TTS__BASE_URL (OpenAITTS)` | `tts` | `str` | `https://api.openai.com/v1` |
| `FAKEYOU_TTS__MODEL (ElevenLabsTTS)` | `tts` | `str` | `eleven_multilingual_v2` |
| `FAKEYOU_TTS__MODEL (OpenAITTS)` | `tts` | `str` | `gpt-4o-mini-tts` |
| `FAKEYOU_TTS__MODEL (CartesiaTTS)` | `tts` | `str` | `sonic-3` |
| `FAKEYOU_TTS__MODEL (MiniMaxTTS)` | `tts` | `str` | `speech-2.8-turbo` |
| `FAKEYOU_TTS__PROVIDER` | `tts` | `Literal['cartesia', 'elevenlabs', 'minimax', 'openai']` | `openai` |
| `FAKEYOU_TTS__SAMPLE_RATE` | `tts` | `int` | `24000` |
| `FAKEYOU_TTS__VOICE (MiniMaxTTS)` | `tts` | `str` | `Indonesian_CalmWoman` |
| `FAKEYOU_TTS__VOICE (OpenAITTS)` | `tts` | `str` | `alloy` |
| `FAKEYOU_TTS__VOICE (CartesiaTTS)` | `tts` | `str` | `f3b7c445-d015-40f7-a665-9595e0bc0c90` |
| `FAKEYOU_TTS__VOICE (ElevenLabsTTS)` | `tts` | `str` | wajib |

### stt

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_STT__API_KEY` | `stt` | `SecretStr, None` | - |
| `FAKEYOU_STT__DIARIZE_MAX_RETRIES` | `stt` | `int` | `3` |
| `FAKEYOU_STT__DIARIZE_TIMEOUT_SECONDS` | `stt` | `float` | `60.0` |
| `FAKEYOU_STT__IDLE_RECONNECT_SECONDS` | `stt` | `float` | `0.0` |
| `FAKEYOU_STT__MODE` | `stt` | `Literal['ENDPOINTING', 'PUSH_TO_TALK', 'DIARIZATION']` | `DIARIZATION` |
| `FAKEYOU_STT__MODEL` | `stt` | `str` | `muse-voice-transcribe-1.0` |
| `FAKEYOU_STT__PROVIDER` | `stt` | `Literal['muse']` | `muse` |
| `FAKEYOU_STT__READY_TIMEOUT_SECONDS` | `stt` | `float` | `10.0` |
| `FAKEYOU_STT__URL` | `stt` | `str` | `wss://api.meta.ai/v1/asr/realtime` |

### vad

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_VAD__CONFIDENCE` | `vad` | `float` | `0.7` |
| `FAKEYOU_VAD__MIN_VOLUME` | `vad` | `float` | `0.6` |
| `FAKEYOU_VAD__START_SECS` | `vad` | `float` | `0.2` |
| `FAKEYOU_VAD__STOP_SECS` | `vad` | `float` | `0.2` |

### memory

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_MEMORY__CLOSE_CALLBACK_SECONDS` | `memory` | `float` | `5.0` |
| `FAKEYOU_MEMORY__CLOSE_HARD_SECONDS` | `memory` | `float` | `30.0` |
| `FAKEYOU_MEMORY__EMBEDDER__API_KEY` | `memory` | `SecretStr, None` | - |
| `FAKEYOU_MEMORY__EMBEDDER__BASE_URL` | `memory` | `str, None` | `https://api.openai.com/v1` |
| `FAKEYOU_MEMORY__EMBEDDER__DIM` | `memory` | `int` | `1024` |
| `FAKEYOU_MEMORY__EMBEDDER__MAX_RETRIES` | `memory` | `int` | `2` |
| `FAKEYOU_MEMORY__EMBEDDER__MODEL` | `memory` | `str` | `text-embedding-3-small` |
| `FAKEYOU_MEMORY__EMBEDDER__TIMEOUT_SECONDS` | `memory` | `float` | `60.0` |
| `FAKEYOU_MEMORY__INGESTION__EPISODE_TIMEOUT_SECONDS` | `memory` | `float` | `120.0` |
| `FAKEYOU_MEMORY__INGESTION__WINDOW_SECONDS` | `memory` | `float` | `60.0` |
| `FAKEYOU_MEMORY__INGESTION__WINDOW_UTTERANCES` | `memory` | `int` | `20` |
| `FAKEYOU_MEMORY__LLM__API_KEY (AnthropicMemoryLLM)` | `memory` | `SecretStr, None` | - |
| `FAKEYOU_MEMORY__LLM__API_KEY (GenericMemoryLLM)` | `memory` | `SecretStr, None` | - |
| `FAKEYOU_MEMORY__LLM__API_KEY (OpenAIMemoryLLM)` | `memory` | `SecretStr, None` | - |
| `FAKEYOU_MEMORY__LLM__BASE_URL (GenericMemoryLLM)` | `memory` | `str` | wajib |
| `FAKEYOU_MEMORY__LLM__BASE_URL (AnthropicMemoryLLM)` | `memory` | `str, None` | `https://api.anthropic.com` |
| `FAKEYOU_MEMORY__LLM__BASE_URL (OpenAIMemoryLLM)` | `memory` | `str, None` | `https://api.openai.com/v1` |
| `FAKEYOU_MEMORY__LLM__CLIENT (AnthropicMemoryLLM)` | `memory` | `Literal['anthropic']` | `anthropic` |
| `FAKEYOU_MEMORY__LLM__CLIENT (OpenAIMemoryLLM)` | `memory` | `Literal['openai']` | `openai` |
| `FAKEYOU_MEMORY__LLM__CLIENT (GenericMemoryLLM)` | `memory` | `Literal['openai_generic']` | `openai_generic` |
| `FAKEYOU_MEMORY__LLM__MAX_RETRIES (AnthropicMemoryLLM)` | `memory` | `int` | `2` |
| `FAKEYOU_MEMORY__LLM__MAX_RETRIES (GenericMemoryLLM)` | `memory` | `int` | `2` |
| `FAKEYOU_MEMORY__LLM__MAX_RETRIES (OpenAIMemoryLLM)` | `memory` | `int` | `2` |
| `FAKEYOU_MEMORY__LLM__MODEL (AnthropicMemoryLLM)` | `memory` | `str` | `claude-haiku-4-5-20251001` |
| `FAKEYOU_MEMORY__LLM__MODEL (GenericMemoryLLM)` | `memory` | `str` | `gpt-5.5` |
| `FAKEYOU_MEMORY__LLM__MODEL (OpenAIMemoryLLM)` | `memory` | `str` | `gpt-5.5` |
| `FAKEYOU_MEMORY__LLM__SMALL_MODEL (AnthropicMemoryLLM)` | `memory` | `str` | `claude-haiku-4-5-20251001` |
| `FAKEYOU_MEMORY__LLM__SMALL_MODEL (GenericMemoryLLM)` | `memory` | `str` | `gpt-4.1-nano` |
| `FAKEYOU_MEMORY__LLM__SMALL_MODEL (OpenAIMemoryLLM)` | `memory` | `str` | `gpt-4.1-nano` |
| `FAKEYOU_MEMORY__LLM__TIMEOUT_SECONDS (AnthropicMemoryLLM)` | `memory` | `float` | `60.0` |
| `FAKEYOU_MEMORY__LLM__TIMEOUT_SECONDS (GenericMemoryLLM)` | `memory` | `float` | `60.0` |
| `FAKEYOU_MEMORY__LLM__TIMEOUT_SECONDS (OpenAIMemoryLLM)` | `memory` | `float` | `60.0` |
| `FAKEYOU_MEMORY__RECALL__RESULT_LIMIT` | `memory` | `int` | `5` |
| `FAKEYOU_MEMORY__RECALL__STRATEGY` | `memory` | `Literal['tool', 'prefetch']` | `prefetch` |
| `FAKEYOU_MEMORY__RECALL__TIMEOUT_SECONDS` | `memory` | `float` | `15.0` |
| `FAKEYOU_MEMORY__STORE__DATABASE` | `memory` | `str` | `neo4j` |
| `FAKEYOU_MEMORY__STORE__GROUP` | `memory` | `str` | `local_twin` |
| `FAKEYOU_MEMORY__STORE__PASSWORD` | `memory` | `SecretStr, None` | - |
| `FAKEYOU_MEMORY__STORE__URI` | `memory` | `str` | `bolt://localhost:7687` |
| `FAKEYOU_MEMORY__STORE__USER` | `memory` | `str` | `neo4j` |

### conversation

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_CONVERSATION__ACTIVATION_SECONDS` | `conversation` | `float` | `30.0` |
| `FAKEYOU_CONVERSATION__ATTRIBUTION_PROBE_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__BARGE_IN_MIN_WORDS` | `conversation` | `int` | `3` |
| `FAKEYOU_CONVERSATION__HISTORY_MAX_TOKENS` | `conversation` | `int` | `32000` |
| `FAKEYOU_CONVERSATION__MAX_SPOKEN_SENTENCES` | `conversation` | `int` | `2` |
| `FAKEYOU_CONVERSATION__MEET_ACTIONS_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__MEET_CHAT_PER_MINUTE` | `conversation` | `int` | `3` |
| `FAKEYOU_CONVERSATION__PARTICIPANT_EVIDENCE_MAX_BYTES` | `conversation` | `int` | `8388608` |
| `FAKEYOU_CONVERSATION__PARTICIPANT_POLICY_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__PREGEN_FUZZY_RESTART_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__PRE_GENERATE_ENABLED` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__RESPONSE_TIMEOUT_SECONDS` | `conversation` | `float` | `60.0` |
| `FAKEYOU_CONVERSATION__SENTENCE_STREAMING` | `conversation` | `bool` | false |
| `FAKEYOU_CONVERSATION__VOICE_LABELS_ENABLED` | `conversation` | `bool` | false |

### participation_decider

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_PARTICIPATION_DECIDER__API_KEY` | `participation_decider` | `SecretStr, None` | - |
| `FAKEYOU_PARTICIPATION_DECIDER__BASE_URL` | `participation_decider` | `str, None` | - |
| `FAKEYOU_PARTICIPATION_DECIDER__CLARIFY_THRESHOLD` | `participation_decider` | `float` | `0.5` |
| `FAKEYOU_PARTICIPATION_DECIDER__MODEL` | `participation_decider` | `str` | `jev-latest` |
| `FAKEYOU_PARTICIPATION_DECIDER__PRE_GENERATE_ENABLED` | `participation_decider` | `bool` | true |
| `FAKEYOU_PARTICIPATION_DECIDER__PROVIDER` | `participation_decider` | `Literal['jev', 'gemini']` | `jev` |
| `FAKEYOU_PARTICIPATION_DECIDER__SPEAK_THRESHOLD` | `participation_decider` | `float` | `0.7` |
| `FAKEYOU_PARTICIPATION_DECIDER__TIMEOUT_SECONDS` | `participation_decider` | `float` | `15.0` |
| `FAKEYOU_PARTICIPATION_DECIDER__WINDOW_TURNS` | `participation_decider` | `int` | `6` |

### meeting

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_MEETING__JOIN_MAX_ATTEMPTS` | `meeting` | `int` | `3` |
| `FAKEYOU_MEETING__JOIN_POLL_SECONDS` | `meeting` | `float` | `2.0` |
| `FAKEYOU_MEETING__JOIN_RETRY_BASE_SECONDS` | `meeting` | `float` | `2.0` |
| `FAKEYOU_MEETING__JOIN_RETRY_CAP_SECONDS` | `meeting` | `float` | `30.0` |
| `FAKEYOU_MEETING__LEAVE_TIMEOUT_SECONDS` | `meeting` | `float` | `30.0` |
| `FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS` | `meeting` | `float` | `300.0` |
| `FAKEYOU_MEETING__MONITOR_POLL_SECONDS` | `meeting` | `float` | `5.0` |
| `FAKEYOU_MEETING__SESSION_DURATION_SECONDS` | `meeting` | `float` | `7200.0` |

### browser

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_BROWSER__HEADLESS` | `browser` | `bool` | false |
| `FAKEYOU_BROWSER__LICENSE_KEY` | `browser` | `SecretStr, None` | - |
| `FAKEYOU_BROWSER__WARMUP` | `browser` | `bool` | true |

### audio

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_AUDIO__DRAIN_FAILURE_JOURNAL_EVERY` | `audio` | `int` | `10` |
| `FAKEYOU_AUDIO__DRAIN_MAX_CONSECUTIVE_FAILURES` | `audio` | `int` | `40` |
| `FAKEYOU_AUDIO__PACTL_TIMEOUT_SECONDS` | `audio` | `float` | `5.0` |
| `FAKEYOU_AUDIO__PERSIST_ENABLED` | `audio` | `bool` | false |
| `FAKEYOU_AUDIO__RETENTION_DAYS` | `audio` | `int` | `30` |

### audio_health

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_AUDIO_HEALTH__ALONE_LEAVE_SECONDS` | `audio_health` | `float` | `600.0` |
| `FAKEYOU_AUDIO_HEALTH__DEAF_GRACE_SECONDS` | `audio_health` | `float` | `120.0` |
| `FAKEYOU_AUDIO_HEALTH__DEAF_WINDOW_SECONDS` | `audio_health` | `float` | `120.0` |

### preflight

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_PREFLIGHT__BUDGET_SECONDS` | `preflight` | `float` | `5.0` |

### worker

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_WORKER__AUDIO` | `worker` | `bool` | false |
| `FAKEYOU_WORKER__DOCKER_TIMEOUT_SECONDS` | `worker` | `float` | `30.0` |
| `FAKEYOU_WORKER__START_TIMEOUT_SECONDS` | `worker` | `float` | `600.0` |

### visual

| Variabel | Bagian | Tipe | Default |
| --- | --- | --- | --- |
| `FAKEYOU_VISUAL__API_KEY` | `visual` | `SecretStr, None` | - |
| `FAKEYOU_VISUAL__BASE_URL` | `visual` | `str, None` | - |
| `FAKEYOU_VISUAL__BUDGET_USD` | `visual` | `float` | `0.5` |
| `FAKEYOU_VISUAL__ENABLED` | `visual` | `bool` | false |
| `FAKEYOU_VISUAL__MAX_CALLS` | `visual` | `int` | `120` |
| `FAKEYOU_VISUAL__MAX_CONCURRENT` | `visual` | `int` | `2` |
| `FAKEYOU_VISUAL__MAX_IMAGES` | `visual` | `int` | `2` |
| `FAKEYOU_VISUAL__MAX_RETRIES` | `visual` | `int` | `2` |
| `FAKEYOU_VISUAL__MODEL` | `visual` | `str` | `gemini-3.8-flash` |
| `FAKEYOU_VISUAL__PROVIDER` | `visual` | `Literal['openai', 'openai_compatible', 'anthropic', 'google']` | `google` |
| `FAKEYOU_VISUAL__RETENTION_RAW_DAYS` | `visual` | `int` | `7` |
| `FAKEYOU_VISUAL__RETENTION_TEXT_DAYS` | `visual` | `int` | `30` |
| `FAKEYOU_VISUAL__TIMEOUT_SECONDS` | `visual` | `float` | `30.0` |

## Kunci compose-only

Kunci ini hanya dibaca `compose.yaml` atau perintah `worker`, dan tidak dipetakan ke `Settings`. `tests/test_config.py` gagal bila ada variabel `FAKEYOU_COMPOSE_*` di `compose.yaml` yang belum terdaftar di `COMPOSE_ONLY_KEYS`.

| Variabel | Dipakai untuk |
| --- | --- |
| `FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE` | Image service `embeddings` pada `compose.yaml`. |
| `FAKEYOU_COMPOSE_EMBEDDINGS_MODEL_ID` | Model embedding yang dijalankan service `embeddings`. |
| `FAKEYOU_COMPOSE_EMBEDDINGS_REVISION` | Revisi model embedding yang dijalankan service `embeddings`. |
| `FAKEYOU_COMPOSE_WORKER_CPUS` | Jumlah CPU container `meeting-worker` (`cpus` pada `compose.yaml`); default `2`. |
| `FAKEYOU_COMPOSE_WORKER_DATABASE_URL` | URL Postgres dari dalam container worker; dipetakan ke `FAKEYOU_DATABASE__URL` oleh `compose.yaml`. |
| `FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL` | Base URL service embedding lokal; dipetakan ke `FAKEYOU_MEMORY__EMBEDDER__BASE_URL` oleh `compose.yaml`. |
| `FAKEYOU_COMPOSE_WORKER_MEM_LIMIT` | Batas memori container `meeting-worker` (`mem_limit` pada `compose.yaml`); default `4g`. |
| `FAKEYOU_COMPOSE_WORKER_NEO4J_URI` | URI Neo4j dari dalam container worker; dipetakan ke `FAKEYOU_MEMORY__STORE__URI` oleh `compose.yaml`. |

## Variabel non-Settings

Variabel di luar model `Settings` yang dibaca langsung oleh aplikasi, compose, atau pengujian.

| Variabel | Dipakai untuk | Default |
| --- | --- | --- |
| `BRIEF_ARTIFACT_DIR` | Direktori artefak eval brief live. | Direktori sementara pytest. |
| `DEBRIEF_ARTIFACT_DIR` | Direktori artefak eval debrief live. | Direktori sementara pytest. |
| `DEBRIEF_REVIEWS_PATH` | Path ulasan independen untuk eval debrief live; kosong berarti status `pending`. | Kosong. |
| `EVAL_JUDGE_API_KEY` | Mengaktifkan judge model live pada eval; kosong berarti judge nonaktif. | Kosong. |
| `EMBEDDING_DIM` | Ditulis CLI dari `FAKEYOU_MEMORY__EMBEDDER__DIM` agar terbaca library embedding Graphiti. | Mengikuti dim embedder. |
| `POSTGRES_PASSWORD` | Password service `postgres` pada `compose.yaml`. Compose menolak start tanpa nilai ini. | Wajib diisi. |
| `PULSE_SINK` | Sink output audio agen. | `agent_output` |
| `PULSE_SOURCE` | Sumber monitor PulseAudio untuk audio meeting. | `meeting_audio.monitor` |
| `SPEAKER_ARTIFACT_DIR` | Direktori artefak eval speaker probe live. | Direktori sementara pytest. |
| `SPEAKER_MEASURE_FIX` | Bila diisi, tes speaker probe live melewati asersi artefak tak berubah. | Kosong. |
| `TEST_DATABASE_URL` | URL database untuk fixture postgres pada suite pytest; kosong berarti tes postgres di-skip. | Kosong. |
| `TRUST_ARTIFACT_DIR` | Direktori artefak eval trust live. | Direktori sementara pytest. |
| `VOICEPRINT_TEST_MODEL` | Path model ONNX untuk tes embedding voiceprint. | `/tmp/f2-spike/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx` |
| `MUSE_DIARIZATION_AUDIO` | File WAV mono 16 kHz berisi dua pembicara untuk tes live diarisasi Muse. | Wajib bila tes live dijalankan. |
