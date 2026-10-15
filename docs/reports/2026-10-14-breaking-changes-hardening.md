# Breaking changes hardening (H1–H6) dan iterasi 1 addressing

Laporan ini mencatat perubahan pemutus dari rencana [hardening](../plans/hardening.md), setelah laporan refactor total ([2026-10-11](2026-10-11-breaking-changes.md)). Tidak ada shim kompatibilitas: import dan nama lama dihapus, dan pemanggil harus pindah ke path baru. Setiap fase tetap berada di satu tree kerja dan belum di-commit; daftar di bawah dibaca dari keadaan kode saat H6 ditutup. Bagian 7 mencatat iterasi addressing yang dikerjakan di tree yang sama setelah H6.

## 1. Batas filesystem, persistence, dan disiplin error (H1, H4)

- ID meeting dan owner/twin yang berbentuk path (`../x`, `a/b`) ditolak di boundary CLI dengan kode `SESSION_ID_INVALID` sebelum pekerjaan apa pun dimulai.
- Path per meeting diakses lewat `persistence/layout.py` (`MeetingDir`); kode lama yang membangun path sendiri tidak lagi tersedia.
- Error SDK di `chat`, `memory`, `browser-tools`, diarize, `audio purge`, dan `report` menjadi `FakeYouError` dengan kode katalog. `reason` hanya memuat nama tipe, bukan teks mentah.

## 2. Browser, secret, dan container (H2)

- `chat --browser` hanya menerima allowlist tool navigasi dan interaksi; `browser_run_code_unsafe`, upload, dan WebMCP dimatikan.
- Environment Chromium memakai allowlist (`_browser_environment` di `meet/browser.py`). Variabel `FAKEYOU_*` dan API key provider tidak lagi diwariskan ke proses browser.
- Setelan `FAKEYOU_BROWSER__SANDBOX` dihapus. Chromium selalu berjalan dengan `--no-sandbox` karena cloakbrowser menambahkannya di setiap launch; isolasi berada di hardening container. Variabel yang masih ada di `.env` akan gagal dengan `Unknown configuration key`.

## 3. Ketahanan runtime (H3)

- Setiap panggilan eksternal berbatas waktu (voiceprint, checkpoint, pactl, docker, diarize, Graphiti). Batas dibaca dari konfigurasi.
- Watchdog timeout dihitung sebagai kegagalan observasi, dan cleanup berjalan dalam anggaran per callback.

## 4. Konfigurasi dan API library (H5)

- `ParticipantObserver` menerima `on_poll` wajib; `epoch`, `snapshot`, dan `reset_epoch` dihapus.
- `PollingLoop.task` dihapus. Gunakan `started` (bool) dan `await wait()`.
- `foundation/participants.py` dan resolver DOM-nya dihapus. `append_participant_evidence` dihapus.
- `ConversationLoop` menolak responder yang hanya streaming bila `sentence_streaming` mati (`TypeError`).
- `fakeyou.identity.voiceprint_embed.SAMPLE_RATE` dan `fakeyou.identity.enrollment.VOICEPRINT_SAMPLE_RATE` dihapus; gunakan `CAPTURE_SAMPLE_RATE` (`foundation/invariants.py`).
- Korelasi koreksi diarization menghasilkan `correction_labels.json` di samping `diarization.json`. Label dibaca oleh ingest dan debrief; nama ditampilkan sebagai *unverified*.
- Perintah baru `fakeyou visual purge-expired` dengan kode `VISUAL_RETENTION_DAYS_NOT_POSITIVE`. Retensi teks tidak ikut berubah dengan `--days`.
- Nama query Cypher `PUBLISH_LEGACY_CORRECTION_QUERY` menjadi `PUBLISH_UNTRACKED_CORRECTION_QUERY`.

## 5. Pemisahan CLI dari domain (H6)

Modul CLI hanya memparse argumen, memanggil satu layanan, dan mencetak hasil. Logika yang pindah:

| Path lama | Path baru |
| --- | --- |
| `fakeyou.cli.join` (drain transkrip, perakitan sesi) | `fakeyou.orchestration.transcript`, `orchestration.join`, `orchestration.join_context`, `orchestration.join_types` |
| `fakeyou.cli.memory` (status ingest, forget, operator, note) | `fakeyou.memory.ingest_status`, `ingest_service`, `forget`, `owner_note_service`, `operator_access`, `maintenance` |
| `fakeyou.cli.debrief` (orkestrasi debrief) | `fakeyou.debrief.debrief_service` |
| `fakeyou.cli._operator` (`OperatorIdentity`, `load_operator_persona`) | `fakeyou.memory.persona` |
| `fakeyou.cli.calibrate` | `fakeyou.identity.calibration` |
| `fakeyou.cli.voiceprint` (enrollment, challenge, verify) | `fakeyou.identity.enrollment` (`EnrollSubject`, `LivenessGate` di sini) |
| `fakeyou.cli.audio` (purge) dan purge voiceprint | `fakeyou.persistence.retention`, `persistence.voiceprint_access` |

Nama publik lain yang dinaikkan dari privat (perubahan nama saja, perilaku sama): `MeetingWorker`, `drive_session`, `session_resources`, `startup_session` (`orchestration/runtime.py`); `CaptureObserver`, `TranscriptObserver` (`audio/audio.py`); `is_bare_ack` dan `request_messages` (`conversation/conversation.py`); `DIALOG_SELECTOR` (`meet/watchdog.py`); `prepare_frame`, `payload_bytes`, `journaled_stream`, `sidecar_turn_count`, `refresh_lease`, `native_transport`, `run_fleet_calls`, `tick_slot`.

Helper duplikat dipusatkan di `fakeyou.foundation`: `numbers` (`finite_float`, `real_number`, `rms`), `timeparse` (`parse_iso_datetime`, `require_iso_datetime`), `digest` (`canonical_json_digest`, dengan byte encoding yang sama seperti sebelumnya), `pcm` (`decode_pcm16`, `encode_pcm16`, `pcm16_to_floats`), dan `resources` (`read_package_text`). Digest yang tersimpan tetap valid; dibuktikan dengan pin nilai di `tests/test_digest.py` dan `tests/test_identity_calibration.py`.

Dependensi: `websockets` dideklarasikan di grup dev (`uv.lock` diperbarui; versi tidak berubah).

## 6. Tes dan evals

- Tes dan evals yang di atas 800 baris dipecah per aspek; nama file mengikuti aturan modul di `tests/test_code_rules.py`. Jumlah test function sama sebelum dan sesudah, dan setiap nama terdefinisi sekali.
- Helper bersama berada di `tests/_*_support.py` dan `evals/_*.py`, tanpa import antar-modul tes.
- `tests/fakes.py` dihapus. Fake dipindah ke `tests/_fakes_common.py` (konstanta dan clock), `_audio_fakes.py`, `_meet_browser_fakes.py`, `_memory_fakes.py`, `_conversation_fakes.py`, `_visual_fakes.py`, `_voiceprint_fakes.py`, `_persistence_fakes.py`, dan `_runtime_fakes.py`. Import `from tests.fakes import ...` harus diganti dengan modul domain yang sesuai.
- `evals/test_trust_eval.py` dan `evals/test_brief_eval.py` digantikan oleh `evals/test_trust_*_eval.py` dan `evals/test_brief_*_eval.py`. Setiap nama berakhiran `_eval`.
- `evals/_replay.py`, `evals/_budgets.py`, dan `evals/_brief.py` adalah modul dukungan baru.
- Pesan error yang diuji lewat `pytest.raises` memakai `tests/_error_support.message_pattern` (diambil dari template katalog).
- Helper tes join (`join_persona_conversation` di `tests/_cli_support.py`) membatasi setiap tunggu sesi dengan `SETTLE_TIMEOUT_SECONDS` (5 detik): hook `settle`, atau `loop.wait_idle()` bila tidak ada hook. Turn yang tidak pernah selesai gagal dengan `TimeoutError` dalam beberapa detik dan tidak menggantung. Dibuktikan dengan tes sementara yang memodelkan turn tak berujung; tes itu sudah dihapus.
- Tes kosakata bare-ack (`test_bare_ack_while_speaking_does_not_yield_the_floor`) diparametrisasi dengan `ACK_CASES["vocabulary"]` sehingga setiap kata dicek; tidak ada literal bahasa Indonesia di file `.py`.

## 7. Iterasi 1: addressing sadar lantai (Digital Twin)

Ini keputusan produk, bukan sekadar refactor. Pemilik perlu menyetujuinya karena mengubah cara twin menjawab di `--converse`.

