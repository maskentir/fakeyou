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

Isi key masing-masing peran aktif: `FAKEYOU_REPLY__API_KEY`, `FAKEYOU_TTS__API_KEY`, `FAKEYOU_STT__API_KEY`, `FAKEYOU_MEMORY__LLM__API_KEY`, dan `FAKEYOU_MEMORY__EMBEDDER__API_KEY`. Tidak ada peminjaman key antarperan. `check`, help, browser, dan laporan dapat digunakan tanpa kredensial provider. `.env` diabaikan Git; `check` menyamarkan secret.

```sh
uv run --env-file .env fakeyou chat "Kita menyiapkan digital twin meeting." --thread demo
uv run --env-file .env fakeyou chat "Apa konteks percakapan kita?" --thread demo
```

`--thread` mengidentifikasi percakapan checkpoint. Gunakan ID berbeda untuk konteks yang harus terpisah. Model awal `gpt-4.1-mini` dapat diganti lewat `FAKEYOU_REPLY__MODEL`; pilih provider secara eksplisit lewat `FAKEYOU_REPLY__PROVIDER`. Nama model tidak memakai prefix provider. `chat` memakai persona default; profil pemilik tersedia untuk `join` lewat `--persona`.

## Browser

```sh
uv run --env-file .env fakeyou browser-tools
uv run --env-file .env fakeyou chat "Buka https://example.com lalu baca judulnya." --thread browser-demo --browser
```

Command pertama membuka browser, mencetak tool, lalu menutup sesi. Command kedua memberikan agent akses tindakan browser. Default headed; `FAKEYOU_MEETING__HEADLESS=true` tersedia untuk smoke test. Setiap invocation menggunakan profile sementara terisolasi. Untuk meeting panjang, pertahankan context `open_browser_tools` sepanjang sesi; CLI satu turn bukan daemon meeting.

CloakBrowser mengunduh binary resmi saat dibutuhkan. Key opsional diisi lewat `FAKEYOU_MEETING__LICENSE_KEY`; tanpa key memakai jalur binary gratis upstream. Browser binary tidak dibundel di repository. MCP terhubung ke CDP loopback browser yang baru diluncurkan. Opsi humanize pada wrapper Python tidak otomatis berlaku pada aksi proses MCP.

Guest admission tetap mengikuti host. Worker Meet memakai halaman CloakBrowser humanized langsung; tool MCP tetap tersedia untuk agent browser. Hasil uji guest live tercatat di [rencana runtime sesi](docs/meeting-runtime-plan.md).

## Sesi Meet

```sh
uv run --env-file .env fakeyou join https://meet.google.com/xxx-yyyy-zzz --twin-name "Fakeyou Twin"
```

Command berjalan foreground sampai sesi berakhir: masuk sebagai guest, menunggu admission, memantau panggilan, lalu leave dan cleanup. Ctrl+C menghentikan startup/lobby atau meminta leave saat sudah admitted. Kegagalan leave menghasilkan `browser_failure`. Hasil akhir dicetak sebagai JSON berisi `meeting_id`, `phase`, `terminal_reason`, dan `detail`. Identitas dan batas default diambil dari `.env` (`FAKEYOU_IDENTITY__OWNER_ID`, `FAKEYOU_IDENTITY__TWIN_ID`, `FAKEYOU_IDENTITY__TWIN_NAME`, `FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS`, `FAKEYOU_MEETING__SESSION_DURATION_SECONDS`) dan dapat dioverride per flag. Detail desain dan uji live ada di [rencana runtime sesi](docs/meeting-runtime-plan.md).

ID sesi menerima 1–128 huruf, angka, `_`, atau `-`, dimulai dengan huruf/angka. Semua batas waktu harus positif dan finite. Checkpoint sesi yang sudah selesai dapat dibaca kembali tanpa membuka browser; checkpoint sesi aktif setelah process crash ditolak agar join tidak diulang tanpa mengetahui keadaan panggilan.

