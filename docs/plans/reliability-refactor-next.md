# Penyelesaian refactor keandalan dan milestone berikutnya

> **Untuk agentic worker:** SUB-SKILL WAJIB: Gunakan superpowers:subagent-driven-development (disarankan) atau superpowers:executing-plans untuk mengimplementasikan rencana ini task demi task. Langkah memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Tujuan:** Selesaikan refactor keandalan yang terputus (9 tes merah, 1 suite lambat) dan tuntaskan setiap temuan B0–B10 sampai gate hijau.

**Arsitektur:** Pertahankan arah Codex siang hari (percakapan degrade-and-continue, preflight yang harus lulus seluruhnya, evaluasi deaf yang dimiliki sesi audio, kapabilitas streaming yang dormant); perbaiki setiap edit setengah jadi sampai sesuai kontrak yang dipin; ganti timer unknown 180 detik dengan 2 kegagalan observasi berturut-turut.

**Stack teknologi:** Python 3.12, Pipecat 1.12.0, pytest, mypy --strict, ruff.

**Spesifikasi:** Temuan Vexa/LMA B0–B10 (pesan pengguna 2026-09-29) ditambah empat rencana sebelumnya: [meeting-runtime-plan.md](meeting-runtime.md), [audio-runtime-plan.md](audio-runtime.md), [conversation-plan.md](conversation.md), [implementation-plan.md](implementation.md).

## Batasan global

- Tes yang sudah di-commit adalah kontrak; perubahan ekspektasi memerlukan alasan yang dinyatakan dan terkait dengan temuan.
- `uv run pytest` (marker default), `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, dan `docker compose --profile production config --quiet` semuanya harus lulus.
- Jangan pernah commit, push, atau tag; biarkan pekerjaan tidak di-commit untuk review.
- Tidak ada panggilan provider live pada verifikasi unit; acceptance live tetap menjadi milestone terpisah yang di-gate.

---

## Kondisi awal (diverifikasi 2026-09-29)

- Pagi: 451 lulus, 14 deselected, semua gate statis hijau.
- Siang: parent + 3 subagent review mengedit secara bersamaan, semuanya mati karena batas penggunaan di tengah edit.
- Sekarang: 442 lulus, 9 gagal, suite memakan waktu ~196s (satu tes menghabiskan 180 detik nyata).

## Keputusan kontrak (terkunci)

1. Preflight menolak kecuali setiap probe berhasil; alasan penolakan tetap `infra_failure` (B9 alasan infra eksplisit, B8 exit 3). Probe campuran membuat preflight menolak.
2. Satu kegagalan observasi UI ditoleransi; 2 kegagalan observasi berturut-turut mengakhiri sesi sebagai `browser_failure`. State `unknown` yang sah mempertahankan perilaku HEAD (ditoleransi, tanpa transisi).
3. Loop percakapan yang gagal menonaktifkan jawaban dan tetap mendengarkan; isolasi callback transkrip tetap berada di `_TranscriptObserver`.
4. Evaluasi deaf berada di sesi audio (`maintenance_tick`); seam `deaf_monitor` yang diinjeksi tetap dihapus.
5. Preflight meneruskan hostname (`preflight_hosts`); nama kwarg, pin CLI, dan semantik DNS+TLS B9 selaras.
6. Streaming TTS tetap menjadi kapabilitas dormant yang diuji unit; tidak ada flag CLI sampai verifikasi end-to-end audio Linux tersedia.
7. Masalah saat leave (exception atau status tidak berhasil) dipetakan ke `browser_failed`; event `leave_failed` tetap dihapus.

## Bagian A: task rekonsiliasi

### Task 1: pemetaan leave kembali ke `browser_failed`

**File:**
- Ubah: `src/fakeyou/runtime.py` (`finish`, jalur exception + jalur status)
- Ubah: `src/fakeyou/lifecycle.py` (hapus kind `leave_failed`, transisi, cabang apply)

- [x] Kirim `browser_failed` (bukan `leave_failed`) saat `leave_meeting` melempar exception dan saat statusnya bukan `left`.
- [x] Jalankan: `uv run pytest tests/test_runtime.py -q -k 'leave_failure or leave_crash'`
- [x] Diharapkan: 2 lulus (pin yang sudah di-commit dipulihkan).

### Task 2: kegagalan observasi berturut-turut dieskalasi, satu blip ditoleransi

**File:**
- Ubah: `src/fakeyou/runtime.py` (`monitor`: ganti blok `unknown_since`/180s dengan penghitung kegagalan berturut-turut pada `checked.observation_failed`, threshold 2, reset saat pemeriksaan bersih)
- Tes: `tests/test_runtime.py` (tambahkan `test_run_meeting_session_tolerates_single_observation_blip`: halaman `[IN_CALL, RuntimeError, IN_CALL, ENDED]` berakhir `meeting_ended`)

- [x] Implementasikan penghitung; state `unknown` yang sah tidak boleh menaikkannya.
- [x] Jalankan: `uv run pytest tests/test_runtime.py tests/test_runtime_audio.py -q`
- [x] Diharapkan: semua lulus, suite kembali di bawah ~30s (tidak ada 180s yang terbuang).

### Task 3: tes deaf lewat jalur maintenance kanonis

**File:**
- Ubah: `tests/test_runtime_audio.py` (hapus `_ScriptedDeafMonitor`; beri fake `Audio` sebuah `maintenance_tick(is_busy, remote_participants)` berskrip sehingga menjadi `AudioMaintenance`; override `observe` per tes agar tidak men-set stop)

- [x] Tes kedaluwarsa: tick 1 meminta reconnect dan mengembalikan `silent`; tick 2 melempar `MeetingAudioError("deaf: persistent stt_fault")`; assert `audio_failure`, detail diawali `deaf:`, satu permintaan reconnect, jumlah tick >= 2.
- [x] Tes perbaikan: tick 1 meminta reconnect; tick 2 men-set stop dan mengembalikan `hearing`; assert `admitted_then_stopped`.
- [x] Jalankan: `uv run pytest tests/test_runtime_audio.py -q -k deaf`
- [x] Diharapkan: 2 lulus.

### Task 4: pulihkan fallback pemberian journal

**File:**
- Ubah: `src/fakeyou/conversation.py` (pulihkan `_with_journal` + import `is_dataclass`/`replace`/`cast`; `self._responder = _with_journal(responder, journal)`)

- [x] Responder dengan journal eksplisit mempertahankannya; responder dataclass dengan journal `None` menerima journal milik loop; selain itu diteruskan tanpa diubah.
- [x] Jalankan: `uv run pytest tests/test_conversation.py -q`
- [x] Diharapkan: semua lulus, termasuk `test_loop_passes_journal_through_to_responder`.

### Task 5: hostname preflight

**File:**
- Ubah: `src/fakeyou/cli.py` (`provider_probe_targets` dikembalikan ke `provider_probe_hosts`, `_PREFLIGHT_TARGETS` dikembalikan ke `_PREFLIGHT_HOSTS`)

- [x] Jalankan: `uv run pytest tests/test_cli.py -q -k 'preflight_hosts or conversation_receives_journal'`
- [x] Diharapkan: 2 lulus.

### Task 6: hapus flag streaming yang setengah terintegrasi, cakup streaming dormant

**File:**
- Ubah: `src/fakeyou/cli.py` (hapus flag `--stream-tts`, validasi, pass-through di `main`, kwarg partial `streaming_tts` — `open_meeting_audio` tidak memiliki parameter tersebut)
- Tes: `tests/test_playback.py` (tambahkan happy path streamed-speak dengan fake stream synthesizer + stream player; tambahkan kasus stream-play-failure yang meng-emit `speak_failed` dan mencatat fault)

- [x] Jalur default non-streaming tidak berubah (`streaming=False`).
- [x] Jalankan: `uv run pytest tests/test_playback.py tests/test_cli.py -q`
- [x] Diharapkan: semua lulus.

### Task 7: regresi playback

**File:**
- Ubah: `src/fakeyou/playback.py` (unpack `_, stderr = await proc.communicate(audio)`; `_unmute` kembali ke play-through: emit `mute_failed`, catat fault, lanjutkan playback)

- [x] Jalankan: `uv run pytest tests/test_playback.py -q`
- [x] Diharapkan: semua lulus, termasuk tes mute-failure-continues dan nonzero-exit.

### Task 8: hapus field progres STT yang mati

**File:**
- Ubah: `src/fakeyou/deaf.py` (hapus `last_stt_progress_at` dari snapshot, state aktivitas, stamping, dan konstruksi snapshot — di-stamp tetapi tidak pernah dibaca)

- [x] Pertahankan urutan capture-fault sebelum alone (temuan: capture-fault menahan left-alone).
- [x] Jalankan: `uv run pytest tests/test_deaf.py tests/test_audio.py -q`
- [x] Diharapkan: semua lulus.

### Task 9: gate penuh

- [x] Jalankan: `uv run pytest -q`, `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, `FAKEYOU_NEO4J_PASSWORD=ci-config-check-only docker compose --profile production config --quiet`
- [x] Diharapkan: 451+ lulus dengan 0 gagal, mypy bersih pada 22+ file, ruff bersih, compose OK.

