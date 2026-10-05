# Plan operasi worker: kontrol, isolasi, pemulihan, dan ingestion

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer kode terpisah dari penulis.

**Tujuan:** operator dapat memulai, memantau, dan menghentikan worker meeting dari luar container; dua meeting berjalan bersamaan tanpa bercampur audio atau data; worker yang crash dapat dilanjutkan tanpa mengulang ucapan; ingestion berjalan sebagai job terpisah yang dapat dilanjutkan.

**Status:** rencana, 5 Oktober 2026. Basis `master` pada `0509927`. Buat branch `feat/worker-operations`. Jangan push/merge.

**Keputusan pengguna (5 Oktober):** backend kontrol adalah Docker lokal lebih dulu, dengan antarmuka backend yang nanti dapat diganti Kubernetes tanpa mengubah CLI; checkpoint percakapan untuk worker paralel memakai Postgres.

## Kondisi sekarang

- Satu meeting = satu `docker compose run meeting-worker fakeyou join …` yang dijalankan manual dan berjalan di foreground. Stop dilakukan dengan SIGINT ke container, seperti di acceptance (`.runtime/recall-latency/acceptance/stop_watcher.py`). Tidak ada perintah start/status/stop.
- Semua worker memakai volume host `.runtime/audio` → `/workspace/.runtime` (`compose.yaml`): record, journal, checkpoint SQLite `checkpoints.sqlite`, dan profil browser.
- Checkpoint: `src/fakeyou/checkpoint.py` hanya membuka `AsyncSqliteSaver`. `langgraph-checkpoint-postgres` dan `psycopg` ada di grup dependency `production` dan service `postgres` di compose, tetapi tidak dipakai CLI dan tidak terpasang di image worker (`docker/audio/Dockerfile` memakai `uv sync --no-dev --extra audio`).
- Lock record (`src/fakeyou/ingestion_status.py:record_lock`) menilai pemilik dengan `os.kill(pid, 0)`. Antar container, PID berada di namespace berbeda sehingga penilaian hidup/mati tidak sah.
- Profil browser default per twin (`settings.paths.browser_profile_dir / twin_id`); dua meeting dengan twin yang sama akan berbagi profil.
- Lifecycle (`src/fakeyou/lifecycle.py`) menyimpan fase dan `terminal_reason`; stop lewat SIGINT tercatat `left_by_owner`, bukan `stopped_by_owner` (temuan review acceptance 4 Oktober).
- Ingestion sudah tahap operator terpisah (`memory-ingest`) dengan status `pending`/`interrupted`/`completed` dan perintah lanjut yang dicetak `join`.

## Batasan global

- Tanpa dependency baru. Paket yang sudah ada di lockfile (`langgraph-checkpoint-postgres`, `psycopg[binary,pool]`) boleh dipindah dari grup `production` ke dependency utama; verifikasi lockfile tidak menambah paket atau mengubah versi. Docker dikendalikan lewat CLI `docker`/`docker compose` (subprocess), bukan SDK baru.
- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- `.env` boleh diubah untuk konfigurasi pengembangan setelah backup `.env.backup-<tanggal>`; jangan menampilkan secret. Jangan mengubah artefak `.runtime` lama. Run baru di `.runtime/worker-operations/`.
- Semua gate yang sudah lulus dipertahankan: gate statis, `pytest -m 'not live and not container'`, container test, gate semantik corpus, dan perilaku acceptance. Tanpa `xfail` dan tanpa menghapus kasus.
- SQLite tetap didukung untuk satu worker lokal; Postgres dipakai bila dikonfigurasi. Tanpa shim atau alias konfigurasi lama.

## Task 0 — Baseline dan peta

- [ ] Catat HEAD/status; jalankan gate statis, `pytest -m 'not live and not container'`, build image, dan `pytest -m container tests/test_worker_image.py`.
- [ ] Baca `compose.yaml`, `docker/audio/Dockerfile`, `docker/audio/entrypoint.sh`, `src/fakeyou/cli/join.py`, `src/fakeyou/lifecycle.py`, `src/fakeyou/checkpoint.py`, `src/fakeyou/ingestion_status.py`, `src/fakeyou/journal.py`, `src/fakeyou/report.py`, `src/fakeyou/cli/memory.py`, dan `tests/test_worker_image.py`.