### Perubahan perilaku

- **Tanpa `--wake-phrase`, twin hanya menjawab transkrip final yang menyebut nama twin.** Sebelumnya setiap transkrip final dijawab, karena `split` pada frasa kosong menghasilkan `[""]` dan `"" in text` selalu benar. Nama twin adalah `display_name` persona bila profil dimuat; bila tidak, `FAKEYOU_IDENTITY__TWIN_NAME` (default `Fakeyou Twin`). Berlaku juga untuk 1-on-1: lawan bicara tetap harus menyebut nama twin.
- **Pencocokan nama adalah kata utuh, tidak lagi substring.** `mentions_whole_phrase` di `foundation/participation.py` memakai `(?<!\w)…(?!\w)` pada teks yang di-casefold, sehingga `Rani` tidak cocok dengan `Ranita`.
- **Jendela aktivasi tetap berlaku.** Setelah twin disebut, transkrip final berikutnya dalam `conversation.activation_seconds` (default 30 detik) ikut dijawab tanpa menyebut nama lagi. Ini memakai mekanisme `_active_until` yang sama dengan sebelumnya.
- **`--wake-phrase` eksplisit menggantikan nama twin.** Bila frasa diberikan, hanya frasa itu yang dipakai sebagai penanda. Nama twin tidak lagi berlaku sebagai penanda.
- **Barge-in pada transkrip final.** Bila `--barge-in` aktif, twin sedang bicara, tidak ada `--wake-phrase`, dan transkrip final tidak dijawab (bukan jawaban singkat), playback dihentikan tanpa jawaban baru. Journal mencatat `barge_in_detected` dengan `trigger="final"`. Jawaban singkat (`oke`, `ya`, dan sejenisnya) tidak menghentikan playback.
- **`--barge-in` interim tidak berubah.** Interim tetap menghentikan playback saat twin sedang bicara, dengan minimal tiga kata. Bila `--wake-phrase` diberikan, interim juga mensyaratkan jendela aktivasi yang masih berlaku.
- **`policy` (`participant_policy_enabled`) tidak berubah.** Mode itu tetap membutuhkan alias eksplisit. Validasi di `cli/join.py` tidak berubah.

### API internal

- `LoopInteraction.twin_names: Sequence[str] = ()` baru. Nilai dinormalisasi casefold dan strip, sama seperti `wake_phrases`. Gate final memakai `wake_phrases or twin_names`.
- `LoopBehavior.twin_names` baru di `orchestration/assembly.py`, diisi dari `orchestration/join.py` dengan nama twin.
- `mentions_whole_phrase(text, phrase)` baru di `foundation/participation.py`. Pencocokan substring di `ConversationLoop._addressed` diganti dengan fungsi ini.
- Tidak ada alias `addressing_aliases`; sempat dibuat lalu dihapus sebelum iterasi ditutup.

### Tes

- `tests/test_conversation_policy.py`: `test_wake_phrase_must_match_a_whole_word`, `test_unaddressed_finals_stay_silent_when_only_the_twin_name_is_an_alias`, `test_explicit_wake_phrase_replaces_the_default_twin_name`, `test_unaddressed_speech_yields_the_floor_without_a_reply`, dan `test_bare_ack_while_speaking_does_not_yield_the_floor` (diparametrisasi per kata kosakata).
- `tests/test_cli_join_audio.py`: `test_join_without_wake_phrase_answers_only_when_the_twin_is_named`.
- `tests/test_participation.py`: tiga tes alias dihapus bersama fungsinya.
- Tes join memakai transkrip yang menyebut nama twin (`Rani Twin, …`) karena transkrip tanpa nama sekarang tidak memanggil model.
- Mutation check: mengganti `mentions_whole_phrase` kembali ke substring membuat tes whole-word gagal.

### Celah yang belum ditutup

- Backchannel Inggris (`mm-hm`, `yeah`) belum masuk kosakata bare-ack. Celah ini ditutup di iterasi 2 (bagian 8).
- `others_speaking` dan `ambiguous` di `_policy_allows` masih `False`. Sinyal overlap dari `VoiceReason` di `audio/voice_activity.py` belum tersambung.
- Acceptance live Meet belum dijalankan. Semua bukti di atas berasal dari tes offline dan probe container.

## 8. Iterasi 2: kosakata backchannel

