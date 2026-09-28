# Fakeyou

Fondasi AI Digital Twin untuk menjadi peserta meeting online, dimulai dari Google Meet: mendengar, melihat, mengingat konteks dan persona, berbicara, serta berinteraksi lewat UI.

Fondasi SDK dan worker guest Google Meet sudah tersedia. Join, admission, monitor, dan leave sudah diuji live; sambungan audio dua arah dan perilaku partisipasi menjadi tahap berikutnya.

Lihat [status fondasi dan pekerjaan berikutnya](docs/project-status.md) serta [hasil refactor](docs/refactor-2026-09-28.md) untuk bukti kemampuan, batas modul, dan urutan pembangunan.

## Stack

| Peran | Library | Status fondasi |
| --- | --- | --- |
| Penalaran | LangChain 1.4.2 | Native `create_agent`, injeksi model/tools/persona |
| State percakapan | LangGraph 1.2.12 | Checkpoint SQLite; dependency PostgreSQL tersedia |
| Tangan dan mata | CloakBrowser 0.5.11 + Playwright MCP 0.0.82 | Satu sesi untuk navigasi, DOM, screenshot, input |
| Telinga | Pipecat 1.12.0 `MetaSTTService` | Muse Voice Transcribe resmi, default Bahasa Indonesia |
| Mulut | Pipecat `OpenAITTSService` | TTS terpisah dari Muse; model/voice dapat diganti |
| Memori | Graphiti 0.30.2 + Neo4j | Client dan tools dibatasi `group_id` |
| Evaluasi | DeepEval 4.2.6 + pytest | Tool trajectory deterministik tanpa judge berbayar |
| Tooling | uv, npm, Ruff, mypy | Dependency terkunci dan pemeriksaan otomatis |

`mcp<2` disengaja: adapter LangChain MCP 0.2.2 masih mengimpor FastMCP v1. Muse menggunakan dependency bawaan Pipecat; extra `pipecat-ai[mcp]` tidak diperlukan dan membutuhkan MCP v2.

## Setup

Butuh uv, Node.js 22+, dan Docker untuk Neo4j. Python 3.12 dipilih melalui `.python-version`.

```sh
uv sync --locked --all-groups
npm ci --ignore-scripts
cp -n .env.example .env
uv run fakeyou check
```

Isi `FAKEYOU_OPENAI_API_KEY` untuk model utama, Graphiti, dan TTS; `FAKEYOU_MODEL_API_KEY` untuk transkripsi Muse. Key tidak dicetak oleh command pemeriksaan, dan `.env` diabaikan Git.

```sh
uv run --env-file .env fakeyou chat "Kita menyiapkan digital twin meeting." --thread demo
uv run --env-file .env fakeyou chat "Apa konteks percakapan kita?" --thread demo
```

`--thread` mengidentifikasi percakapan checkpoint. Gunakan ID berbeda untuk konteks yang harus terpisah. Model awal `openai:gpt-4.1-mini` dapat diganti lewat `FAKEYOU_CHAT_MODEL`; provider lain memerlukan package LangChain terkait. Factory agent menerima persona khusus, tetapi profile manager belum tersedia.

## Browser

```sh
uv run --env-file .env fakeyou browser-tools
uv run --env-file .env fakeyou chat "Buka https://example.com lalu baca judulnya." --thread browser-demo --browser
```

Command pertama membuka browser, mencetak tool, lalu menutup sesi. Command kedua memberikan agent akses tindakan browser. Default headed; `FAKEYOU_HEADLESS=true` tersedia untuk smoke test. Setiap invocation menggunakan profile sementara terisolasi. Untuk meeting panjang, pertahankan context `open_browser_tools` sepanjang sesi; CLI satu turn bukan daemon meeting.

CloakBrowser mengunduh binary resmi saat dibutuhkan. Key opsional diisi lewat `FAKEYOU_CLOAKBROWSER_LICENSE_KEY`; tanpa key memakai jalur binary gratis upstream. Browser binary tidak dibundel di repository. MCP terhubung ke CDP loopback browser yang baru diluncurkan. Opsi humanize pada wrapper Python tidak otomatis berlaku pada aksi proses MCP.

Guest admission tetap mengikuti host. Worker Meet memakai halaman CloakBrowser humanized langsung; tool MCP tetap tersedia untuk agent browser. Hasil uji guest live tercatat di [rencana runtime sesi](docs/meeting-runtime-plan.md).

## Sesi Meet

