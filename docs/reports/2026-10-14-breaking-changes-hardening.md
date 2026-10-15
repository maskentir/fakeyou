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

## 11. Iterasi 4: satu aturan addressing untuk dua mode

Pemicu: sesi live Meet `kyf-live-3` (twin Ugi, mode `--converse` dengan nama twin). Dari sembilan turn skrip standup, dua dijawab padahal seharusnya diam:

- "Eh tunggu bukan itu" dijawab "Oh, maksud lo yang mana tuh?". Jendela aktivasi diperpanjang oleh turn sebelumnya yang dijawab twin, sehingga interjeksi lolos.
- "Oke" dijawab "Oke, sip." sesaat setelah twin menjawab "makasih". Ack yang berdiri sendiri selalu dijawab.

Speaker sudah teratribusi (`speaker-a` pada 7 dari 8 transkrip), jadi masalahnya bukan identitas pembicara. Yang kurang adalah aturan isi: ack dan interjeksi dari lawan bicara yang sama juga tidak meminta jawaban.

### Perubahan perilaku

- **Satu fungsi keputusan.** `choose_participation` di `foundation/participation.py` adalah satu-satunya aturan. Mode `participant_policy_enabled` dan mode nama twin sama-sama memanggilnya lewat `ConversationLoop._decide`. Sebelumnya ada dua jalur (`_addressed` dan `_policy_allows`) dengan aturan yang berbeda; keduanya dihapus.
- **Jendela diperpanjang oleh jawaban, bukan oleh nama saja.** Setiap turn yang dijawab twin memperpanjang `activation_seconds`. Turn yang diam tidak memperpanjang jendela.
- **Interjeksi diam.** Kata pembuka seperti `eh`, `tunggu`, `bentar`, `nanti dulu`, `stop`, `bukan` (daftar `interjections` di `ack_vocabulary.json`) tidak dijawab di dalam jendela. Bila twin sedang bicara, barge-in tetap menghentikan playback.
- **Ack hanya dijawab bila twin baru bertanya.** `bare_ack` di dalam jendela dijawab hanya bila kalimat terakhir yang diucapkan twin diakhiri `?` (`awaiting_reply`). Setelah pernyataan, ack diam.
- **Lawan bicara yang lain diam.** Di mode nama twin, turn dari pembicara yang diketahui berbeda dari pembicara yang sedang dilayani tidak dijawab. Speech tanpa atribusi tetap boleh melanjutkan jendela, karena diarization tidak selalu tersedia.
- **Mode policy tetap ketat.** Di mode policy, turn tanpa atribusi tidak mewarisi jendela. Ini perilaku yang sudah ada dan diuji (`test_policy_unknown_followup_*`).
- **Label journal `policy_decided` dipakai di kedua mode.** Alasan yang mungkin: `addressed`, `followup_same_speaker`, `followup_answers_question`, `bare_ack`, `interjection`, `not_addressed`, `floor_occupied`. `ack_decided` dengan `bare_ack_run` hanya muncul di mode tanpa nama.

### API internal

- Dihapus: `ConversationLoop._addressed`, `_policy_allows`, `_window_speaker`, `_last_speaker_id`.
- Baru: `ConversationLoop._decide`, `_note_spoken`, `_engaged_speaker`, `_awaiting_reply`.
- Baru: `is_other_observed_speaker` di `foundation/participation.py` (keduanya diketahui dan berbeda).
- `ParticipationContext` mendapat `bare_ack`, `interjection`, `awaiting_reply` dengan default `False`, sehingga pemanggil lama tetap valid.

### Tes

- RED lebih dulu: `tests/test_participation.py` (interjeksi, ack setelah pernyataan, ack setelah pertanyaan, addressed tidak dibungkam, `is_other_observed_speaker`).
- `tests/test_conversation_policy.py`:
  - `test_bare_acks_after_a_statement_stay_silent_inside_the_window` menggantikan `test_addressed_text_replies_inside_wake_window_ack_runs`. Tes lama mengharapkan ack dijawab; itu perilaku yang kini diganti.
  - `test_bare_ack_answers_a_question_the_twin_just_asked` baru.
  - `test_bare_ack_inside_the_window_is_not_answered` memakai pernyataan sebagai turn pertama. Versi lama memakai pertanyaan, sehingga jawaban twin ikut bertanya dan `oke` memang dijawab.
  - `test_live_turns_replay_with_the_labelled_answers_and_reasons` mereplay delapan turn live dari `tests/fixtures/addressing-labels-kyf-live-3.json`. Setiap turn diberi label jawab/diam dan alasan `policy_decided`. Turn live berikutnya cukup ditambahkan ke fixture tanpa mengubah tes.