## Graphiti

```sh
docker compose up -d --wait neo4j
uv run --env-file .env fakeyou memory-init
uv run --env-file .env fakeyou chat "Ingat bahwa proyek ini bernama Fakeyou." --thread memory-demo --memory
```

`FAKEYOU_MEMORY__STORE__GROUP` membatasi ruang pengetahuan `chat --memory`. Sesi `join --memory` tidak memakai setting itu: grupnya diturunkan dari `owner_id` dan `twin_id` profil persona. Keduanya pemisahan data lokal, bukan autentikasi multi-tenant. `remember_meeting_fact` adalah tool fakta manual yang hanya tersedia di `chat`. Ingestion transkrip meeting, catatan pemilik, dan penghapusan manual dijelaskan di bagian persona.

Neo4j memakai named volume dan port loopback. Password contoh hanya untuk lokal. Mengubah `.env` tidak mereset password volume yang sudah terisi. `docker compose stop` mematikan layanan tanpa menghapus data.

Model Graphiti dipilih per peran. Default memakai OpenAI (`FAKEYOU_MEMORY__LLM__MODEL`, `FAKEYOU_MEMORY__LLM__SMALL_MODEL`, `FAKEYOU_MEMORY__EMBEDDER__MODEL`). Ekstraksi memilih kelas client lewat `FAKEYOU_MEMORY__LLM__CLIENT` (`openai`, `openai_generic`, atau `anthropic`). Untuk endpoint kompatibel OpenAI pilih `openai_generic`, lalu atur `FAKEYOU_MEMORY__LLM__BASE_URL` dan `FAKEYOU_MEMORY__LLM__API_KEY` (`OpenAIGenericClient` dengan `json_schema`), misalnya Muse Spark 1.2. Embedding dapat berjalan lokal dengan model open-source lewat service `embeddings` (Text Embeddings Inference, `ibm-granite/granite-embedding-97m-multilingual-r2` dengan revisi dipin):

```sh
docker compose --profile embeddings up -d --wait embeddings
uv run --env-file .env fakeyou memory-reembed --persona examples/persona.example.json \
  --owner-id demo_owner --twin-id demo_twin
docker compose run --rm meeting-worker fakeyou memory-reembed \
  --persona examples/persona.example.json --owner-id demo_owner --twin-id demo_twin
```

Setel `FAKEYOU_MEMORY__EMBEDDER__BASE_URL=http://127.0.0.1:8080/v1`, `FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL=http://embeddings/v1`, `FAKEYOU_MEMORY__EMBEDDER__API_KEY=local`, dan `FAKEYOU_MEMORY__EMBEDDER__DIM=384`. Tambahkan `EMBEDDING_DIM=384` dalam `.env`, lalu jalankan dengan `uv run --env-file .env` supaya Graphiti membaca dimensi sebelum import. Worker Compose mengekspor dimensi yang sama. Perubahan dimensi setelah Graphiti di-import ditolak saat memori dibuka. Vektor dari model embedding lain tidak sebanding, jadi setiap kali model embedding berganti jalankan `memory-reembed` untuk tiap persona; perintah ini menghitung ulang embedding entity dan fakta tanpa menghapus episode atau owner note. Service `embeddings` harus berjalan sebelum `join --memory` karena recall membutuhkan embedding query. Image default untuk arm64; di x86_64 setel `FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`.

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

Worker membaca `examples/` secara read-only dan menghubungi Neo4j lewat service Compose `bolt://neo4j:7687`; override dengan `FAKEYOU_COMPOSE_WORKER_NEO4J_URI` bila perlu. Simpan profil pribadi di `.runtime/audio/persona.json` (diabaikan Git) lalu pakai `--persona .runtime/persona.json` di dalam worker.

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

