# Breaking changes refactor total

Laporan perubahan pemutus dari rangkaian refactor (`15a0011` sampai P10).
Setiap bagian menyebut commit sumber dan cara migrasinya.

## 1. Database, katalog error, dan kontrak output CLI (`15a0011`)

- Kode keluar input kini `2` (`ExitCode.INPUT_REJECTED`), bukan `1`.
- Stdout JSON selalu indent-2 dengan `ensure_ascii=False`; hasil hanya di stdout.
- `Journal` menulis echo ke stderr; parameter `Journal(stdout=...)` menjadi `echo=`.
- Env `FAKEYOU_CHECKPOINT__*` menjadi `FAKEYOU_DATABASE__*`.
- Modul `checkpoint`, `schema`, `voiceprint_store` pindah ke paket `fakeyou.db`.

## 2. Modul dikelompokkan ke paket domain (`5155eeb`)

63 modul dipindah ke 14 paket domain. Import lama putus tanpa shim kompatibilitas.
Pemetaan lengkap:

| Path lama | Path baru |
| --- | --- |
| `fakeyou.audio` | `fakeyou.audio.audio` |
| `fakeyou.deaf` | `fakeyou.audio.deaf` |
| `fakeyou.diarize_mix` | `fakeyou.audio.diarize_mix` |
| `fakeyou.playback` | `fakeyou.audio.playback` |
| `fakeyou.probe_replay` | `fakeyou.audio.probe_replay` |
| `fakeyou.stream_capture` | `fakeyou.audio.stream_capture` |
| `fakeyou.voice` | `fakeyou.audio.voice` |
| `fakeyou.voice_activity` | `fakeyou.audio.voice_activity` |
| `fakeyou.meeting_request` | `fakeyou.config.meeting_request` |
| `fakeyou.settings` | `fakeyou.config.settings` |
| `fakeyou.agent` | `fakeyou.conversation.agent` |
| `fakeyou.conversation` | `fakeyou.conversation.conversation` |
| `fakeyou.endpointing` | `fakeyou.conversation.endpointing` |
| `fakeyou.session` | `fakeyou.conversation.session` |
| `fakeyou.debrief` | `fakeyou.debrief.debrief` |
| `fakeyou.debrief_labels.json` | `fakeyou.debrief/debrief_labels.json` |
| `fakeyou.debrief_schema` | `fakeyou.debrief.debrief_schema` |
| `fakeyou.debrief_sources` | `fakeyou.debrief.debrief_sources` |
| `fakeyou.correction_labels` | `fakeyou.diarization.correction_labels` |
| `fakeyou.diarize_client` | `fakeyou.diarization.diarize_client` |
| `fakeyou.diarize_correction` | `fakeyou.diarization.diarize_correction` |
| `fakeyou.attribution_probe` | `fakeyou.foundation.attribution_probe` |
| `fakeyou.greetings` | `fakeyou.foundation.greetings` |
| `fakeyou.invariants` | `fakeyou.foundation.invariants` |
| `fakeyou.participants` | `fakeyou.foundation.participants` |
| `fakeyou.participation` | `fakeyou.foundation.participation` |
| `fakeyou.provenance` | `fakeyou.foundation.provenance` |
| `fakeyou.taxonomy` | `fakeyou.foundation.taxonomy` |
| `fakeyou.utterance_quality` | `fakeyou.foundation.utterance_quality` |
| `fakeyou.visual_retention` | `fakeyou.foundation.visual_retention` |
| `fakeyou.identity` | `fakeyou.identity.identity` |
| `fakeyou.verify` | `fakeyou.identity.verify` |
| `fakeyou.voiceprint_embed` | `fakeyou.identity.voiceprint_embed` |
| `fakeyou.voiceprint_match` | `fakeyou.identity.voiceprint_match` |
| `fakeyou.browser` | `fakeyou.meet.browser` |
| `fakeyou.meet` | `fakeyou.meet.meet` |
| `fakeyou.meet_action_ledger` | `fakeyou.meet.meet_action_ledger` |
| `fakeyou.meet_actions` | `fakeyou.meet.meet_actions` |
| `fakeyou.participant_observer` | `fakeyou.meet.participant_observer` |
| `fakeyou.watchdog` | `fakeyou.meet.watchdog` |
| `fakeyou.ingestion` | `fakeyou.memory.ingestion` |
| `fakeyou.memory` | `fakeyou.memory.memory` |
| `fakeyou.owner_notes` | `fakeyou.memory.owner_notes` |
| `fakeyou.persona` | `fakeyou.memory.persona` |
| `fakeyou.visual_hydrate` | `fakeyou.memory.visual_hydrate` |
| `fakeyou.cleanup` | `fakeyou.observability.cleanup` |
| `fakeyou.faults` | `fakeyou.observability.faults` |
| `fakeyou.journal` | `fakeyou.observability.journal` |
| `fakeyou.provider_faults` | `fakeyou.observability.provider_faults` |
| `fakeyou.report` | `fakeyou.observability.report` |
| `fakeyou.assembly` | `fakeyou.orchestration.assembly` |
| `fakeyou.lifecycle` | `fakeyou.orchestration.lifecycle` |
| `fakeyou.preflight` | `fakeyou.orchestration.preflight` |
| `fakeyou.providers` | `fakeyou.orchestration.providers` |
| `fakeyou.runtime` | `fakeyou.orchestration.runtime` |
| `fakeyou.audio_persist` | `fakeyou.persistence.audio_persist` |
| `fakeyou.brief` | `fakeyou.persistence.brief` |
| `fakeyou.ingestion_status` | `fakeyou.persistence.ingestion_status` |
| `fakeyou.record` | `fakeyou.persistence.record` |
| `fakeyou.visual_caption` | `fakeyou.visual.visual_caption` |
| `fakeyou.visual_observer` | `fakeyou.visual.visual_observer` |
| `fakeyou.visual_store` | `fakeyou.visual.visual_store` |
| `fakeyou.worker_backend` | `fakeyou.worker.worker_backend` |