- Kosakata interjeksi dan pertanyaan tanpa nama ada di `tests/fixtures/ack-cases.json`. File Python tidak boleh memuat kata Indonesia (`test_code_rules.py`), jadi teks uji Indonesia dibaca dari fixture.
- Hasil: `tests/test_participation.py`, `test_conversation_policy.py`, `test_conversation_streaming.py`, `test_conversation_core_journal.py`, `test_cli_join_audio.py`, `test_report_session.py`: 228 lulus.

### Celah yang belum ditutup

- Aturan ini masih heuristik berbasis kosakata dan tanda `?`. Ia tidak memahami isi. Pertanyaan retoris dari lawan bicara atau interjeksi di tengah kalimat panjang bisa salah klasifikasi.
- Belum diuji live. Worker `kyf-live-3` masih menjalankan kode sebelum iterasi ini, dan restart membuat twin keluar dan masuk meeting lagi.
- Langkah berikutnya yang sudah dipertimbangkan: gerbang addressee berbasis model kecil yang membaca beberapa turn terakhir dan memutuskan `reply`, `silent`, atau `yield`. Ia akan menggantikan kosakata, tetapi menambah satu langkah sebelum jawaban. Latensinya harus diukur, dan keputusannya harus dievaluasi terhadap korpus transkrip live (`record.jsonl`), sebelum dipakai.

## 12. Iterasi 5: kegagalan giliran, barge-in default, dan koneksi putus

Pemicu: audit read-only atas bagian twin di luar addressing. Temuan diverifikasi terhadap kode sebelum dikerjakan. Yang diambil dulu adalah tiga celah terbesar terhadap rasa manusiawi: satu error model membuat twin diam sampai akhir panggilan, barge-in mati secara default sehingga twin berbicara di atas lawan bicara, dan twin tetap bicara setelah koneksi putus.

### Perubahan perilaku

- **Satu giliran yang gagal tidak mengakhiri percakapan.** Timeout, error provider, atau bug di satu giliran membuat twin mengucapkan `turn_failed` dari `src/fakeyou/spoken_lines.json` (`id` atau `en`, mengikuti bahasa wicara), lalu tetap mendengarkan. Journal mencatat `turn_failed` dengan tipe error dan urutan kegagalan. Fault tetap direkam.
- **Batas beruntun.** Setelah `MAX_CONSECUTIVE_TURN_FAILURES` (3) giliran gagal berturut-turut, percakapan gagal seperti sebelumnya dan runtime mencatat `conversation_disabled`. Giliran yang berhasil mereset hitungan. Kegagalan saat memutar kalimat fallback langsung mengakhiri percakapan, karena audio yang gagal terus juga harus berhenti.
- **Barge-in default ikut `--converse`.** `--barge-in` dan `--no-barge-in` berlaku untuk `join` dan `worker start`. Tanpa `--converse`, barge-in tetap nonaktif. `--barge-in` eksplisit tanpa `--converse` tetap ditolak.
- **Koneksi putus menahan bicara.** Saat lobby muncul setelah admitted, percakapan di-pause: playback dihentikan, transkrip baru dibuang (`transcript_suppressed`, alasan `paused`), dan balasan yang sedang dibuat dibuang (`turn_suppressed`). Saat tersambung lagi, percakapan di-resume.

### API internal

- Baru: `ConversationLoop._recover_turn`, `ConversationLoop.pause()` (async), `ConversationLoop.resume()`, `MAX_CONSECUTIVE_TURN_FAILURES`, `TURN_FAILED_LINES`, `_resolve_barge_in` di `cli/main.py`.
- `MeetingConversation` (Protocol) mendapat `pause` dan `resume`. Implementasi test (`FakeConversation`) ikut diperbarui.
- `MeetingWorker._apply_observation` menjadi `apply_observation`, publik seperti `disable_conversation`.
- Breaking: default `--barge-in` berubah bila `--converse` aktif, dan kegagalan satu giliran tidak lagi sama dengan kegagalan percakapan.

