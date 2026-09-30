# Fakeyou

Fondasi AI Digital Twin untuk menjadi peserta meeting online, dimulai dari Google Meet: mendengar, melihat, mengingat konteks dan persona, berbicara, serta berinteraksi lewat UI.

Fondasi SDK, worker guest Google Meet, dan audio dua arah sudah tersedia. Loop percakapan opt-in, profil persona tervalidasi, dan recall Graphiti per owner/twin sudah diuji lokal dan dengan provider live; acceptance di Google Meet serta perilaku partisipasi menjadi tahap berikutnya.

Lihat [status fondasi dan pekerjaan berikutnya](docs/project-status.md) serta [hasil refactor](docs/refactor-2026-09-28.md) untuk bukti kemampuan, batas modul, dan urutan pembangunan.

## Stack

| Peran | Library | Status fondasi |
| --- | --- | --- |
| Penalaran | LangChain 1.4.2 | Native `create_agent`, injeksi model/tools/persona |
| State percakapan | LangGraph 1.2.12 | Checkpoint SQLite; dependency PostgreSQL tersedia |
| Tangan dan mata | CloakBrowser 0.5.11 + Playwright MCP 0.0.82 | Satu sesi untuk navigasi, DOM, screenshot, input |
| Telinga | Pipecat 1.12.0 `MetaSTTService` | Muse Voice Transcribe resmi, default Bahasa Indonesia |
| Mulut | `OpenAISpeechSynthesizer` + `PlaybackEngine` | Sintesis unary PCM + `paplay` serial; model/voice dapat diganti |
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

`--thread` mengidentifikasi percakapan checkpoint. Gunakan ID berbeda untuk konteks yang harus terpisah. Model awal `openai:gpt-4.1-mini` dapat diganti lewat `FAKEYOU_CHAT_MODEL`; provider lain memerlukan package LangChain terkait. `chat` memakai persona default; profil pemilik tersedia untuk `join` lewat `--persona`.

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

`FAKEYOU_MEMORY_GROUP` membatasi ruang pengetahuan `chat --memory`. Sesi `join --memory` tidak memakai setting itu: grupnya diturunkan dari `owner_id` dan `twin_id` profil persona. Keduanya pemisahan data lokal, bukan autentikasi multi-tenant. `remember_meeting_fact` adalah tool fakta manual yang hanya tersedia di `chat`. Ingestion transkrip meeting, catatan pemilik, dan penghapusan manual dijelaskan di bagian persona.

Neo4j memakai named volume dan port loopback. Password contoh hanya untuk lokal. Mengubah `.env` tidak mereset password volume yang sudah terisi. `docker compose stop` mematikan layanan tanpa menghapus data.

```sh
docker compose --profile production up -d --wait postgres
```

PostgreSQL disediakan untuk worker deployment. CLI tetap memakai SQLite untuk penggunaan lokal dengan satu proses penulis; jangan berbagi satu file checkpoint di banyak proses meeting. Migrasi memakai `AsyncPostgresSaver` dan `setup()` resmi.

## Suara dan partisipasi

`voice.py` menyediakan STT Pipecat asli melalui `create_muse_stt` dan sintesis unary OpenAI melalui `OpenAISpeechSynthesizer`; `playback.py` memutar hasilnya lewat `paplay`. Baca [komposisi suara](docs/voice.md). Tidak ada custom WebSocket Muse atau VAD.

Aktifkan respons agent dengan `--audio --converse` pada worker Linux. `--audio` saja mempertahankan mode transkripsi dan uji `--speak`. Dengan `--audio`, pipeline audio dan STT dibuka sebelum twin meminta masuk, sehingga twin sudah mendengar saat di-admit; mikrofon, ucapan awal, dan jawaban baru aktif setelah admission. Gagal membuka audio mengakhiri sesi sebagai `infra_failure` tanpa mengetuk meeting. Lihat [kontrak conversation dan batas replay](docs/conversation-plan.md) dan [hasil refactor](docs/conversation-refactor-2026-09-28.md).

## Persona pemilik

```sh
docker compose up -d --wait neo4j
docker compose run --rm meeting-worker fakeyou join https://meet.google.com/xxx-yyyy-zzz \
  --owner-id demo_owner --twin-id demo_twin \
  --audio --converse --persona examples/persona.example.json --memory
```

Worker membaca `examples/` secara read-only dan menghubungi Neo4j lewat service Compose `bolt://neo4j:7687`; override dengan `FAKEYOU_WORKER_NEO4J_URI` bila perlu. Simpan profil pribadi di `.runtime/audio/persona.json` (diabaikan Git) lalu pakai `--persona .runtime/persona.json` di dalam worker.

`--persona` memerlukan `--converse`; `--memory` pada `join` memerlukan `--persona` yang mengizinkan `recall_meeting_context` atau `ingest_transcripts`. `owner_id` dan `twin_id` profil harus sama dengan identitas sesi, dan profil ditolak sebelum model, database, browser, atau audio dibuka. Tanpa `--twin-name`, nama di Meet diambil dari `display_name` profil.