## Task 1 — Keamanan lintas container

- [ ] Tes merah: lock record yang dipegang worker lain (identitas host/container berbeda) dengan heartbeat segar ditolak; lock dengan heartbeat basi dapat diambil alih; lock milik proses sendiri tetap berperilaku seperti sekarang.
- [ ] Ganti penilaian pemilik lock dari PID saja menjadi lease: identitas pemilik (hostname/container + PID + token), waktu heartbeat yang diperbarui selama lock dipegang, dan batas basi yang dapat dikonfigurasi. Tulis atomik di volume bersama.
- [ ] Profil browser: dua meeting tidak boleh memakai direktori profil yang sama secara bersamaan. Mode guest memakai profil per meeting; profil bertanda masuk (signed-in) dikunci eksklusif dan worker kedua ditolak sebelum browser dibuka. Tes untuk kedua mode.

## Task 2 — Checkpoint Postgres

- [ ] Tes merah: dengan setting DSN checkpoint terkonfigurasi, `open_checkpointer` membuka saver Postgres; tanpa DSN tetap SQLite pada `paths.checkpoint_path`. Secret DSN tersamarkan di `fakeyou check`.
- [ ] Tambah setting DSN checkpoint (misalnya `FAKEYOU_CHECKPOINT__URL`) dan `.env.example`; pindahkan paket Postgres ke dependency utama; pasang di image worker.
- [ ] Compose: worker dapat memakai service `postgres` (profil yang jelas dan healthcheck). Tes container/live: dua thread meeting berbeda di Postgres yang sama tidak saling memengaruhi, dan checkpoint terbuka ulang setelah worker restart.

## Task 3 — CLI kontrol worker (backend Docker)

- [ ] Tes merah dengan runner command palsu: `fakeyou worker start`, `fakeyou worker status`, dan `fakeyou worker stop` membentuk command Docker yang benar, memberi label container per meeting ID, menolak start untuk meeting ID yang sedang berjalan, dan tidak pernah mencetak secret.
- [ ] Antarmuka backend kecil (start/status/stop/ingest) dengan implementasi Docker; CLI bergantung pada antarmuka, bukan pada Docker langsung.
- [ ] `start` menjalankan worker detached dengan argumen `join` yang sama seperti sekarang (persona, brief, memory, durasi, lobby) dan mengembalikan ID container serta meeting ID.
- [ ] `status` menggabungkan status container dan fase lifecycle terakhir dari journal (join, admission, conversation, ended, terminal_reason, fault terakhir). Tersedia untuk satu meeting atau semua.
- [ ] `stop` mengirim stop yang anggun, menunggu `session_finished` di journal sampai batas waktu, melaporkan `terminal_reason`, lalu eskalasi bila worker tidak keluar. Stop dari operator tercatat `stopped_by_owner`; perbaiki label SIGINT di lifecycle bila perlu.

## Task 4 — Crash dan restart

- [ ] Definisikan perilaku: worker mati mendadak (SIGKILL/OOM) di tengah meeting meninggalkan record, journal, dan checkpoint tanpa `session_finished`. `fakeyou worker start` untuk meeting ID yang sama mendeteksi sesi tak selesai dan menjalankan lanjutan, bukan meeting baru.
- [ ] Tes merah: lanjutan memakai thread checkpoint yang sama, menulis penanda lanjutan di record dan journal, masuk ulang ke Meet dengan browser baru, dan tidak memutar ulang jawaban yang sudah tercatat sebagai `twin_speech`. Snapshot brief yang ada dipakai ulang dan divalidasi.
- [ ] Opsional dengan bukti: restart otomatis terbatas (jumlah percobaan maksimum) setelah crash; nonaktif secara default.

## Task 5 — Ingestion sebagai job