### Tes

- RED dulu: `tests/test_conversation_recovery.py` (4), `tests/test_conversation_pause.py` (3), `tests/test_runtime_connection.py` (1), `tests/test_cli_barge_in_default.py` (8).
- Tes lama yang mengharapkan satu error mengakhiri percakapan diubah dan disebut satu per satu: `test_conversation_core_journal.py` (3), `test_conversation_core_adoption.py` (1), `test_conversation_streaming.py` (1), `test_conversation_pregen_adoption.py` (1), `test_conversation_pregen_journal.py` (1). Setiap perubahan kini menguji streak atau fallback.

### Verifikasi

- Ruff check dan format (540 file), mypy (422 file), lint-imports (4 kontrak), deptry: bersih.
- Default: 8256 lulus, 82 dilewati, 0 gagal. DB mode: 8295 lulus, 43 dilewati.
- Image runtime dibangun ulang dan kode baru dicek ada di dalamnya. `pytest -m container` 6/6, `fakeyou check` exit 0, pulse + transport 2/2 di image test.

### Yang sudah diukur dan tidak dikerjakan

- **Latensi.** Di live `kyf-live-3`, waktu dari transkrip final sampai suara pertama adalah 4,7 sampai 13,5 detik (median sekitar 6,6 detik). Target dokumen adalah median 3 detik. Streaming kalimat tidak akan menolong: `docs/plans/sentence-streaming.md` mengukur sekitar 94% waktu terjadi sebelum teks pertama keluar, sehingga penghematannya sekitar 0,07 detik. Lever yang tersisa adalah time-to-first-token model (reasoning dan leg tool) dan filler suara yang dimulai segera. Belum dikerjakan, karena menyangkut pilihan model dan aset suara.
- **Suara orang pertama.** Aturan persona memaksa kalimat seperti "the owner needs to confirm it", dan melarang janji. Itu melindungi dari klaim palsu. Mengubah twin menjadi bicara sebagai pemilik mengubah model identitas dan kejujuran, jadi itu keputusan produk dan belum diubah.
- **Diam total setelah tiga kegagalan.** Gangguan provider yang berlangsung lebih dari tiga giliran tetap membuat twin diam untuk sisa panggilan. Runtime tidak membuat ulang percakapan. Ini membedakan gangguan dari bug yang berulang, tetapi berarti diam setelah tiga kegagalan.
- **Rejoin otomatis belum ada.** Pause hanya menahan bicara. Jika panggilan tidak kembali, twin tetap tidak bicara.
- **Belum diuji live.** Worker `kyf-live-3` masih berjalan dengan kode sebelum iterasi ini. Pengujian live memerlukan restart, dan twin harus di-admit ulang.

## 13. Iterasi 6: pondasi provider TTS

Pemicu: permintaan agar provider TTS bisa diganti tanpa tambalan, dengan Cartesia dan ElevenLabs sekarang dan open-source menyusul. Desain lengkap ada di `docs/plans/tts-provider-switching.md`.

### Breaking change

- `audio/voice.py` (STT) dipindah ke `fakeyou/transcription/muse.py`. Adapter TTS pindah ke `fakeyou/speech/`: `contract.py`, `streaming.py`, `languages.py`, `registry.py`, dan satu file per provider.
- `SpeechSynthesizer` pindah dari `audio/playback.py` ke `speech/contract.py`.
- `create_speech_synthesizer` diganti `build_speech_synthesizer`. Pemilihan adapter memakai `match` dengan `assert_never`, sehingga provider yang tidak terdaftar gagal di mypy.
- `OpenAIVoiceStyle` dihapus, beserta parameter `style` dan `speed` yang tidak dipakai.
- `journaled_stream` menerima `SpeechTag` (provider, indeks kalimat, id transkrip). Event `synthesis_*` dan `synthesis_pcm_emitted` memuat field `provider`.
- Settings TTS: empat kelas (OpenAI, MiniMax, Cartesia, ElevenLabs) dalam union yang didiskriminasi `provider`. Nama variabel env tidak berubah.
- Settings memvalidasi `speech.language` terhadap `tts.languages` saat load. Konfigurasi yang tidak didukung provider gagal lebih awal.
- Catalog env: baris `FAKEYOU_TTS__PROVIDER` dan `FAKEYOU_REPLY__PROVIDER` digabung menjadi satu baris dengan semua nilai. Sebelumnya setiap kelas menjadi baris sendiri.
- Tes TTS dipecah menurut subjek: `test_openai_speech.py`, `test_minimax_speech.py`, `test_streaming_speech.py`, `test_registry_conformance.py`, `test_cartesia_speech.py`, dan `test_elevenlabs_speech.py`.