Profil adalah konfigurasi operator dengan schema ketat: `owner_id`, `twin_id`, `display_name`, `role`, `language` (`id` atau `en`), `speaking_style`, `goals`, `verified_facts`, `allowed_tools`, `ingest_transcripts`, dan `vocabulary`. Field asing, teks kosong, lebih dari 20 item, teks di atas 500 karakter, istilah vocabulary lebih dari 30 atau di atas 64 karakter, atau tool selain `recall_meeting_context` ditolak. `vocabulary` berisi nama dan istilah penting; bersama nama twin, daftar ini dikirim sebagai keyword STT Muse dan dipakai instruksi ekstraksi memori untuk menormalkan ejaan. [Contoh sintetis](examples/persona.example.json) dapat langsung dicoba; jangan menyimpan profil pribadi di repository.

Ruang memori adalah `twin_` + UUID5 dari `[owner_id, twin_id]`: stabil lintas meeting dan tidak berubah saat nama diganti. Checkpoint percakapan tetap per owner/twin/meeting. Koneksi Neo4j diverifikasi sebelum model dan browser dibuka; kegagalannya berakhir sebagai `infra_failure` dengan exit code 3. Recall hanya mengembalikan fakta yang masih berlaku, masing-masing dengan sumber dan waktu UTC. Recall yang gagal atau melewati 15 detik dikembalikan ke model sebagai informasi yang belum diketahui, dicatat sebagai fault `memory`, dan tidak menghentikan capture.

Dengan `ingest_transcripts: true`, memori diturunkan dari catatan meeting setelah twin leave: transkrip final dan ucapan twin yang selesai diputar disusun menjadi jendela lintas speaker (20 ucapan atau 60 detik) dan disimpan sebagai episode Graphiti `<meeting_id>:w<index>` dalam saga `meeting:<meeting_id>`. Setiap fakta menyebut speaker-nya (`Digital twin`, `Unidentified participant`, atau nama yang diketahui), dan pertanyaan tidak disimpan sebagai fakta. Selama meeting, twin memakai thread percakapan; Graphiti dipakai untuk meeting sebelumnya dan catatan pemilik. Model tidak memiliki tool tulis. Tidak ada persetujuan peserta di aplikasi dan tidak ada retensi otomatis; kepatuhan aturan rekaman menjadi tanggung jawab operator.

Ingestion dapat dilanjutkan: episode terakhir di saga menjadi penanda, sehingga menjalankan ulang tidak menggandakan episode. Stop saat ingestion menghentikannya di antara jendela; sisanya dilanjutkan dengan `memory-ingest`. Untuk memproses ulang meeting setelah instruksi ekstraksi berubah, hapus episodenya dengan `--keep-record` lalu ingest lagi. `memory-forget --meeting` tanpa `--keep-record` menghapus episode, catatan meeting, journal, dan thread percakapan meeting itu.

```sh
docker compose run --rm meeting-worker fakeyou memory-ingest --meeting MEETING_ID \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
docker compose run --rm meeting-worker fakeyou memory-add "Update dari Rani: demo Orchid dipindah ke hari Jumat." \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
docker compose run --rm meeting-worker fakeyou memory-forget --meeting MEETING_ID --keep-record \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
docker compose run --rm meeting-worker fakeyou memory-forget --meeting MEETING_ID \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
docker compose run --rm meeting-worker fakeyou memory-forget --episode EPISODE_UUID \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
```

Detail keputusan dan bukti ada di [rencana persona dan memori](docs/persona-memory-plan.md), [ingestion memori](docs/memory-ingestion-plan.md), dan [rencana jangka panjang](docs/long-term-plan.md).

Audio remote Meet sudah masuk ke Pipecat melalui PulseAudio, dan ucapan tersintesis keluar lewat `paplay` ke virtual microphone. Pekerjaan berikutnya adalah acceptance persona dan memori di meeting live, lalu speaker/video/participant events, turn-taking, chat, serta raise hand.

## Model dan laporan sesi

Model dipilih per peran lewat `Settings`, bukan dari default library. `fakeyou check` mencetak semuanya di bagian `models`:

| Peran | Setelan | Default |
| --- | --- | --- |
| Jawaban lisan | `FAKEYOU_CHAT_MODEL`, `FAKEYOU_CHAT_BASE_URL`, `FAKEYOU_CHAT_API_KEY`, `FAKEYOU_CHAT_REASONING_EFFORT` | `openai:gpt-4.1-mini` dengan key OpenAI |
| Cadangan jawaban lisan | `FAKEYOU_FALLBACK_CHAT_MODEL`, `FAKEYOU_FALLBACK_CHAT_BASE_URL`, `FAKEYOU_FALLBACK_CHAT_API_KEY`, `FAKEYOU_FALLBACK_CHAT_REASONING_EFFORT` | Tidak aktif |
| Memori Graphiti | `FAKEYOU_MEMORY_LLM_MODEL`, `FAKEYOU_MEMORY_SMALL_MODEL`, `FAKEYOU_MEMORY_EMBEDDING_MODEL` | `gpt-5.5`, `gpt-4.1-nano`, `text-embedding-3-small` |
| STT | `FAKEYOU_STT_MODEL` | `muse-voice-transcribe-1.0` |

