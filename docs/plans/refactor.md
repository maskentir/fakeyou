# Rencana refactor: DRY, konfigurasi eksplisit, dan taksonomi

Status: fase 0–5 diimplementasikan di branch `feat/explicit-config-refactor` (1 Oktober 2026); lihat [status implementasi](#status-implementasi). Dasar: audit baca-saja atas 26 modul `src/fakeyou` (6,8 ribu baris), `tests/` dan `evals/` (12,7 ribu baris), `compose.yaml`, dan dokumentasi. Temuan berdampak besar diverifikasi ulang langsung di kode dan data `.runtime/`. Re-ingest `acc2` dijalankan setelah refactor selesai.

## Prinsip

1. **Satu definisi per konsep.** Konstanta, default, pemetaan provider, jalur event, dan fake tes masing-masing punya satu sumber. Duplikat yang harus tetap sama dilindungi tes, bukan disalin.
2. **Configuration over Convention.** Perilaku berasal dari konfigurasi eksplisit yang divalidasi saat startup. Tidak ada provider yang ditebak dari prefix string, tidak ada kelas client yang dipilih dari ada atau tidaknya URL, tidak ada key yang meminjam key peran lain, dan salah ketik env ditolak. Default tetap boleh, tetapi tertulis di satu skema dan terlihat di `fakeyou check`. Nilai yang merupakan invarian protokol (format PCM, kode keluar, regex identitas) tetap konstanta di satu modul.
3. **Taksonomi tertulis dan ditegakkan otomatis.** Aturan penamaan untuk konfigurasi, CLI, journal, fault, lifecycle, kode, tes, dan dokumen dicek oleh tes, bukan hanya disepakati.
4. **Fitur library dulu.** pydantic-settings (nested env, discriminated union, `extra="forbid"`), `init_chat_model(model_provider=...)`, argparse `set_defaults`, `typing.get_args`, `dataclasses.replace`, `logging`, pytest fixture, `unittest.mock.create_autospec`, `httpx2.MockTransport`.
5. **Bertahap dan selalu hijau.** Setiap fase berdiri sendiri, lulus seluruh verifikasi, dan di-commit terpisah.

## Bug yang ditemukan audit

| # | Masalah | Bukti |
| --- | --- | --- |
| B1 | Fault tidak pernah masuk journal. `FaultReporter` mencetak ke stdout dan CLI tidak menyambungkannya ke journal, sehingga bagian `faults` di `fakeyou report` selalu kosong di sesi nyata | 0 baris `fault` di 13 journal `.runtime/audio/journals/`, termasuk sesi dengan fault drain timeout. `callback_fault`, `monitor_poll`, `cli_exit` juga hanya ke stdout |
| B2 | Journal menyimpan isi percakapan: teks transkrip final dan ucapan twin, padahal kontraknya metadata saja. Journal juga tercetak ke stdout sehingga isi ikut masuk `docker logs` | 25 baris berisi teks di `acc2-0930-s1.jsonl` (`transcript_final` 9, `speak_*` 16). Redaksi berbasis nama field tidak mencakup `text` |
| B3 | Key lintas peran: bila key peran kosong, key OpenAI dikirim ke host pihak ketiga (Muse, TEI, MiniMax) | 5 titik `or openai_api_key` / `or config.api_key` di `cli.py` dan `memory.py` |
| B4 | `memory_config` selalu mewajibkan key OpenAI walau key LLM dan embedding memori sudah diatur | `cli.py` `memory_config` |
| B5 | Dua database checkpoint. `memory-forget` hanya menghapus thread percakapan, bukan thread lifecycle | `.runtime/audio/checkpoints.sqlite` dan `.runtime/audio/meeting-checkpoints.sqlite`; path lifecycle tidak ada di `Settings` |
| B6 | Setelan sesi dan percakapan yang sudah divalidasi tidak dapat diatur: poll join, leave, jendela deaf, budget preflight, batas jawaban 60 s, aktivasi wake phrase | `SessionConfig` hanya menerima lobby, durasi, headless dari CLI; `ConversationLoop` dibuat tanpa parameter itu |
| B7 | Bahasa Indonesia tertanam di STT, pemecah kalimat, instruksi TTS, dan voice. Persona `language: en` tetap mendapat STT dan TTS Indonesia | `Language.ID`, `language="id"`, `language_boost="Indonesian"` |
| B8 | Preflight membuang skema dan port (`http://127.0.0.1:8000` diprobe sebagai TLS 443), host provider yang tidak dikenal hilang dari preflight, host memori tidak pernah diprobe | `preflight_hosts`, `PROVIDER_API_URLS.get(provider, "")` |
| B9 | Env yang salah ketik atau tidak dipakai diam-diam diabaikan | `extra="ignore"`; `FAKEYOU_ANTHROPIC_API_KEY` di `.env` tidak dibaca apa pun |
| B10 | Batas waktu terikat lintas file tanpa validasi: 20 s model × retry + recall 15 s harus muat di batas jawaban 60 s | `cli.py`, `conversation.py`, `memory.py`, README |

## Fase 0 — Perbaiki bug data dan sink (tanpa rename)

- B1: `FaultReporter(journal=...)` mencatat lewat `journal.record("fault", ...)`; `callback_fault`, `monitor_poll`, `cli_exit` juga lewat journal. Reporter kehilangan jam, log, dan `meeting_id` sendiri.
- B2: field isi (`text`) dihapus dari event journal. Laporan menghitung jawaban yang diakhiri pertanyaan dari field boolean `ends_with_question` yang dicatat saat playback. Tes memastikan tidak ada baris journal yang membawa isi.
- B5: satu path checkpoint untuk percakapan dan lifecycle (thread terpisah dalam satu database), sehingga `memory-forget` menghapus keduanya.
- Kode mati: `ProviderJournal.request`, `DeafMonitor(log=)`, `CaptureActivity.observe_frame`, parameter `log` `record_consumer_fault`, `agent_invoke` (mengukur rentang yang sama dengan `agent_finished`), cabang ternary identik di `probe_host`, `try` ganda di `_fault_report`.

## Fase 1 — Fondasi tes sebelum refactor besar

Refactor fase berikutnya menyentuh `fake_settings` di 59 tempat dan fake Graphiti di 8 tempat. Fondasinya dibuat dulu agar perubahan berikutnya murah dan fake tidak tertinggal dari interface asli.

- `tests/conftest.py` dan `tests/fakes.py` (belum ada saat ini). `evals/test_persona.py` diganti nama menjadi `evals/test_persona_eval.py` karena bentrok basename dengan `tests/test_persona.py`.
- Fixture `settings`: `Settings` asli dengan env `FAKEYOU_*` dibersihkan, sehingga field baru otomatis ikut dan salah ketik ditolak.
- Fixture `graphiti`: `create_autospec(Graphiti, instance=True)` menggantikan delapan fake tangan yang menerima `**kwargs` apa pun.
- Fixture `journal` dengan `.events(name)`, `FakeClock` tunggal, `FakePage` Meet gabungan (dua salinan yang sudah berbeda), `ToolCallingFakeModel`, `recall_call`, `OfflineJudge`, fixture `join` dan `session_run` untuk `test_cli`, fixture `open_audio` untuk `test_runtime_audio`, STT doubles berparameter.
- Fake OpenAI SDK di `test_voice` diganti `httpx2.MockTransport` seperti tes MiniMax.
- Parametrize tes kembar (hasil terminal runtime, mikrofon dan kamera, kode keluar), pindahkan asersi yang tercatat dua kali ke satu modul.
- `mypy` ikut memeriksa `tests/fakes.py`, sehingga fake yang tidak memenuhi Protocol (misalnya `Conversation` tanpa `is_busy`) tertangkap.
- Perkiraan: sekitar 1.200 baris tes berkurang tanpa mengurangi cakupan.

## Fase 2 — Konfigurasi eksplisit dan taksonomi konfigurasi

Skema `Settings` berbentuk bagian per peran, dibaca lewat nested env (`env_nested_delimiter="__"`), `extra="forbid"`, dan `dotenv_filtering="match_prefix"`. Provider adalah discriminator; setiap varian hanya menerima field yang berlaku untuknya.

```text
identity       owner_id, twin_id, twin_name
paths          journal_dir, record_dir, checkpoint_path, browser_profile_dir
speech         language (id | en)
reply          provider: openai | openai_compatible | anthropic; model, api_key, base_url,
               timeout_seconds, max_retries, reasoning_effort, extra_body (openai_compatible)
reply_fallback sama dengan reply, opsional
tts            provider: openai | minimax; model, voice, api_key, base_url, sample_rate
stt            provider: muse; model, api_key, url, mode
memory.store   uri, user, password, database, group
memory.llm     client: openai | openai_generic | anthropic; model, small_model, api_key, base_url
memory.embedder model, api_key, base_url, dim
memory.recall  result_limit, timeout_seconds
memory.ingestion window_utterances, window_seconds, episode_timeout_seconds
conversation   response_timeout_seconds, barge_in_min_words, max_spoken_sentences, activation_seconds
meeting        lobby_timeout_seconds, session_duration_seconds, join_poll_seconds, leave_timeout_seconds
audio_health   deaf_window_seconds, deaf_grace_seconds, alone_leave_seconds
preflight      budget_seconds
worker         audio (bool)
```

Contoh env: `FAKEYOU_REPLY__PROVIDER=anthropic`, `FAKEYOU_REPLY__MODEL=claude-haiku-4-5-20251001`, `FAKEYOU_TTS__PROVIDER=minimax`, `FAKEYOU_MEMORY__EMBEDDER__DIM=384`.

Aturan validasi (`model_validator(mode="after")`):

1. Setiap peran yang aktif wajib punya `api_key` sendiri; tidak ada fallback lintas peran (B3, B4).
2. `openai_compatible` wajib `base_url`; `init_chat_model` dipanggil dengan `model_provider` eksplisit dan nama model tanpa prefix. Kelas client Graphiti ditentukan field `client`, bukan ada tidaknya URL.
3. TTS: voice OpenAI harus ada di `VALID_VOICES`; `sample_rate` TTS harus sama dengan rate playback. Tidak ada tabel default per provider di kode.
4. `speech.language` dipetakan eksplisit ke bahasa STT, pemecah kalimat, instruksi TTS, dan `language_boost`. `join` menolak persona yang `language`-nya berbeda (B7).
5. Anggaran waktu: `reply.timeout_seconds × (max_retries + 1)` ditambah fallback dan `memory.recall.timeout_seconds` tidak boleh melebihi `conversation.response_timeout_seconds` (B10).
6. Target preflight diambil dari URL setiap peran lengkap dengan skema dan port, termasuk memori (B8).
7. `memory.embedder.dim` sama dengan `EMBEDDING_DIM` yang dibaca Graphiti; URL kosong dari Compose dibaca sebagai `None` oleh validator, bukan `or None` di pemanggil.
8. Variabel yang hanya dipakai Compose memakai prefix `FAKEYOU_COMPOSE_` agar tidak ditolak `extra="forbid"`.
9. `fakeyou check` mencetak `settings.model_dump(mode="json")` dengan secret disamarkan, bukan tampilan yang dirakit ulang.

Konstanta invarian dikumpulkan di satu modul: nama device PulseAudio, 16 kHz capture, 24 kHz playback, format PCM s16le mono. Default yang sekarang tersebar di `settings.py`, `session.py`, `meet.py`, `audio.py`, dan `deaf.py` (misalnya 300 s lobby tiga kali, 120 s deaf sepuluh kali) hanya ada di skema.

Migrasi: `.env` lama ditolak dengan pesan yang menyebut key barunya. Tabel pemetaan key lama ke baru ada di README bersama `.env.example` yang diperbarui.

## Fase 3 — DRY di `src`

| Area | Konsolidasi | Hemat |
| --- | --- | --- |
| Pengukuran waktu | Satu context manager `journal.timed(started=, finished=, failed=)` menggantikan enam pola timing (TTS unary dan stream, agent, tool, ingestion) | ~60 |
| Tipe fault | Satu dataclass `Fault` menggantikan `ConsumerFault`, `CaptureFault`, `_FaultRecord`, `FaultSummary`; set runtime dari `typing.get_args` pada `Literal` | ~45 |
| CLI | argparse `set_defaults(handler=...)`; satu context manager operator yang mengembalikan persona dan memori untuk `memory-*`; `TypeAdapter(SessionId)` sebagai `type=` argumen; validasi durasi cukup di pydantic | ~70 |
| Provider | Pembuatan model jawaban, TTS, STT, dan client Graphiti dari bagian konfigurasi masing-masing, tanpa percabangan string | ~40 |
| TTS | `AsyncOpenAI(timeout=..., max_retries=0)` dan `close()` SDK menggantikan pembungkus HTTP client; argumen request dipakai bersama unary dan stream | ~30 |
| Resource | Penutupan conversation diutamakan pada pembuka; batas kepemilikan API runtime dicatat sebagai pengecualian; helper `journal_path`, `journal_files`, `open_checkpointer` | ~30 |
| Audio dan playback | Spawn `paplay` tunggal, satu `_consume_task`, `CaptureActivity.snapshot` lewat `dataclasses.replace`, klasifikasi kategori error STT memakai fungsi dari `provider_faults` | ~60 |
| Meet dan preflight | Satu `_toggle_control` untuk mikrofon dan kamera; preflight tanpa dedupe ganda, timeout ganda, dan pembungkus `invoke` | ~50 |
| Transkrip | Dedupe dan filter final hanya di satu tempat | ~6 |
| Journal | `logging.Logger` non-propagating dengan `RotatingFileHandler` menggantikan `LogRecord` manual dan lock sendiri; registry event global yang hanya dipakai tes dihapus | ~20 |

Jalur playback non-streaming (retry `speak`, `create_paplay_runner`, sintesis unary) tidak pernah berjalan di produksi karena `default_playback_engine` selalu streaming. Jalur ini dihapus bila disetujui (~130 baris dan tesnya).

## Fase 4 — Taksonomi kode, journal, dan fault

**Journal.** Nama event `<objek>_<participle lampau>` (29 dari 41 event sudah mengikuti). Contoh perubahan: `speak_*` menjadi `playback_*`, `synth_*` menjadi `synthesis_*`, `stt_reconnect` dengan `outcome` menjadi `stt_reconnect_queued`, `stt_reconnect_finished`, `stt_reconnect_failed`, `stt_reconnect_skipped`, dan `transcript_final` menjadi `transcript_finalized`. `source` wajib dan diambil dari satu kosakata yang juga dipakai fault: `runtime, browser, audio, stt, tts, playback, agent, llm, memory, memory_ingest, meeting_record, preflight, cli`. Operasi (camera, leave, teardown, close) masuk field `operation`; asal data masuk field `origin`. Satuan durasi `_ms` di journal, `_seconds` di konfigurasi, kode, dan laporan.

**Fault dan lifecycle.** Fault adalah `(source, kind)` dengan `kind` dari satu `Literal` yang tidak mengulang source; masalah kesehatan audio memakai `Literal` terpisah. Event lifecycle memakai participle lampau (`owner_stopped`, `lobby_timed_out`, `duration_reached`). Alasan terminal memakai tiga keluarga: `<subsistem>_failure`, `<participle>_by_<aktor>`, dan `<batas>_reached`. Atribusi menambah `policy` untuk batas durasi dan ditinggal sendirian. Manusia selalu disebut `owner`.

**Kata kerja.**

| Kata kerja | Arti |
| --- | --- |
| `create_` | membangun objek layanan tanpa I/O jaringan |
| `open_` | `asynccontextmanager` yang memiliki resource |
| `build_` | merakit nilai murni (`build_persona` menjadi `build_system_prompt`) |
| `load_` | membaca dari disk dan memvalidasi (`read_meeting_record` menjadi `load_meeting_record`) |
| `read_` | membaca keadaan luar secara longgar (DOM, journal) |
| `ensure_` / `verify_` | membuat bila belum ada / memeriksa lalu raise |
| `run_` / `request_` | menjalankan sampai selesai / meminta ke pihak luar |

Parameter memakai satu nama per konsep (`faults`, `journal`, `meeting_record`). Protocol kapabilitas opsional memakai `Supports<X>`. Akhiran `Literal`: `Kind`, `Status`, `State`, `Phase`.

**Modul.** `cli.py` (1.018 baris, sekitar 15 tanggung jawab) dipecah menjadi paket `fakeyou/cli/` (`main`, `join`, `memory`, `inspect`, `chat`) ditambah `providers.py`, `assembly.py`, dan `paths.py`. `voice.py` dipecah menjadi `stt.py` dan `tts.py`; `callbacks.py` digabung ke `faults.py`; `ProviderJournal` menjadi `Journal`; `SessionResult` pindah ke `session.py`. Evals dan tes tidak lagi mengimpor fungsi library dari modul CLI.

**Tes dan eval.** File tes `test_<modul>[_<aspek>].py`; tingkat lingkungan hanya lewat marker. Eval `evals/test_<kapabilitas>_eval.py`; pemeriksaan offline eval pindah ke `tests/` agar ikut run default.

**Penegakan otomatis** (tes baru yang membaca kode lewat `ast` dan `tokenize`):

1. Setiap panggilan `journal.record` memakai nama event yang sesuai pola dan `source` dari kosakata.
2. Tidak ada field isi (`text`, `prompt`, `transcript`) di event journal.
3. Tidak ada komentar maupun docstring di `src/`, `tests/`, `evals/`.
4. Tidak ada kata Bahasa Indonesia di file `.py` (daftar kata dari aturan yang dipakai sekarang).
5. Nama fungsi publik diawali kata kerja dari tabel di atas atau berupa kata benda turunan murni.

## Fase 5 — Taksonomi dokumen

`docs/plans/<topik>.md`, `docs/reports/YYYY-MM-DD-<topik>.md`, `docs/reference/`, `docs/runbooks/` (misalnya uji Muse live dan verifikasi). Satu bahasa per kelas dokumen. Tautan di README dan antar dokumen diperbarui; tes perintah terdokumentasi ikut membaca folder baru.

## Keputusan yang dibutuhkan

| Keputusan | Rekomendasi |
| --- | --- |
| Jalur playback non-streaming | Hapus; streaming satu-satunya jalur produksi |
| Migrasi `.env` | Putus bersih tanpa alias lama; `.env` lama ditolak dengan pesan key baru |
| Journal lama setelah rename event | Putus bersih; journal lama hanya data uji |
| Bahasa dokumen | Bahasa Indonesia untuk semua kelas dokumen (mayoritas saat ini, 13 banding 10) |
| Bahasa speech | Satu `speech.language` per deployment, divalidasi terhadap persona saat `join` |

## Verifikasi setiap fase

- `ruff format --check`, `ruff check`, `mypy` (termasuk `tests/fakes.py`), seluruh tes offline dan browser, tes container, rebuild image worker.
- Live: tes provider, eval replay dengan model jawaban aktif, dan probe recall pada namespace uji.
- Fase 0 menambah tes regresi untuk B1, B2, B5 sebelum perbaikannya.
- Setelah fase 5: re-ingest `acc2` dengan ekstraksi Muse, lalu acceptance ketiga di Meet.

## Di luar cakupan

Perbaikan invalidasi fakta Graphiti (dibahas setelah re-ingest), pemilihan model jawaban permanen (menunggu key Anthropic yang valid dan eval replay), dan deployment Kubernetes.

## Status implementasi

Dikerjakan di worktree `feat/explicit-config-refactor` di atas `f30d295`. Commit `89111d6` menyimpan perubahan lokal sebelumnya (jawaban dan TTS MiniMax, embedding lokal, `memory-reembed`, persiapan Claude, dan rencana ini) apa adanya.

| Fase | Commit | Isi |
| --- | --- | --- |
| 0 | `47db0a9` | Fault, kegagalan callback, polling monitor, dan keluar CLI tercatat di journal; teks percakapan tidak lagi masuk journal; percakapan dan lifecycle berbagi satu database checkpoint |
| 1 | `e92f595`, `916d1ac` | `tests/conftest.py`, `tests/fakes.py`, fixture `Settings` asli dengan env dibersihkan, fake Graphiti `create_autospec`, fixture journal, jam, halaman Meet, model chat |
| 2 | `a787023`, `cd0ad89`, `60bdf26` | Skema konfigurasi bersarang per peran dengan discriminated union, `extra="forbid"`, penolakan key lama beserta penggantinya, anggaran waktu jawaban tervalidasi, bahasa speech eksplisit, bagian `browser` terpisah |
| 3 | `151ca0a`, `69f9587` | Satu jalur TTS streaming (jalur unary dihapus), `journal.timed`, satu tipe `Fault`, dispatch argparse `set_defaults`, satu konteks memori operator, satu `open_checkpointer`, pemilik penutupan percakapan dicatat eksplisit; pengecualian API runtime dijelaskan di bawah |
| 4 | `a08e1ac`, `cf36999`, `2d62e2c` | Kosakata `Subsystem` dan `EventVerb` di `taxonomy.py`; event journal, fault, dan lifecycle diganti nama sesuai aturan; `journal.record` mewajibkan `source` bertipe; tabel kata kerja factory; paket `fakeyou.cli` ditambah `providers.py` dan `assembly.py`; nama file tes `test_<modul>[_<aspek>]` dan eval `*_eval.py` |
| 5 | `3bc8791`, `f7d16fb` | Folder `docs/plans`, `docs/reports`, `docs/reference`, `docs/runbooks` dengan tautan relatif yang dicek tes; dokumen keadaan saat ini memakai key konfigurasi baru |

Status bug audit: B1, B2, B3, B4, B5, B6 (setelan sesi dan percakapan masuk skema), B7 (`speech.language`), B8 (target preflight dengan skema dan port, termasuk memori), B9 (`extra="forbid"`), dan B10 (validator anggaran) selesai. Bug tambahan yang ditemukan saat implementasi: stream TTS yang macet melewati batas waktu tercatat sebagai timeout playback; sekarang tercatat sebagai `tts timeout`. Kegagalan consumer tercatat dua kali (`fault` dan `callback_fault`); sekarang sekali sebagai `fault_recorded`. Tes timeout recall menambal salinan konstanta `RECALL_TIMEOUT_SECONDS` yang tidak lagi dibaca kode, sehingga diam-diam menunggu budget asli 15 detik; salinan `RECALL_*` dihapus, batas pencarian menjadi `RecallSettings.search_limit`, dan pemindaian memastikan tidak ada tes lain yang menambal konstanta mati serta tidak ada field konfigurasi yang tidak dibaca kode.

Penegakan otomatis yang kini menjalankan aturan: `tests/test_taxonomy.py` (nama event, source, tanpa field isi, satuan `_ms`, keluarga alasan terminal, atribusi), `tests/test_code_rules.py` (tanpa komentar, docstring, maupun kata Bahasa Indonesia di `.py`; penamaan file tes dan eval; tabel kata kerja factory), dan `tests/test_documentation.py` (tautan relatif dan taksonomi folder dokumen). `journal.record`, `ProviderFault`, dan `Fault` menerima `source` bertipe `Subsystem`, sehingga mypy menolak source di luar kosakata.

Keputusan implementasi dan pengecualian:

- Penutupan percakapan tidak memiliki satu registrasi global: `open_conversation` menjamin percakapan berhenti sebelum checkpoint pembukanya ditutup; `run_meeting_session` menerima percakapan mandiri dan menjamin penutupan pada kegagalan checkpoint, preflight, atau startup browser; stack audio menjamin penutupan sebelum audio dan browser pada kegagalan setelah audio dibuka. Penutupan langsung saat percakapan dinonaktifkan atau sesi selesai tetap mendahului penghentian audio dan permintaan keluar Meet. Ketiga registrasi sengaja dipertahankan sebagai batas pengamanan API; `ConversationLoop.close` idempoten. Klaim sebelumnya bahwa registrasi hanya sekali dikoreksi, bukan dijadikan perubahan urutan shutdown.
- Konteks memori memiliki SDK LLM, embedding, dan reranker selain driver Graphiti. SDK Anthropic dipakai bersama reranker dan ditutup sekali. SDK didaftarkan saat dibuat agar kegagalan konstruksi berikutnya tidak membocorkan transport; pembatalan pembukaan menunggu konstruksi thread selesai sebelum menutup resource. Factory langsung `create_graphiti_memory` menerima stack pemilik opsional; pemanggil langsung tanpa stack bertanggung jawab menutup SDK dan driver sendiri.
- Engine playback memiliki iterator PCM luar sejak antrean menerima stream. Penyelesaian, kegagalan consumer, pembatalan, dan pembuangan antrean menutup iterator dengan anggaran cleanup yang sudah ada; finalizer yang tidak kooperatif tetap dibatasi anggaran tersebut.
- `voice.py` tidak dipecah menjadi `stt.py` dan `tts.py`: modul ini adalah I/O suara dua arah dan pemetaan `SPEECH_LANGUAGES` dipakai kedua sisi.
- Dedupe transkrip final tetap ada di `MeetingRecord` dan `ConversationLoop`: keduanya dipakai dan diuji terpisah, sehingga masing-masing menjaga invariannya sendiri.
- `TypeAdapter(SessionId)` tidak dijadikan `type=` argparse: fungsi command tetap memvalidasi sendiri karena juga dipanggil langsung oleh tes dan modul lain.
- Aturan kata kerja tidak memaksa semua nama fungsi publik masuk satu tabel; kata kerja domain (`forget_`, `ingest_`, `probe_`) dan kata benda turunan (`journal_path`, `episode_name`) tetap sah. Yang ditegakkan: sinonim factory dilarang, `create_` tidak async, `open_` adalah context manager.

Ukuran: `src` naik dari 6.804 menjadi 7.043 baris. Sekitar 284 baris duplikasi dan kode mati dihapus (terbesar `playback.py` −100, `voice.py` −57, `callbacks.py` −33); skema konfigurasi bertipe, peta migrasi, kosakata taksonomi, dan konstanta invarian menambah sekitar 390 baris; client Anthropic untuk memori dan preflight sadar skema/port menambah sisanya. Pemecahan `cli.py` hampir netral (1.018 menjadi 1.032 baris). Tes: 1.407 baris duplikasi dihapus dari file tes lama dan dipusatkan di `tests/fakes.py` dan `tests/conftest.py` (+831); cakupan baru menambah skema konfigurasi (+410) serta penegakan taksonomi, aturan kode, dan dokumen (+284).

Verifikasi pada HEAD branch: `ruff`, `ruff format --check`, dan `mypy` bersih; 1.356 tes offline dan browser (termasuk pemeriksaan eval offline) lulus; tes container lulus dengan image worker yang dibangun dari branch ini; 6 tes provider live lulus (jawaban dan TTS MiniMax, tiga tes Graphiti dengan ekstraksi Muse dan embedding lokal, fallback Muse). Eval replay berjalan lengkap 24 giliran dengan `recall_called` dan `no_trailing_question` 1,0; `must_mention_any` 0,833 sama dengan sebelum refactor, dan latency p95 mengikuti variasi MiniMax (permintaan mentah tanpa kode proyek 1,8–7,3 detik).