- `src/fakeyou/ack_vocabulary.json` (`bare_acks`) bertambah: `yeah`, `yep`, `yup`, `mm-hm`, `mhm`, `uh-huh`. Sebelumnya isinya `iya`, `oke`, `okay`, `siap`, `sip`, `ya`, dan `yes`.
- Akibatnya, backchannel satu kata dari peserta lain saat twin sedang bicara tidak lagi menghentikan playback. Ini perilaku yang diinginkan: backchannel tidak menjadi interupsi.
- Aturan lain tidak berubah. Frasa berisi lebih dari satu kata (`yeah but what about Orchid`) tetap bukan bare-ack, dan frasa dengan tanda tanya tetap tidak dihitung sebagai bare-ack.
- Pin kosakata di `tests/fixtures/ack-cases.json` ikut diperbarui. Ini tetap satu sumber: `test_bare_ack_vocabulary_is_pinned` gagal bila kosakata di `src` dan fixture berbeda.
- Tes: `test_english_backchannels_are_bare_acks` dan `test_backchannel_while_speaking_does_not_yield_the_floor` (RED dulu: 22 gagal sebelum kosakata diubah).

### Verifikasi ulang: transkrip join dan sapuan per file

- Setiap transkrip join yang mengharapkan balasan menyebut nama twin: `Rani Twin` di persona tes, `Settings Twin` di fixture CLI. Transkrip tanpa nama yang tersisa (`wait`) dipakai untuk barge-in dan memang tidak diharapkan dibalas. Tes visual (`tests/test_join_visual.py`) hanya memeriksa referensi visual pada event dan tidak bergantung pada addressing.
- Sapuan per file pada 17 file join dan terkait lulus tanpa HANG atau FAIL pada tree saat ini. Catatan sapuan lama (sebelum perbaikan iterasi ini) tidak berlaku lagi: `test_worker_entrypoint.py` lulus, dan HANG di `test_brief_join_isolation.py` dan `test_persona_join.py` sudah tertutup oleh batas waktu.
- Sapuan seluruh 244 file per proses dihentikan karena memerlukan sekitar dua jam. Cakupan penuh dibuktikan oleh pytest serial default (8079 lulus) dan mode DB (8117 lulus).

## 9. Audit ulang hardening dan penutupan H1

Audit ulang H1–H6 menemukan bahwa klaim sebelumnya, "H1 selesai", terlalu luas. Beberapa butir rencana belum ada di kode. Yang sudah diperbaiki di bagian ini:

- **1.3 berkas atomik dan lease.** `write_atomically` pindah ke `fakeyou.persistence.files` dan memakai `O_EXCL` dengan mode `0o600`. Mekanisme lease pindah ke `fakeyou.persistence.lease`. `fakeyou.persistence.ingestion_status` tinggal memuat status dan kunci rekaman. Penulisan kalibrasi dan preferensi browser memakai penulis atomik.
- **1.4 umask.** Entry `fakeyou` memasang `umask 077` sebelum apa pun dibuat. Direktori baru ikut `0o700`.
- **1.5 ekor rekaman terpotong.** Saat dibuka untuk append, ekor tanpa newline dibuang dan dicatat `record_tail_dropped`. Loader hanya melewati baris terakhir yang terpotong (`record_torn_line_dropped`). Korupsi di tengah tetap ditolak. fsync dilakukan setiap 20 entri dan saat close.
- **1.6 penulis audio slot.** Gap diisi nol paling banyak 2 detik per gap, dan sisanya dihitung di `gaps_truncated`. Slot di luar 0..15 ditolak. Urutan `seq` yang mundur jauh (lebih dari 50 chunk) dianggap restart: data tetap ditulis dan dihitung di `restarts`. Anchor ditulis append per chunk ke `audio/anchors.jsonl`. Header WAV diperbarui berkala sehingga berkas yang terpotong tetap bisa dibaca. Metadata `slots.json` ditulis berkala dan memakai skema `slot_audio/v2` tanpa anchor.
- **1.7 sidecar visual.** Pembaruan `visible_to` ditambahkan sebagai baris `range`, tidak lagi menulis ulang seluruh berkas. Pembaca memakai `read_visual_sidecar`, yang melipat baris-baris tersebut.
- **1.8 forget.** Hapus berkas tidak lagi memakai `ignore_errors`. Error dilaporkan dengan `MEMORY_FORGET_FILES_INCOMPLETE`, termasuk ketika direktori masih ada setelah hapus. Profil browser meeting ikut dihapus. Hasil JSON `memory forget` mendapat kunci `browser_profile_removed`.
- **1.9 status dan lease.** Berkas status korup memberi `INGESTION_STATUS_CORRUPT` dan tidak lagi dianggap kosong. Status worker melaporkan `invalid`. Refresh lease menulis di tempat di bawah `flock` dengan perbandingan token. Kehilangan lease (token berubah, berkas hilang, atau tiga kegagalan heartbeat berturut-turut) dilaporkan `LEASE_LOST` saat keluar dari lock. Takeover di host yang sama hanya terjadi bila PID sudah mati, sehingga heartbeat basi tidak lagi cukup.
- **1.10 link Meet.** Hanya `https://meet.google.com/xxx-xxxx-xxx` dengan query `authuser` (angka) atau `hl` yang diterima. Port, userinfo, fragment, dan query lain ditolak. Setelah navigasi berhasil, host akhir harus `meet.google.com`.
- **1.2 validasi ID.** Diverifikasi: satu pola `IDENTIFIER_BODY` dan satu tipe `SessionIdField` dipakai oleh semua pemanggil.