## Bagian B: milestone berikutnya (setelah Bagian A hijau)

### M1: acceptance percakapan live (memerlukan Meet yang di-host)

- [ ] Bergabung ke Meet yang di-host pengguna dengan `--audio --converse` dan durasi singkat; ajukan dua pertanyaan berkaitan; pastikan kedua jawaban terdengar dan jawaban kedua memakai konteks giliran pertama.
- [ ] Pastikan transkrip, tape journal (`.runtime/journals/<meeting>.jsonl` dengan pasangan `synth_*`/`agent_invoke`), dan hasil terminal konsisten satu sama lain.
- [ ] Stop di tengah jawaban; pastikan tidak ada ucapan terlambat, lifecycle berakhir (ended), dan tidak ada container tersisa.

### M2: acceptance keandalan live

- [ ] Penolakan preflight: arahkan worker ke jaringan yang tidak dapat dirutekan (atau blokir host provider) dan pastikan `infra_failure` dengan exit code 3 sebelum browser apa pun dibuka.
- [ ] Terminasi deaf: bergabung dengan kredensial STT yang dicabut dan pastikan sesi berakhir `audio_failure` (deaf) dalam masa grace alih-alih diam sampai durasi habis.
- [ ] Laporan fault: pastikan output terminal memuat ringkasan `faults` setelah error provider yang nyata.

### M3: streaming TTS end-to-end (P2)

- [ ] Perkenalkan kembali flag CLI hanya bersama verifikasi audio Linux: jawaban streaming terdengar utuh lewat `paplay`, journal menampilkan `streaming=True` dengan latency first-chunk, dan generation drop tetap menginterupsi dengan bersih.

### M4: deployment dan CI

- [ ] Jalankan workflow CI GitHub yang sesungguhnya secara end-to-end; jadwalkan worker meeting (batas durasi, rotasi journal lintas meeting, stop jarak jauh).
