# Rencana refactor hardening

Status: berjalan di branch `refactor/hardening` (11 Oktober 2026), dasar `fae9781`. Dasar temuan: empat audit baca-saja atas `src/fakeyou`, `tests`, `evals`, `compose.yaml`, `docker/audio`, dan CI — penanganan error, keamanan, ketahanan runtime, serta sisa utang refactor P1–P10. Baseline: seluruh gate hijau, 5.814 tes lulus.

## Prinsip

1. **Invarian dijamin oleh konstruksi.** ID yang menjadi path divalidasi di tipe, bukan di setiap pemanggil; file yang dibaca ulang selalu ditulis atomik; I/O eksternal selalu punya batas waktu.
2. **Tanpa jalan pintas yang menunggu fase.** Pengecualian bertanda "sampai Px" dihapus beserta penyebabnya.
3. **Gagal terlihat.** Setiap `except` yang menelan error meninggalkan jejak (fault, counter, journal, hasil terminal). Error internal tidak menyamar sebagai error provider, dan error provider tidak sampai ke pengguna sebagai traceback.
4. **Gate menegakkan aturan.** Aturan baru masuk `ruff`/`mypy`/`import-linter`/tes aturan kode setelah pelanggarannya nol, tanpa `noqa`.
5. **Breaking change diterima** dan dicatat di laporan akhir.

## Fase