Setiap panggilan model jawaban dibatasi 20 detik agar dua panggilan plus recall muat dalam deadline percakapan 60 detik. Bila cadangan diatur, model utama tidak di-retry; `ModelFallbackMiddleware` LangChain langsung memindahkan panggilan ke model cadangan (terukur 7,5 detik untuk jawaban dengan tool call saat provider utama mati, memakai Muse Spark 1.2 `reasoning_effort=minimal`). Host cadangan tidak ikut preflight agar cadangan yang mati tidak memblokir meeting.

Model yang kompatibel OpenAI dapat dipakai lewat `FAKEYOU_CHAT_BASE_URL`, misalnya Muse Spark di `https://api.meta.ai/v1` dengan `FAKEYOU_CHAT_MODEL=openai:muse-spark-1.3`. Host preflight mengikuti endpoint itu. Perbandingan latency dan perilaku antarmodel ada di [rencana jangka panjang](docs/long-term-plan.md#4a-model-per-peran-lewat-langchain); mengganti model lisan harus lolos eval yang sama.

Setiap `join` menulis dua berkas:

- Journal `.runtime/journals/<meeting_id>.jsonl` (worker: `.runtime/audio/journals/`): metadata bertimestamp untuk startup (audio siap, join, mikrofon, conversation), VAD, transkrip, transisi, agent, tool, TTS, playback, ingestion, fault, dan hasil sesi. Isi sensitif seperti prompt dan body di-redact.
- Catatan meeting `.runtime/meetings/<meeting_id>/record.jsonl`: transkrip final, ucapan twin yang selesai diputar, dan hasil tool secara lengkap. Catatan disimpan sampai dihapus manual.

```sh
uv run fakeyou report --meeting MEETING_ID --journal-dir .runtime/audio/journals
```

Laporan berisi startup (lama membuka audio, lama di lobby, admission → mikrofon dan → conversation aktif, audio siap saat admission), latency per giliran (VAD stop → transkrip final → jawaban → PCM pertama) beserta p50/p95/maks, jawaban terucap/terpotong/berakhir pertanyaan, interupsi per pemicu dan latency VAD mulai bicara → playback berhenti, pemakaian tool, ingestion, fault, dan hasil sesi.

Dengan `--barge-in`, transkrip interim minimal tiga kata menghentikan twin yang sedang berbicara tanpa menunggu transkrip final; transkrip final yang lebih pendek tetap memotong playback.

## Verifikasi

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run pytest
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_meeting_agent.py
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_persona.py
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_replay.py
uv run pytest tests/test_browser.py tests/test_meeting_browser.py -m browser
uv run pytest tests/test_worker_image.py -m container
uv build
docker compose --profile production config --quiet
```

Tes default tidak menggunakan API berbayar atau membuka browser. Browser smoke test opt-in memakai browser asli pada halaman lokal untuk DOM, klik, dan screenshot. DeepEval default menggunakan respons terprogram untuk menguji wiring prompt, tool, dan argumen, bukan kualitas penalaran model live. Penilaian kualitas persona dengan model dan judge OpenAI dijalankan terpisah:

```sh
uv run --env-file .env deepeval test run evals/test_persona.py -m live
uv run --env-file .env pytest evals/test_replay.py -m live -s
uv run --env-file .env pytest tests/test_providers_live.py -m live
```

`evals/test_replay.py` memutar transkrip STT nyata dari sesi `acc-0930-s1`/`s2` ke model yang sedang dikonfigurasi dalam satu thread per sesi, tiga kali, lalu menghitung pass rate fakta wajib/terlarang, recall, batas dua kalimat, pertanyaan penutup, dan latency p95. Mengganti model jawaban lisan harus lolos replay ini.

CI menjalankan lint, format, type check, tes offline, evaluasi, build, dan validasi Compose. Live Muse, TTS, model, retrieval Graphiti, serta Google Meet memerlukan kredensial dan uji tersendiri.

Untuk uji suara sintetis melalui API Muse, ikuti [panduan tes live Muse](docs/live-muse-test.md). Tes ini opt-in dan memakai Model API key terpisah dari login OAuth Muse Code.

## Dokumen

- [Arsitektur](docs/architecture.md)
- [Kemampuan Digital Twin](docs/capabilities.md)
- [Lesson learned Amazon LMA, Attendee, Vexa](docs/reference-lessons.md)
- [Sumber dokumentasi library](docs/library-sources.md)
- [Rencana fondasi](docs/implementation-plan.md)
- [Rencana runtime sesi dan guest Meet](docs/meeting-runtime-plan.md)
- [Persona pemilik dan retrieval memori](docs/persona-memory-plan.md)
- [Ingestion memori dengan provenance](docs/memory-ingestion-plan.md)
- [Acceptance Meet 30 September dan rencana perbaikan](docs/acceptance-2026-09-30.md)
- [Rencana jangka panjang memori, suara, dan bukti kualitas](docs/long-term-plan.md)
