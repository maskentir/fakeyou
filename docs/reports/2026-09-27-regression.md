# Laporan regresi: fondasi Digital Twin

**Tanggal** 2026-09-27 · **Versi** 0.1.0 · **Cakupan** pemeriksaan lokal static, unit, evaluasi, browser smoke, Muse STT live, packaging, dan Compose

## Ringkasan

| Pemeriksaan | Hasil | Cakupan |
| --- | --- | --- |
| `uv run ruff check .` | LULUS | 0 error lint |
| `uv run ruff format --check .` | LULUS | 25 file sudah terformat |
| `uv run mypy` | LULUS | 7 file source, 0 masalah |
| `DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run pytest` | LULUS | 19 lulus, browser dan Muse live deselected |
| `DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_meeting_agent_eval.py` | LULUS | 1 lulus, pass rate 100% |
| `uv run pytest tests/test_browser.py -m browser` | LULUS | 1 lulus, 1 tes non-browser deselected |
| `uv lock --check` | LULUS | Lockfile sudah mutakhir |
| `uv pip check` | LULUS | Paket terpasang saling kompatibel |
| `uv build` | LULUS | Source distribution dan wheel berhasil dibangun |
| `docker compose --profile production config --quiet` | LULUS | Konfigurasi Compose valid |
| Kesehatan service Compose yang sudah ada | LULUS | `fakeyou-neo4j-1` healthy; port hanya loopback |
| Lifecycle startup Graphiti | LULUS | Konstruksi worker, kesiapan schema, konektivitas lokal, dan cleanup |
| Lifecycle Pipecat native | LULUS | Aliran frame audio, transkrip final, shutdown, dan penolakan error upstream tanpa panggilan provider |
| Tes Muse live dengan audio sintetis berbahasa Indonesia | LULUS | Fixture PCM 9,076 detik dialirkan secara real-time; transkripsi final cocok |

## Temuan

### Diperbaiki: Tool Graphiti dapat berjalan sebelum inisialisasi schema

`chat --memory` kini membangun Graphiti di luar event loop yang aktif dan menunggu setup schema selesai sebelum mengekspos tool. Tes regresi mencakup urutan kesiapan dan cleanup setelah setup gagal. Smoke check Neo4j lokal menjalankan jalur konstruksi, setup, konektivitas, dan penutupan native dengan key model placeholder offline.

### Keterbatasan: concurrent writer SQLite

Probe stress terpisah yang membuka 32 koneksi saver SQLite native terhadap file sementara yang baru menghasilkan 5 kegagalan `database is locked`. CLI tetap menjadi antarmuka pengembangan lokal dengan single writer. Backend PostgreSQL yang sudah dikonfigurasi masih memerlukan integrasi CLI sebelum ada worker meeting yang berjalan bersamaan. SQLite memakai timeout lock bawaan driver; tidak ada koordinasi penulisan di level proyek.

### [LOW] Peringatan deprecation pihak ketiga selama pytest

- **Lokasi** dependency terpasang (`graphiti_core`, `pipecat`)
- **Detail** Pytest melaporkan tiga peringatan: deprecation config berbasis class Pydantic di Graphiti, deprecation `audioop` Python, dan deprecation `AudioContextTTSService` Pipecat.
- **Dampak** Tidak ada kegagalan tes saat ini; upgrade dependency atau Python di masa depan mungkin memerlukan pembaruan kompatibilitas.
- **Keyakinan** Tinggi

## Celah cakupan dan keterbatasan

- Pemeriksaan Muse live memakai kumpulan kredensial lokal dan melakukan satu panggilan STT eksternal singkat. Pemeriksaan lain tidak memakai kredensial provider dan tidak melakukan panggilan eksternal ke model, TTS, retrieval Graphiti, maupun Google Meet.
- DeepEval memvalidasi tool trajectory `raise_hand` yang deterministik dengan metrik offline-nya; DeepEval tidak menilai penalaran model live.
- Tes browser hanya menjalankan jalur browser/MCP lokal dan sebuah halaman lokal. Tes ini tidak membuktikan admission guest, media Meet, atau perilaku meeting yang berjalan lama.
- Tidak ada persentase coverage yang dikumpulkan; regresi ini berfokus pada perilaku lulus/gagal dan jalur smoke yang dikonfigurasi.