```sh
uv run --env-file .env fakeyou join https://meet.google.com/xxx-yyyy-zzz --twin-name "Fakeyou Twin"
```

Command berjalan foreground sampai sesi berakhir: masuk sebagai guest, menunggu admission, memantau panggilan, lalu leave dan cleanup. Ctrl+C menghentikan startup/lobby atau meminta leave saat sudah admitted. Kegagalan leave menghasilkan `browser_failure`. Hasil akhir dicetak sebagai JSON berisi `meeting_id`, `phase`, `terminal_reason`, dan `detail`. Identitas dan batas default diambil dari `.env` (`FAKEYOU_OWNER_ID`, `FAKEYOU_TWIN_ID`, `FAKEYOU_TWIN_NAME`, `FAKEYOU_LOBBY_TIMEOUT_SECONDS`, `FAKEYOU_SESSION_DURATION_SECONDS`) dan dapat dioverride per flag. Detail desain dan uji live ada di [rencana runtime sesi](docs/meeting-runtime-plan.md).

ID sesi menerima 1–128 huruf, angka, `_`, atau `-`, dimulai dengan huruf/angka. Semua batas waktu harus positif dan finite. Checkpoint sesi yang sudah selesai dapat dibaca kembali tanpa membuka browser; checkpoint sesi aktif setelah process crash ditolak agar join tidak diulang tanpa mengetahui keadaan panggilan.

## Graphiti

```sh
docker compose up -d --wait neo4j
uv run --env-file .env fakeyou memory-init
uv run --env-file .env fakeyou chat "Ingat bahwa proyek ini bernama Fakeyou." --thread memory-demo --memory
```

`FAKEYOU_MEMORY_GROUP` membatasi ruang pengetahuan agent. Ini pemisahan data lokal, bukan autentikasi multi-tenant. `remember_meeting_fact` adalah tool fakta manual; ingestion transcript final dengan ID sumber, waktu kejadian, deduplikasi, dan koreksi belum tersedia.

Neo4j memakai named volume dan port loopback. Password contoh hanya untuk lokal. Mengubah `.env` tidak mereset password volume yang sudah terisi. `docker compose stop` mematikan layanan tanpa menghapus data.

```sh
docker compose --profile production up -d --wait postgres
```

PostgreSQL disediakan untuk worker deployment. CLI tetap memakai SQLite untuk penggunaan lokal dengan satu proses penulis; jangan berbagi satu file checkpoint di banyak proses meeting. Migrasi memakai `AsyncPostgresSaver` dan `setup()` resmi.

## Suara dan partisipasi

`voice.py` menyediakan layanan Pipecat asli melalui `create_muse_stt` dan `create_openai_tts`. Baca [komposisi suara](docs/voice.md). Tidak ada custom WebSocket Muse atau VAD.

Tahap berikutnya menyambungkan audio remote Meet ke PCM Pipecat dan TTS ke virtual microphone pada worker Linux/PulseAudio sesuai keputusan runtime, lalu memetakan voice messages ke LangGraph dan menambahkan speaker/video/participant events, turn-taking, interupsi, chat, serta raise hand. Avatar/video keluar dipilih setelah transport media stabil.

## Verifikasi

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run pytest
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_meeting_agent.py
uv run pytest tests/test_browser.py tests/test_meeting_browser.py -m browser
uv build
docker compose --profile production config --quiet
```

Tes default tidak menggunakan API berbayar atau membuka browser. Browser smoke test opt-in memakai browser asli pada halaman lokal untuk DOM, klik, dan screenshot. DeepEval menggunakan respons terprogram untuk menguji tool dan argumen, bukan kualitas penalaran model live.

CI menjalankan lint, format, type check, tes offline, evaluasi, build, dan validasi Compose. Live Muse, TTS, model, retrieval Graphiti, serta Google Meet memerlukan kredensial dan uji tersendiri.

Untuk uji suara sintetis melalui API Muse, ikuti [panduan tes live Muse](docs/live-muse-test.md). Tes ini opt-in dan memakai Model API key terpisah dari login OAuth Muse Code.

## Dokumen

- [Arsitektur](docs/architecture.md)
- [Kemampuan Digital Twin](docs/capabilities.md)
- [Lesson learned Amazon LMA, Attendee, Vexa](docs/reference-lessons.md)
- [Sumber dokumentasi library](docs/library-sources.md)
- [Rencana fondasi](docs/implementation-plan.md)
- [Rencana runtime sesi dan guest Meet](docs/meeting-runtime-plan.md)