Breaking changes:

- Impor dari `fakeyou.persistence.ingestion_status` untuk `write_atomically`, `LeaseError`, `LockPurpose`, `file_lease`, `leased`, `lock_owner`, `refresh_lease`, `describe_lease`, `LOCK_STALE_AFTER_SECONDS`, `LEASE_HEARTBEAT_MAX_FAILURES`, dan `LeaseState` tidak lagi valid. Gunakan `fakeyou.persistence.files` atau `fakeyou.persistence.lease`.
- `read_status` melempar `FakeYouError` untuk berkas korup. Berkas yang tidak ada tetap `None`.
- `refresh_in_place` menjadi publik di `fakeyou.persistence.lease`.
- Format slot audio berubah ke `slot_audio/v2`: anchor pindah dari `slots.json` ke `audio/anchors.jsonl`. Berkas lama tidak dibaca lagi. Tidak ada shim.
- Sidecar visual memakai baris `range`. Pembaca langsung dari berkas harus memakai `read_visual_sidecar`.
- Link Meet yang berisi parameter lain selain `authuser` atau `hl` ditolak dengan `MEETING_LINK_INVALID`.
- Kode katalog baru: `LEASE_LOST`, `INGESTION_STATUS_CORRUPT`, `MEMORY_FORGET_FILES_INCOMPLETE`. Nama event jurnal baru: `record_tail_dropped`, `record_torn_line_dropped`.
- Modul `fakeyou.foundation.jsonl` (`read_jsonl`) baru. Pembaca JSONL toleran ini dipakai observability dan probe replay.

Keputusan yang menyimpang dari rencana, dengan alasan:

- `read_jsonl` ditempatkan di `foundation/jsonl.py`, bukan `persistence/files.py`. Kontrak lapisan import-linter menempatkan `observability` di bawah `persistence`, sehingga `observability` tidak boleh mengimpor `persistence`.
- Pembaca JSONL kalibrasi dan snapshot debrief tetap ketat dan melempar error pada baris rusak. Pembacaan toleran di sana bisa menelan data latih atau bukti.
- Sequence yang mundur dibuat rebase pada berkas yang sama, bukan segmen baru. Diarization mensyaratkan satu segmen per slot dan `seq_start` yang sama di semua slot.
- Batas slot adalah `MAX_SLOTS = 16`, bukan `MAX_PARTICIPANTS`, yang tidak ada di kode. `MEET_AUDIO_SLOTS` berada di lapisan audio, yang lebih tinggi daripada persistence, sehingga tidak bisa diimpor.
- Fixture probe replay mengembalikan `skipped_rows`.
- Mode `--profile-dir` tidak diketahui forget. Profil dengan lokasi kustom tidak ikut dihapus dan harus dihapus manual.

Verifikasi:

- Tes baru atau diubah: `tests/test_memory_forget_files.py`, `tests/test_meet.py`, `tests/test_meeting_request.py`, `tests/test_ingestion_status.py`, `tests/test_record.py`, `tests/test_audio_persist.py`, `tests/test_visual_store.py`, `tests/test_visual_observer.py`, `tests/test_cli_main.py`, dan beberapa tes lain yang menyesuaikan kontrak baru.
- Mutation check pada penulis audio: menghapus batas gap, mengembalikan drop saat restart, menghapus patch header, dan menghapus metadata berkala. Setiap mutasi membuat tes yang relevan gagal, lalu kode dipulihkan.
- Tes yang memakai `settings` sekarang mengisolasi `browser_profile_dir` dan `browser_output_dir` di `tmp_path`, sehingga tidak menyentuh `.runtime` di repositori.
- Gate: ruff check dan format, mypy (416 file), lint-imports (4 kontrak), deptry, pytest serial default (8154 lulus), pytest mode DB (8192 lulus), `pytest -m container` (6/6), `fakeyou check` (exit 0), probe canary browser (3 canary ada, 0 bocor), dan pulse + transport di stage (2/2).

Catatan keterbatasan audit:

- Butir H2–H6 yang belum dibaca baris demi baris, dan hanya diverifikasi lewat pencarian kode dan konfigurasi: 2.8, 2.10, 2.11, 3.1–3.13, dan H4–H6 (sebagian besar lewat konfigurasi ruff dan keberadaan modul). Butir-butir ini ada di kode, tetapi perilakunya belum diuji ulang satu per satu.
- Pembacaan ulang seluruh baris untuk H2–H6 belum dilakukan.

## 10. Audit H2–H6 dan penutupan lanjutan

Setelah bagian 9, H2–H6 diaudit ulang dengan membaca kode (tiga audit read-only per kelompok). Temuan yang nyata diperbaiki; sisanya dicatat di bawah sebagai keputusan atau batas.

Perubahan perilaku dan breaking changes:

- **Iterasi 3, `others_speaking`.** Tracker suara (`VoiceActivityTracker.others_speaking`) menandai peserta lain yang sedang bicara: suara jarak jauh dalam jendela 500 ms (`OTHERS_SPEAKING_HANGOVER_MS`) yang tidak sepenuhnya tertutup suara twin sendiri. Di mode policy, twin diam (`floor_occupied`) selama ada yang bicara. Sinyal dipasang lewat `LoopInteraction.others_speaking` dan `LoopBehavior.others_speaking`. `ambiguous` tetap `False`: addressed turn selalu dijawab, sesuai keputusan yang disetujui.
- **H2 2.5.** Pemutusan userinfo dan query diterapkan ke setiap nilai setting yang berbentuk URL (`://`), bukan hanya kunci `base_url`/`url`. `memory.store.uri` ikut tersamarkan.
- **H2 2.1.** MCP browser berjalan tanpa `--caps vision`. Capability itu hanya membuka tool mouse berkoordinat, yang sudah tidak diizinkan allowlist. `@playwright/mcp` 0.0.82 tidak punya flag penyaring tool per nama, sehingga `browser_run_code_unsafe` tetap terdaftar di server; kontrol utamanya tetap allowlist di sisi klien.
- **H3 3.2.** Checkpointer memakai `AsyncConnectionPool` (min 1, maks 4) dengan `check_connection` dan opsi koneksi yang sama dengan sebelumnya. Koneksi yang putus diganti saat checkout. Dibuktikan dengan tes DB yang mematikan backend lalu menulis ulang checkpoint.
- **H3 3.5.** Persiapan lobi (`PREPARE_TIMEOUT_SECONDS` 120 s) masuk anggaran join, dengan batas sendiri. Kegagalan persiapan memberi `failed` ("lobby preparation did not finish"), bukan `timeout`. Pembacaan status panggilan (`CALL_STATE_TIMEOUT_SECONDS` 5 s) memberi `unknown` saat macet. Probe captcha dibatasi 2 s dan dianggap tidak ada captcha, sehingga halaman yang menampilkan penolakan menjadi `blocked`. Pembacaan `content()` pada loop echo dibatasi 2 s per pembacaan.
- **H3 3.7.** `docker compose run -d` memakai anggaran sendiri `FAKEYOU_WORKER__START_TIMEOUT_SECONDS` (default 600 s), bukan tanpa batas.
- **H3 3.9.** `fakeyou chat` memakai `HistoryTrimMiddleware` dan `OrphanToolCallMiddleware`, sama seperti jalur meeting.
- **H3 3.10.** Error dari deadline teardown tidak lagi menggantikan error body yang sedang berlangsung.
- **H3 3.13.** (a) Timeout sintesis TTS berlaku per chunk, sehingga konsumen yang lambat tidak dihitung sebagai sintesis macet. (b) Binary dan lisensi CloakBrowser diresolusikan di thread sebelum launch. (c) Anggaran respons berlaku per kalimat, bukan untuk seluruh balasan termasuk waktu bicara. Jalur pregenerasi tetap dibatasi per respons. (d) `idle_reconnect_seconds` ≤ 86 400 dan `diarize_max_retries` ≤ 10.
- **H4.** `classify_exception` memberi fault dengan detail `internal:<Tipe>` untuk `AssertionError`, `AttributeError`, `IndexError`, `KeyError`, dan `TypeError`. Fault itu tidak lagi tampil sebagai provider yang tak dikenal. Loop tetap merekam dan berjalan, sesuai kontrak resiliensi playback dan monitor. Drop jurnal ditandai satu baris stderr pada kegagalan pertama dan tetap dihitung. Kegagalan reporter fault dicatat sebagai `fault_report_failed`, bukan dibuang.
- **H5.** Akses `getattr` dengan nama untuk kapabilitas opsional diganti `isinstance` terhadap tujuh `Protocol` yang dapat diperiksa saat runtime, di `fakeyou/foundation/protocols.py`. Empat sisi batas tetap `getattr`: atribut error SDK, atribut frame pipecat, jalur dotted di konfigurasi, dan nama origin di env catalog.
- **H5 deptry.** `psycopg-pool` dideklarasikan langsung di `pyproject.toml`. `uv.lock` diperbarui offline; hanya dua entri yang berubah, tanpa perubahan versi.
- **H6 CLI tipis.** `cli/join.py` tinggal 87 baris dan `cli/diarize.py` 20 baris. Logika pindah ke `orchestration/join_preflight.py` (preflight dan gate platform), `orchestration/join.py` (`resume_command`), `catalog/commands.py` (`memory_ingest_args`), dan `diarization/diarize_service.py` (`sidecar_turn_count`, `diarize_status`). `run_join_session` kini menjalankan preflight sendiri, sehingga pemanggil langsung juga divalidasi.
- **Dokumentasi.** Kontrak `forbidden` diperjelas (hanya `catalog` dan `db`). Daftar pengecualian lint mencantumkan `journal.py`. Aturan nama privat dan `getattr` diperjelas.