- [ ] `fakeyou worker ingest <meeting-id>` menjalankan `memory-ingest` di container terpisah setelah sesi selesai, menolak bila sesi masih berjalan atau lock dipegang, dan dapat dilanjutkan dari status `interrupted`.
- [ ] `worker status` menampilkan status ingestion dan debrief per meeting.

## Task 6 — Dokumentasi dan status proyek

- [ ] Runbook baru `docs/runbooks/worker-operations.md`: start/status/stop, dua meeting paralel, crash dan lanjutan, ingestion job, konfigurasi Postgres.
- [ ] Perbarui README dan `docs/reference/project-status.md`, yang masih bertanggal 2 Oktober, sesuai keadaan setelah milestone brief/delegasi/debrief, memori tepercaya, prefetch, dan operasi worker.

## Task 7 — Verifikasi akhir dan acceptance

- [ ] Gate statis + non-live/non-container, build image, container test, dan tes Postgres.
- [ ] Acceptance Meet dua meeting paralel (minta dua link Meet baru dari pengguna setelah semua gate offline dan container siap):
  - start dua worker dengan `fakeyou worker start`, pastikan record, journal, checkpoint thread, dan profil browser terpisah, dan setiap twin hanya menjawab meeting-nya;
  - `fakeyou worker status` menampilkan fase keduanya;
  - `fakeyou worker stop` satu meeting tanpa memengaruhi yang lain, dengan `stopped_by_owner`;
  - `docker kill -s KILL` worker kedua di tengah percakapan, lalu `fakeyou worker start` dengan meeting ID yang sama melanjutkan tanpa jawaban ganda;
  - `fakeyou worker ingest` untuk kedua meeting, termasuk lanjutan dari `interrupted`.
- [ ] Commit lokal per task yang direview oleh reviewer kode terpisah. Akhiri dengan daftar commit, command + exit code, artefak, tabel acceptance, dan risiko (termasuk apa yang masih diperlukan untuk backend Kubernetes).

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/worker-operations.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/plans/next-development.md bagian milestone operasi worker, docs/reference/capabilities.md, dan compose.yaml sebelum mulai. Buat branch feat/worker-operations dari master terbaru (sudah memuat plan ini); jangan reset, buang perubahan, push, atau merge.

Keputusan pengguna: backend kontrol Docker lokal lebih dulu dengan antarmuka backend yang nanti dapat diganti Kubernetes; checkpoint percakapan untuk worker paralel memakai Postgres. Tujuan: fakeyou worker start/status/stop/ingest dari luar container; lock record dan profil browser aman lintas container (lease, bukan PID); checkpoint Postgres bila dikonfigurasi dengan SQLite tetap untuk satu worker lokal; worker yang crash dapat dilanjutkan pada meeting ID yang sama tanpa memutar ulang jawaban; ingestion sebagai job terpisah yang dapat dilanjutkan; runbook dan status proyek diperbarui.

Aturan: Python Inggris tanpa komentar/docstring baru dan tanpa string Bahasa Indonesia di .py; dokumentasi Bahasa Indonesia; tanpa dependency baru (paket Postgres yang sudah ada di lockfile boleh dipindah ke dependency utama tanpa menambah paket atau mengubah versi; Docker lewat CLI, bukan SDK baru); boleh mengubah .env untuk konfigurasi pengembangan setelah backup dan tanpa menampilkan secret; jangan mengubah artefak lama; semua gate yang sudah lulus dipertahankan; tanpa xfail.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Setiap task direview oleh reviewer kode terpisah sebelum commit lokal. Catat progres di .runtime/worker-operations/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Akhiri dengan commit lokal yang direview, tabel acceptance dua meeting paralel (isolasi, status, stop, crash dan lanjutan, ingestion), risiko, dan apa yang masih dibutuhkan untuk backend Kubernetes. Minta dua link Meet baru hanya setelah gate offline, Postgres, dan container siap.
```

Jalankan:

```sh
mkdir -p .runtime/worker-operations
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/worker-operations/muse-prompt.txt
```

Salin blok prompt ke `.runtime/worker-operations/muse-prompt.txt` terlebih dahulu; periksa `muse exec --help` bila opsi berbeda.