Setiap fase: tes regresi RED lalu GREEN, gate penuh (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`, `deptry`, `pytest` tanpa dan dengan `TEST_DATABASE_URL`), review independen, snapshot. Fase yang mengubah `src`, `compose.yaml`, atau `docker/` ditutup dengan rebuild image `fakeyou-meeting-worker:local` dan `pytest -m container`.

### H1 — Batas filesystem dan integritas persistence

| # | Temuan | Perbaikan |
| --- | --- | --- |
| 1.1 | `--meeting-id ../../x` pada `diarize run/show` (`cli/diarize.py:92,112`) dan `join` (`config/meeting_request.py:81-99`, `cli/join.py:541-549`) keluar dari `record_dir`, `journal_dir`, dan profil browser | `persistence/layout.py`: `MeetingDir` satu-satunya sumber nama file per meeting (record, ingestion, actions, meeting-context, participants, attribution-probe, diarization, correction labels, debrief, `slots.json`, `audio/`, `visual/`); konstruktor hanya menerima ID bertipe tervalidasi. `meeting_record_path`, `journal_path`, lock, dan profil browser memakai layout ini. `.runtime` yang tertanam di `meet/browser.py:314` dan `worker/worker_backend.py:487-584` diambil dari `Settings.paths` |
| 1.2 | Empat gaya validasi ID (`TypeAdapter(SessionId)`, `IDENTIFIER_PATTERN`, `_GROUP_PATTERN`, salinan di `debrief_schema.py:64`) | Satu tipe ID tervalidasi; `meeting_id`, `owner_id`, `twin_id` divalidasi di `validate_meeting_request` dan setiap command |
| 1.3 | `write_atomically` tinggal di `ingestion_status.py` bersama lease; empat file yang dibaca ulang ditulis tidak atomik (`visual_store.py:142`, `audio_persist.py:106`, `meet/browser.py:155`, `cli/calibrate.py:314`) | `persistence/files.py` (tulis atomik `O_EXCL` mode `0o600` + fsync direktori; pembaca JSONL toleran dengan penghitung baris rusak) dan `persistence/lease.py`. Semua penulis dan pembaca JSONL longgar (`report.py:76`, `probe_replay.py:415,436,651`, `visual_store.py:125`, `cli/calibrate.py:105`, `debrief_sources.py:152`) memakai helper tersebut |
| 1.4 | Audio, transkrip, journal, frame visual, dan profil browser dibuat `0644`/`0755` | `os.umask(0o077)` di entry CLI dan `umask 077` di `entrypoint.sh`; root runtime dibuat `0o700` |
| 1.5 | Baris terakhir `record.jsonl` yang terpotong (SIGKILL, ENOSPC) membuat ingest, debrief, dan diarize gagal permanen; `--resume` menyambung ke potongan | Saat dibuka untuk append, ekor tanpa `\n` dipotong dan dicatat `record_tail_dropped`; loader hanya melewati baris terakhir yang rusak (dengan fault), korupsi di tengah tetap ditolak; fsync berkala dan saat close |
| 1.6 | `SlotAudioWriter`: gap fill tanpa batas, regresi `seq` membuang semua chunk berikutnya, anchor hanya ditulis saat close, header WAV salah setelah crash | Gap fill dibatasi, `slot` dibatasi `MAX_PARTICIPANTS`, regresi membuka segmen baru, anchor append-only, header WAV dipatch berkala |
| 1.7 | Sidecar visual ditulis ulang penuh setiap frame | Record append-only yang dilipat saat dibaca |
| 1.8 | `memory forget` melaporkan sukses walau `rmtree(ignore_errors=True)` gagal, profil browser meeting tertinggal | Hapus tanpa `ignore_errors`, verifikasi direktori hilang, profil ikut dihapus |
| 1.9 | `read_status` menyamakan status korup dengan status kosong; lease kalah oleh lompatan jam dan TOCTOU saat refresh | File hilang berarti `None`, file korup berarti error katalog; host sama memeriksa pid dulu; refresh memakai compare-and-swap di bawah `flock`; kegagalan heartbeat dihitung |
| 1.10 | Link Meet menerima path, query, dan port apa pun di `meet.google.com`; host akhir setelah `goto` tidak dicek | Path `^/[a-z]{3}-[a-z]{4}-[a-z]{3}$`, tanpa port/userinfo, query terbatas; host akhir diverifikasi |

### H2 — Browser, secret, container, CI

| # | Temuan | Perbaikan |
| --- | --- | --- |
| 2.1 | `chat --browser` memberi agent seluruh tool Playwright MCP, termasuk `browser_run_code_unsafe` (setara RCE), upload file, cookie, dan tool WebMCP milik halaman; `cwd` MCP adalah root proyek | Allowlist tool eksplisit (navigasi, snapshot, klik, ketik, tombol, tunggu, screenshot, tutup); WebMCP mati; `cwd` direktori kosong sementara |
| 2.2 | Chromium berjalan `--no-sandbox` di semua lingkungan dan mewarisi seluruh secret `.env` | Environment browser memakai allowlist (`browser_environment`). Sandbox Chromium tidak dapat diaktifkan per launch: cloakbrowser selalu menambahkan `--no-sandbox` dan tidak menyediakan opsi untuk membatalkannya tanpa mematikan stealth. Setelan `browser.sandbox` yang sebelumnya tidak berfungsi dihapus; `--no-sandbox` diterima dan diimbangi hardening container |
| 2.3 | Port CDP terbuka pada sesi meeting walau hanya MCP membutuhkannya | Argumen remote debugging hanya di `open_browser_tools` |
| 2.4 | Hook tes `stream_capture.js` terpasang di `window` produksi | Hook hanya terpasang bila flag tes diinjeksi |
| 2.5 | `fakeyou check` mencetak kredensial di URL dan `extra_body`; `trim_detail` hanya mengenali `https?://`; `BrowserLaunch.license_key` tampil di repr; detail lifecycle checkpoint memuat `str(error)` mentah | Samarkan userinfo/query URL dan nilai `extra_body`; regex sanitasi mencakup semua skema URL, `sk-…`, `x-goog-api-key`, `Basic`; `repr=False`; detail lewat `trim_detail` |
| 2.6 | Base URL `http://` diterima sehingga key terkirim tanpa TLS | Validator: `https` wajib kecuali loopback atau nama service tanpa titik |
| 2.7 | `meet_send_chat` tanpa batas panjang dan laju | Batas panjang, batas laju per meeting, tolak teks berisi evidence memori |
| 2.8 | Postgres (berisi voiceprint) memakai password default; worker tanpa `cap_drop`, `no-new-privileges`, rootfs read-only, batas pid/memori, rotasi log; healthcheck Neo4j membawa password di argv; image dipin per tag | `POSTGRES_PASSWORD` wajib; hardening service worker; healthcheck lewat env; pin digest; `stop_grace_period` selaras anggaran cleanup |
| 2.9 | Image runtime membawa `build-essential`, `curl`, header dev | Stage builder terpisah |
| 2.10 | CI: token checkout tersimpan, tanpa `timeout-minutes`, tanpa audit dependency dan pemindai secret, lock npm tidak diverifikasi | `persist-credentials: false`, `timeout-minutes`, komentar versi pin, Dependabot, `pip-audit`, `npm ci --ignore-scripts` + `npm audit`, gitleaks |
| 2.11 | Tiga dependency npm tidak dipakai (`esbuild` satu-satunya yang punya install script); katalog error menyuruh `npm ci` tanpa `--ignore-scripts`; `node` dan `docker` diambil dari PATH tanpa pemeriksaan | Hapus dependency; perbaiki pesan katalog; binary di-resolve sekali dengan error katalog |

### H3 — Ketahanan runtime meeting

| # | Temuan | Perbaikan |
| --- | --- | --- |
| 3.1 | Scoring voiceprint (unpack PCM, inferensi ONNX, query sinkron SQLAlchemy) berjalan di event loop setiap ucapan final | Voiceprint dimuat sekali per sesi; scoring di thread pekerja tunggal dengan batas waktu; konversi PCM lewat `numpy` |
| 3.2 | Checkpointer satu koneksi psycopg tanpa keepalive, timeout, atau reconnect; `MeetingLifecycle.send` tanpa batas waktu; `psycopg.Error` lolos sebagai traceback | Pool koneksi dengan pemeriksaan, keepalive, `connect_timeout`, `statement_timeout`; `send` berbatas waktu; kegagalan persist saat admitted menjadi degradasi tercatat; `psycopg.Error` dipetakan ke kode katalog |
| 3.3 | Timeout watchdog tidak dihitung sebagai kegagalan observasi sehingga renderer macet membuat bot tertinggal sampai durasi habis | `observation_failed=True` pada timeout |
| 3.4 | Pregen yang diadopsi memutar audio di dalam callback transkrip 10 detik; pregen yang dibuang menulis ke thread percakapan asli | Kalimat buffer masuk antrean consumer; pregen di thread scratch, dipindah ke thread asli hanya saat diadopsi |
| 3.5 | `evaluate`/`content` Playwright tanpa timeout di loop poll dan alur join | Batas waktu per poll; batas total per percobaan join |
| 3.6 | Drain capture menelan kegagalan tanpa journal dan membuang seluruh batch bila satu entri rusak | Parse per entri, journal kegagalan pertama dan setiap ke-N, eskalasi setelah K kegagalan berturut-turut |
| 3.7 | `pactl`, Docker CLI, diarize HTTP (`urlopen` tanpa timeout di bawah lease yang terus di-refresh), Postgres connect, SDK Graphiti (600 s × 2 retry) tanpa batas waktu | Batas waktu eksplisit dari konfigurasi; diarize mendapat retry berbatas dan pemetaan error yang benar |
| 3.8 | Thread percakapan tumbuh sepanjang meeting sampai melewati context window | Pemangkasan riwayat hanya pada request model (checkpoint utuh), anggaran token dari konfigurasi |
| 3.9 | Tool call yang dibatalkan meninggalkan `AIMessage.tool_calls` yatim sehingga giliran berikut ditolak provider | Middleware penambal tool call yatim sebelum setiap panggilan model, berlaku untuk jalur streaming dan non-streaming |
| 3.10 | Anggaran teardown tunggal 8 detik untuk banyak resource; penutupan Graphiti bisa menggantung tanpa bisa dibatalkan; `docker stop --time` lebih pendek dari `stop_grace_period` | Anggaran per callback; tenggat keras penutupan memori; grace stop dari jumlah anggaran |
| 3.11 | Reconnect STT berurutan memblokir loop monitor hingga 80 detik, tanpa jitter | Reconnect per slot di task sendiri dengan backoff eksponensial + jitter |
| 3.12 | Bukti partisipan ~0,5 GB per jam ditulis sinkron; kerja gambar visual di event loop; caption visual tanpa batas konkurensi dan anggaran berbalapan | Tulis hanya saat berubah dengan batas ukuran, kerja berat di thread, semaphore + reservasi anggaran |
| 3.13 | `asyncio.timeout` membungkus `yield`; `gather` tanpa agregasi; cleanup terlewati saat cancel; handler sinyal hanya di sekitar sesi; anggaran prefetch tidak lengkap; setelan STT/VAD tanpa batas; lisensi CloakBrowser divalidasi sinkron di loop | Timeout per `__anext__`; `TaskGroup`; cleanup bertingkat; handler sinyal sepanjang `join` dengan keluar paksa pada sinyal kedua; satu timeout prefetch; `Field` berbatas + validator silang; resolusi binary di thread |

### H4 — Disiplin error

- 35 situs broad tanpa jejak diperbaiki; sekitar 45 situs dipersempit (29 ke `playwright.async_api.Error`, 11 ke tipe OS/jaringan, 5 kode mati atau murni dihapus).
- Satu kelas dasar `PollingLoop` untuk lima loop poll yang menduplikasi idiom cancel; `close_safely` untuk setiap `aclose`; sink diagnostik wajib di kode library (sink null hanya di tes); satu batas klasifikasi per subsistem (LLM, Graphiti, Pipecat, TTS).
- Cancellation: tidak ada `except CancelledError: pass` yang menelan cancel milik pemanggil; sinyal stop pemilik memakai exception sendiri, bukan `CancelledError`; `except BaseException` di sink sinkron menjadi `Exception` atau lebih sempit.
- 16 penelanan sempit tanpa jejak diperbaiki (record visual, sidecar korup, `slots.json` korup, status korup, sinyal gagal dipasang, laporan tanpa penghitung baris rusak).
- Kebocoran ke pengguna: error SDK di `chat`, `memory *`, `browser-tools`, diarize, `audio purge`, dan `report` menjadi `FakeYouError`; `classify_exception` meneruskan `FakeYouError` dan tidak menyamarkan bug internal sebagai provider; `reason` hanya memuat nama tipe; traceback berbatas ditulis di batas sesi.
- Gate: `TRY002`, `TRY004`, `TRY201`, `TRY203`, `TRY300`, `TRY400`, `TRY401`, `S110`, `S112`, `RUF006`, `BLE001` (ignore hanya untuk modul penampung di `observability`). `TRY003`, `EM101`, `EM102`, `TRY301`, dan `SIM105` ditolak: bertentangan dengan aturan pesan literal atau mendorong `suppress(CancelledError)`.

### H5 — Gate dan utang refactor

- `per-file-ignores` di `src` dihapus (30 dari 37 situs): `@override` dan error code mypy `explicit-override`, hook alembic `include_name`, boolean keyword-only, parameter `settings` mati dibuang. Yang tersisa sah: `PLR0913` perintah typer dan `T201` sink output.
- Service diarize dipisah dari CLI sehingga `ignore_imports` import-linter hilang.
- `per-file-ignores` `tests/*` dan `evals/*` dihapus: 709 pelanggaran diperbaiki per aturan (`ARG`, `SLF`, `FBT`, `PLR0913`, `C901`, `T201`), termasuk dataclass `kw_only` dan seam publik pengganti akses privat.
- 82 `pytest.raises` tanpa `match`; snapshot `os.environ` menjadi fixture autouse; nama `legacy` dihapus dan dicek tes aturan kode; import basi di runbook diperbaiki.
- `cast` yang berbohong (`conversation.py:1290`) dan `Any` yang punya tipe nyata (`Page`, `Frame`, transport, agent); stub lokal `cloakbrowser` dan `sherpa_onnx`.
- Kode mati dihapus; `visual_purge_expired` disambungkan ke command dengan kode error sendiri; API publik yang hanya dipakai tes dipindah ke dukungan tes atau disambungkan; mesin replay eval pindah ke `evals/_replay.py`.

### H6 — Struktur dan konsolidasi

- CLI tipis: drain transkrip dan perakitan sesi `join` ke `orchestration`; mesin status ingest dan forget ke `memory`; debrief ke service `debrief`; kalibrasi dan enrollment ke `identity`; `_operator` ke `memory/persona.py`; purge retensi ke service domain.
- Duplikat: coercer angka finite, lima parser waktu ISO, konstanta 16 kHz, konversi PCM16, digest JSON kanonis, lokasi data paket.
- 24 file tes di atas 800 baris dipecah per aspek.
- `docs/reference/code-standard.md` ditulis ulang sesuai keadaan nyata (daftar aturan aktif, keputusan TRY/EM, cakupan mypy, kebijakan dukungan tes, mekanisme `Any`/`cast`).
- Laporan breaking change di `docs/reports/`.

## Keputusan yang diambil sendiri

| Keputusan | Alasan |
| --- | --- |
| Sandbox Chromium menjadi setelan eksplisit, bukan dihapus total dari container | Seccomp default Docker memblokir user namespace; container memakai hardening sebagai kompensasi |
| Riwayat percakapan dipangkas di request model, checkpoint tidak dipangkas | Pola yang sama dengan evidence memori; tidak ada data hilang, resume tetap utuh |
| `TRY003`/`EM101`/`EM102` tidak diaktifkan | Bertentangan dengan aturan pesan literal; nilai hardening nol |
| File `.env.backup-*` di root tidak disentuh | Milik operator; direkomendasikan rotasi key lalu hapus |
| Tanpa commit | Perubahan disimpan sebagai snapshot per fase untuk review |

## Di luar cakupan

Enkripsi voiceprint saat diam (butuh manajemen key), sandbox Chromium di dalam container dengan profil seccomp kustom, dan event journal bertipe penuh (331 `Any`, sebagian besar kantong baris journal) — dicatat sebagai milestone lanjutan.