Gate `ruff`, `deptry`, dan `import-linter` berjalan di CI sejak commit ini.

## 3. Model error dan entry (`47fddc4`)

- Error katalog di tengah sesi keluar dengan kode aslinya (sebelumnya diratakan ke `1`).
- JSON `join` mendapat field `error_code`.
- String fault/detail hanya membawa nama tipe; teks lengkap hanya di journal.
- Mapping compose `EMBEDDING_DIM` dihapus; CLI mengekspornya saat entry dari `FAKEYOU_MEMORY__EMBEDDER__DIM`.
- Visual membutuhkan `FAKEYOU_VISUAL__API_KEY` sendiri.

## 4. Boolean keyword-only (`440afff`)

- Perintah `voiceprint challenge` kehilangan flag `--meeting`.
- Fungsi observer memakai argumen keyword-only, antara lain `observe_speech(*, active)`, `observe_vad(*, active)`, `observe_self_speech(*, active)`, `observe_transcript(*, finalized)`, `note_vad(*, active)`, `mark(*, active, at_ms)`, dan callback `vad(*, active)`. Penelepon posisional harus berpindah ke keyword.
- `FaultReporter._fail` menjadi `fail` (akses privat dihapus).

## 5. Potong ke typer (`692ef2d`)

- Dispatcher `argparse` diganti perintah typer yang diketik; pohon perintah dipertahankan dan diverifikasi oleh tes CLI yang dimigrasi.
- Ejaan flag diseragamkan ke `--meeting-id`; `worker status/stop/ingest` tetap memakai subjek posisional.
- Sukses dalam proses kini `SystemExit(0)`; `UsageError`, help, dan kode keluar dipertahankan.
- Format teks `--help` mengikuti typer.

## 6. Pengelompokan parameter P5 batch 3 (`d3efb96`)

- `ingest_meeting` memakai `IngestScope(group_id, meeting_id, twin_name)` sebagai pengganti keyword terpisah.
- `open_direct_page` memakai `BrowserLaunch(...)` sebagai pengganti keyword `headless` dan kawan-kawan.

## 7. Restruktur evals (P8)

- Helper keluar dari modul tes: `evals.test_trust_eval` ke `evals._trust`, `evals.test_memory_correction_eval` ke `evals._correction`, sanitizer dan `token_present` ke `evals._shared`.
- `evals/` kini paket reguler (`__init__.py`); tidak ada lagi import privat antar modul tes.
- `tests/test_conversation_benchmark.py` tetap mengimpor mesin replay dari `evals.test_replay_eval` tanpa perubahan path.

## 8. Konsistensi P9 (kompatibel)

- `diarize_run`/`diarize_show` mengembalikan `ExitCode` (`IntEnum`); perbandingan `== 0` tetap benar.
- `unpack_embedding` menerima `bytes | memoryview`; `trust_gate` menerima `set | frozenset` untuk `expected_case_ids`.
- `ConfigKey`/`COMPOSE_ONLY_KEYS` bertambah lima kunci compose; `tests/test_config.py` mengunci cakupan terhadap `compose.yaml`.
- Kode mati yang dihapus (tidak ada penelepon): `describe_poll`, `trust_source_commit`, `trust_dirty_flag`, `wait_for_census`.

## Verifikasi

- `ruff check`, `ruff format --check`, `mypy` strict (255 file), `lint-imports` (4 kontrak terjaga), `deptry` bersih.
- Suite penuh: 5806 lulus tanpa `TEST_DATABASE_URL`; 5844 lulus dengan Postgres lokal.
- Image `fakeyou-meeting-worker:local` dibangun ulang; 5 tes container lulus.