### Verifikasi

- Cartesia diverifikasi langsung lewat env dan registry. Provider dipilih dari environment dan mengalahkan `.env`. Key tidak dicetak.
- ElevenLabs: tes dengan fake transport saja.
- Default: 8368 lulus, 82 dilewati, 0 gagal (sebelum perbaikan tes kecil terakhir; diulang setelah itu).
- Ruff, format, mypy (437 file), lint-imports (4 kontrak, termasuk lapisan `speech`), dan deptry bersih.

### Belum

- Open-source sidecar, profil suara per twin, `fakeyou voice test`, fallback per kalimat, dan penyimpanan key per profil.
- Header `xi-api-key` dan `language_code` ElevenLabs perlu verifikasi dengan key nyata.

## 14. Iterasi 7: kontrak STT dan keepalive Muse

Pemicu: permintaan agar STT Muse menjadi provider yang mengikuti kontrak, dengan jaminan yang benar di rapat sungguhan. Desain ada di `docs/plans/stt-provider-contract.md`.

### Breaking change

- `audio/voice.py` dihapus. Isinya pindah ke `transcription/muse.py` (`MuseSTTOptions`, `create_muse_stt`, `ManagedMuseSTT`).
- `audio/audio.py` tidak lagi memuat `ManagedMuseSTT`, `STT_*`, atau `_bounded_frames`.
- `TranscriptionFrame` pipecat tidak lagi dipakai oleh konsumen. Konsumen menerima `TranscriptUpdate` (`foundation/transcript.py`). Konversi frame terjadi di `muse_update`. Field `transport_source`, `capture_seq`, dan `capture_ms` yang dulu ditempel ke frame sekarang field eksplisit.
- `transcript_event` menerima `TranscriptUpdate`. Identitas event dihitung dari `timestamp` atau `stream_id:capture_seq`.
- `create_muse_stt(api_key, options, service_class, journal)`. `url` dan `sample_rate` pindah ke `MuseSTTOptions`.
- `STT_CONTINUITY_BUFFER_SECONDS = 30` diganti `MUSE_MAX_BACKLOG_SECONDS = 4`. Nilai 30 detik menyebabkan burst setelah rekonek yang melanggar batas backlog Muse.
- Event journal baru: `stt_audio_dropped` (`dropped_ms`, `reason`) dan `stt_keepalive_started` (`idle_ms`).
- `TranscriptionError` menggantikan `MeetingAudioError` untuk kegagalan rekonek STT.
- Layer `fakeyou.transcription` ditambahkan ke kontrak import, di bawah `audio`.
- File tes: `test_voice.py` menjadi `test_muse.py`, dan `test_voice_muse.py` menjadi `test_muse_stt.py`.

### Verifikasi

- Tes live `test_muse_endpointing_transcribes_realtime_wav` lulus dengan WAV bahasa Inggris dari voice clone, tanpa silence di ujung audio.
- Default: 8392 lulus, 82 dilewati, 0 gagal. DB mode: 8431 lulus, 43 dilewati.
- Ruff, format, mypy (439 file), lint-imports (4 kontrak, termasuk `transcription`), dan deptry bersih.
- Image runtime dibangun ulang dan paket `transcription` terverifikasi ada di dalamnya. `pytest -m container` 6/6, `fakeyou check` exit 0, pulse dan transport di image test 2/2.

### Belum

- Provider STT kedua dan registry STT.
- Tes rapat live penuh (menunggu link Meet).

## 15. Lepas Pipecat

Pemicu: Pipecat dipakai di tujuh file produksi, tetapi hanya sebagian kecil APInya. Tiga masalah terbukti di kode dan probe live: label speaker Muse tidak sampai ke frame, rekonek dimiliki dua pihak, dan pre-roll menggandakan audio ke STT. Desain dan urutan kerja ada di `docs/plans/drop-pipecat.md`.