Konfigurasi menggunakan bagian bertipe dan nested env dengan pemisah `__`. `fakeyou check` mencetak seluruh `settings.model_dump(mode="json")`, termasuk default dan secret tersamarkan:

| Peran | Setelan | Default |
| --- | --- | --- |
| Jawaban lisan | `FAKEYOU_REPLY__MODEL`, `FAKEYOU_REPLY__BASE_URL`, `FAKEYOU_REPLY__API_KEY`, `FAKEYOU_REPLY__REASONING_EFFORT` | `gpt-4.1-mini`, provider `openai`, key peran sendiri |
| Cadangan jawaban lisan | `FAKEYOU_REPLY_FALLBACK__MODEL`, `FAKEYOU_REPLY_FALLBACK__BASE_URL`, `FAKEYOU_REPLY_FALLBACK__API_KEY`, `FAKEYOU_REPLY_FALLBACK__REASONING_EFFORT` | Tidak aktif |
| Memori Graphiti | `FAKEYOU_MEMORY__LLM__MODEL`, `FAKEYOU_MEMORY__LLM__SMALL_MODEL`, `FAKEYOU_MEMORY__EMBEDDER__MODEL` | `gpt-5.5`, `gpt-4.1-nano`, `text-embedding-3-small` |
| STT | `FAKEYOU_STT__MODEL` | `muse-voice-transcribe-1.0` |

Anggaran waktu diperiksa saat konfigurasi dibaca: jumlah `timeout_seconds × (max_retries + 1)` untuk jawaban utama dan cadangan, ditambah timeout recall, harus muat dalam `FAKEYOU_CONVERSATION__RESPONSE_TIMEOUT_SECONDS`. Default utama 15 detik × 3 percobaan dan recall 15 detik, total 60 detik. Retry utama dan cadangan selalu berasal dari konfigurasi; bila mengaktifkan cadangan, kurangi retry atau sesuaikan deadline secara eksplisit. Cadangan memakai API key sendiri. Preflight memeriksa URL peran aktif, termasuk cadangan dan memori, dengan skema dan port utuh.

Provider jawaban adalah `openai`, `openai_compatible`, atau `anthropic`. Contoh MiniMax: `FAKEYOU_REPLY__PROVIDER=openai_compatible`, `FAKEYOU_REPLY__MODEL=MiniMax-M3`, dan `FAKEYOU_REPLY__BASE_URL=https://api.minimax.io/v1`. `openai_compatible` wajib memiliki URL. Field khusus provider hanya tersedia pada variannya; `extra_body` hanya untuk `openai_compatible`. Kutip JSON agar dibaca konsisten oleh dotenv dan `uv --env-file`: `FAKEYOU_REPLY__EXTRA_BODY='{"reasoning_split": true}'`.

TTS memiliki `provider`, `model`, `voice`, `api_key`, `base_url`, dan `sample_rate` yang terlihat di `check`. Pilih `FAKEYOU_TTS__PROVIDER=minimax` untuk default `speech-2.8-turbo` dan `Indonesian_CalmWoman`. Voice OpenAI divalidasi terhadap daftar library. Format audio tetap PCM s16le mono, capture 16 kHz, playback 24 kHz; sample rate TTS selain 24 kHz ditolak.

`FAKEYOU_SPEECH__LANGUAGE=id` atau `en` mengatur STT, pemisahan kalimat, instruksi TTS, dan language boost. Bahasa profil persona harus sama; `join` menolak ketidaksesuaian sebelum membuka resource. Kontrol browser (`headless`, `warmup_browser`, `monitor_poll_seconds`), percakapan, ingestion, kesehatan audio, dan preflight ada pada bagian masing-masing dalam `.env.example`.

### Migrasi konfigurasi lama