Keputusan yang menyimpang dari audit atau rencana:

- `classify_exception` tidak melempar ulang bug. Percobaan awal melakukannya, tetapi itu merusak kontrak resiliensi playback dan monitor. Diganti dengan pelabelan `internal:`.
- Batas respons per kalimat menggantikan batas seluruh balasan di jalur streaming. Batas per fetch tidak lagi membatasi total durasi balasan, tetapi tiap fetch tetap dibatasi.
- Checkpoint pool memakai konstanta `CHECKPOINT_POOL_MAX_SIZE = 4`, bukan setting baru.

Batas yang masih ada:

- Batas deadline teardown hanya diperiksa di antara callback. Overshoot paling banyak sebesar satu anggaran callback (`close_callback_seconds`).
- `audio_enabled` di CLI tetap dihitung di CLI (`options.audio_enabled or settings.worker.audio`). Memindahkannya mengubah semantik pemanggil langsung.
- `db/schema.py` tetap memakai `Path(__file__)` karena alembic memerlukan direktori migrasi di filesystem.
- Dua decoder PCM (`decode_pcm16` dan `pcm16_to_floats`) menghasilkan tipe berbeda, bukan duplikat.
- Live acceptance Meet belum dijalankan.

Verifikasi (dijalankan setelah semua perubahan di bagian ini):

- Default: 8197 lulus, 82 dilewati, 67 tidak dipilih. DB mode: 8236 lulus, 43 dilewati.
- Static: ruff check dan format (536 file), mypy (418 file), lint-imports (4 kontrak), deptry bersih.
- Container: image runtime dan test dibangun ulang. `pytest -m container` 6/6, `fakeyou check` exit 0, probe canary 3 ada dan 0 bocor, pulse + transport 2/2. Catatan: build runtime pertama gagal diam-diam; image `:local` lama terdeteksi dan semua hasil container diulang setelah build berhasil.