### Breaking change

- Dihapus: `PreRollBuffer`, `CaptureObserver`, `SessionPipeline`, `native_transport`, `ManagedMuseSTT`, dan `create_muse_stt`. Juga `LocalAudioTransport` (pyaudio), `MetaSTTService` sebagai basis STT, enum `Language`, impor `VALID_VOICES`, dan pemetaan `ErrorCategory`.
- Diganti: `match_endofsentence` menjadi `sentencex` di `foundation/sentences.py`. Volume memakai `loudness` di `audio/vad.py`. `SPEECH_LANGUAGES` berbasis string (`speech/languages.py`). Klasifikasi error memakai `ProviderFault` yang sudah ada.
- `MeetingAudioSession` menerima `run: SessionRun`, menggantikan argumen `pipeline`.
- `StreamAudioInput` tidak lagi mewarisi `BaseInputTransport`.
- `TranscriptUpdate.speaker` dihapus. Gantinya `voice_clusters: tuple[str, ...]`, yang berisi label cluster per sesi.
- Setting `STTSettings.preroll_ms` (env `FAKEYOU_STT__PREROLL_MS`, default 500.0) dihapus.
- Default `FAKEYOU_STT__MODE` berubah dari `ENDPOINTING` ke `DIARIZATION`. `ENDPOINTING` tetap nilai yang valid.
- Event journal Muse diganti nama: `stt_socket_lost` menjadi `stt_socket_failed`, `stt_speaker_after_turn` menjadi `stt_speaker_dropped`, `stt_empty_turn` menjadi `stt_turn_skipped`, `stt_final_without_turn` menjadi `stt_final_observed`, `stt_unhandled_message` menjadi `stt_message_skipped`, dan `stt_unparsable_message` menjadi `stt_message_dropped`. Pencarian di journal lama memakai nama lama.
- Extra `audio` (`pipecat-ai[local]`, pyaudio) dihapus dari `pyproject.toml`. pyaudio dan portaudio tidak lagi dipasang.
- Dependensi `pipecat-ai[silero,webrtc]` dihapus. Dependensi langsung baru: `onnxruntime~=1.24.3`, `loudness>=0.2,<1`, `sentencex==1.0.31`, dan `websockets>=16,<17`.
- `uv.lock` diregenerasi.
- Model `silero_vad.onnx` (2,3 MB, MIT) di-vendor di `src/fakeyou/audio/models/` dengan README yang mencatat sumber, versi, dan sha256.

### Verifikasi

- VAD cocok per jendela dengan golden Pipecat 1.12 (`tests/fixtures/vad-golden-standup-16k.json`). Selisih confidence dan volume paling besar 1e-5, dan state serta urutan event identik.
- Probe DIARIZATION pada klip 22,5 detik: label `A, B, A, B`, dan label datang sebelum final dalam burst yang sama. Lihat [kontrak STT](../plans/stt-provider-contract.md#2-bukti-live-diarization).
- Tes live `MUSE_DIARIZATION_AUDIO` (`tests/test_muse_client.py`) tercatat lulus pada S1.
- Pemeriksaan statis: `pipecat` tidak ada di `pyproject.toml` dan `uv.lock`. `src/` tidak mengimpor `pipecat`, kecuali README model yang mencatat sumber.
- Gate penuh (ruff, format, mypy, lint-imports, deptry, tes default dan container): lihat gate.

### Belum

- Capture `parec` di container dengan `PULSE_SOURCE` hidup.
- Rapat Google Meet sungguhan.
- ElevenLabs dengan key sungguhan.
- Sesi Muse 60 menit. Meta menyebut close code 1011; belum diuji live.
- Sisa yang ditemukan saat pemeriksaan:
  - Empat modul tes masih mengimpor `pipecat` (`tests/test_audio_failures.py`, `test_audio_session.py`, `test_audio_shutdown.py`, `test_audio_stt_reconnect.py`). Di instalasi tanpa Pipecat, modul ini gagal saat dikumpulkan.
  - `orchestration/join_preflight.py` masih mensyaratkan `find_spec("pyaudio")` untuk join dengan audio. Di instalasi tanpa pyaudio, join itu ditolak.
  - `docs/runbooks/verification.md` dan `docs/reference/code-standard.md` masih menyebut Pipecat.