Key `FAKEYOU_*` yang tidak dikenal ditolak, termasuk typo bernilai kosong dalam dotenv maupun environment proses, tanpa membedakan kapitalisasi. Hanya `FAKEYOU_COMPOSE_*` yang dikecualikan untuk interpolasi Compose. Variabel library seperti `EMBEDDING_DIM` dan `LANGSMITH_*` tetap diperbolehkan. Simpan salinan `.env` yang diabaikan Git sebelum migrasi; jangan mengaktifkan provider hanya karena key lama tersedia. Key bersama lama harus dipindahkan secara sengaja ke setiap peran yang memang menggunakannya.

| Key lama | Pengganti |
| --- | --- |
| `FAKEYOU_CHAT_MODEL` | `FAKEYOU_REPLY__MODEL` |
| `FAKEYOU_CHAT_BASE_URL` | `FAKEYOU_REPLY__BASE_URL` |
| `FAKEYOU_CHAT_API_KEY` | `FAKEYOU_REPLY__API_KEY` |
| `FAKEYOU_CHAT_REASONING_EFFORT` | `FAKEYOU_REPLY__REASONING_EFFORT` |
| `FAKEYOU_CHAT_EXTRA_BODY` | `FAKEYOU_REPLY__EXTRA_BODY` |
| `FAKEYOU_FALLBACK_CHAT_MODEL` | `FAKEYOU_REPLY_FALLBACK__MODEL` |
| `FAKEYOU_FALLBACK_CHAT_BASE_URL` | `FAKEYOU_REPLY_FALLBACK__BASE_URL` |
| `FAKEYOU_FALLBACK_CHAT_API_KEY` | `FAKEYOU_REPLY_FALLBACK__API_KEY` |
| `FAKEYOU_FALLBACK_CHAT_REASONING_EFFORT` | `FAKEYOU_REPLY_FALLBACK__REASONING_EFFORT` |
| `FAKEYOU_FALLBACK_CHAT_EXTRA_BODY` | `FAKEYOU_REPLY_FALLBACK__EXTRA_BODY` |
| `FAKEYOU_MEMORY_LLM_MODEL` | `FAKEYOU_MEMORY__LLM__MODEL` |
| `FAKEYOU_MEMORY_SMALL_MODEL` | `FAKEYOU_MEMORY__LLM__SMALL_MODEL` |
| `FAKEYOU_MEMORY_LLM_BASE_URL` | `FAKEYOU_MEMORY__LLM__BASE_URL` |
| `FAKEYOU_MEMORY_LLM_API_KEY` | `FAKEYOU_MEMORY__LLM__API_KEY` |
| `FAKEYOU_MEMORY_EMBEDDING_MODEL` | `FAKEYOU_MEMORY__EMBEDDER__MODEL` |
| `FAKEYOU_MEMORY_EMBEDDING_BASE_URL` | `FAKEYOU_MEMORY__EMBEDDER__BASE_URL` |
| `FAKEYOU_MEMORY_EMBEDDING_API_KEY` | `FAKEYOU_MEMORY__EMBEDDER__API_KEY` |
| `FAKEYOU_MEMORY_EMBEDDING_DIM` | `FAKEYOU_MEMORY__EMBEDDER__DIM` |
| `FAKEYOU_STT_MODEL` | `FAKEYOU_STT__MODEL` |
| `FAKEYOU_TTS_PROVIDER` | `FAKEYOU_TTS__PROVIDER` |
| `FAKEYOU_TTS_API_KEY` | `FAKEYOU_TTS__API_KEY` |
| `FAKEYOU_TTS_VOICE` | `FAKEYOU_TTS__VOICE` |
| `FAKEYOU_CHECKPOINT_PATH` | `FAKEYOU_PATHS__CHECKPOINT_PATH` |
| `FAKEYOU_HEADLESS` | `FAKEYOU_MEETING__HEADLESS` |
| `FAKEYOU_CLOAKBROWSER_LICENSE_KEY` | `FAKEYOU_MEETING__LICENSE_KEY` |
| `FAKEYOU_OPENAI_API_KEY` | `FAKEYOU_REPLY__API_KEY` |
| `FAKEYOU_MODEL_API_KEY` | `FAKEYOU_STT__API_KEY` |
| `FAKEYOU_NEO4J_URI` | `FAKEYOU_MEMORY__STORE__URI` |
| `FAKEYOU_NEO4J_USER` | `FAKEYOU_MEMORY__STORE__USER` |
| `FAKEYOU_NEO4J_PASSWORD` | `FAKEYOU_MEMORY__STORE__PASSWORD` |
| `FAKEYOU_MEMORY_GROUP` | `FAKEYOU_MEMORY__STORE__GROUP` |
| `FAKEYOU_OWNER_ID` | `FAKEYOU_IDENTITY__OWNER_ID` |
| `FAKEYOU_TWIN_ID` | `FAKEYOU_IDENTITY__TWIN_ID` |
| `FAKEYOU_TWIN_NAME` | `FAKEYOU_IDENTITY__TWIN_NAME` |
| `FAKEYOU_LOBBY_TIMEOUT_SECONDS` | `FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS` |
| `FAKEYOU_SESSION_DURATION_SECONDS` | `FAKEYOU_MEETING__SESSION_DURATION_SECONDS` |
| `FAKEYOU_JOURNAL_DIR` | `FAKEYOU_PATHS__JOURNAL_DIR` |
| `FAKEYOU_MEETING_RECORD_DIR` | `FAKEYOU_PATHS__RECORD_DIR` |
| `FAKEYOU_ANTHROPIC_API_KEY` | `FAKEYOU_REPLY__API_KEY (select provider explicitly)` |
| `FAKEYOU_AUDIO` | `FAKEYOU_WORKER__AUDIO` |
| `FAKEYOU_WORKER_NEO4J_URI` | `FAKEYOU_COMPOSE_WORKER_NEO4J_URI` |
| `FAKEYOU_WORKER_EMBEDDING_BASE_URL` | `FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL` |
| `FAKEYOU_EMBEDDINGS_IMAGE` | `FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE` |
| `FAKEYOU_EMBEDDINGS_MODEL_ID` | `FAKEYOU_COMPOSE_EMBEDDINGS_MODEL_ID` |
| `FAKEYOU_EMBEDDINGS_REVISION` | `FAKEYOU_COMPOSE_EMBEDDINGS_REVISION` |


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
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_meeting_agent_eval.py
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_persona_eval.py
DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_replay_eval.py
uv run pytest tests/test_browser.py tests/test_runtime_meeting.py -m browser
uv run pytest tests/test_worker_image.py -m container
uv build
docker compose --profile production config --quiet
```

Tes default tidak menggunakan API berbayar atau membuka browser. Browser smoke test opt-in memakai browser asli pada halaman lokal untuk DOM, klik, dan screenshot. DeepEval default menggunakan respons terprogram untuk menguji wiring prompt, tool, dan argumen, bukan kualitas penalaran model live. Penilaian kualitas persona dengan model dan judge OpenAI dijalankan terpisah:

```sh
uv run --env-file .env deepeval test run evals/test_persona_eval.py -m live
uv run --env-file .env pytest evals/test_replay_eval.py -m live -s
uv run --env-file .env pytest tests/test_providers.py -m live
```

`evals/test_replay_eval.py` memutar transkrip STT nyata dari sesi `acc-0930-s1`/`s2` ke model yang sedang dikonfigurasi dalam satu thread per sesi, tiga kali, lalu menghitung pass rate fakta wajib/terlarang, recall, batas dua kalimat, pertanyaan penutup, dan latency p95. Mengganti model jawaban lisan harus lolos replay ini.

Eval persona live memerlukan `EVAL_JUDGE_API_KEY` tersendiri untuk judge OpenAI; key jawaban aktif tidak dipinjam. Variabel ini hanya untuk evaluator, bukan runtime.

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
