# Plan nama speaker dari CSRC

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer terpisah dari penulis.

**Tujuan:** label suara per orang ("Speaker A") yang sudah stabil lewat CSRC diberi nama display dari roster Meet bila buktinya kuat. Nama dipakai sebagai klaim tampilan yang tidak terverifikasi di record, ingestion, dan debrief. Nama yang salah lebih buruk daripada label anonim.

**Status:** rencana, 5 Oktober 2026. Basis `master` pada `92c05e8`. Buat branch `feat/speaker-names`. Jangan push/merge.

**Keputusan pengguna (5 Oktober):** milestone berikutnya adalah nama speaker dari CSRC (dipilih atas latency, tindakan Meet, dan worker Kubernetes).

## Dasar

- [Riset CSRC](../reports/2026-10-05-csrc-research.md):
  - Meet membawa CSRC peserta di metadata frame terenkode.
  - Tiap slot berisi satu CSRC peserta, dan CSRC stabil per join.
  - Label CSRC live mendapat 13/13 solo benar dan overlap 2/2 `unknown`.
- [Laporan label suara](../reports/2026-10-05-voice-labels.md):
  - Binder slot ↔ device roster lewat eliminasi menghasilkan presisi nama 0,971 pada probe-attr-8, di bawah gate 0,98.
  - Eliminasi murni gagal bila twin masuk setelah semua orang sudah ada, dan itu kasus paling umum.
- Penanda bicara DOM:
  - Probe-attr-9/10 dan csrc-live-3 tidak menemukan token pembeda di 3000 karakter pertama HTML tile, maupun di `aria`/`class` akar tile.
  - Penanda Meet kemungkinan ada lebih dalam atau berupa perubahan class/style elemen turunan.
  - Kini CSRC dapat menjadi kebenaran otomatis untuk berburu penanda itu tanpa ground truth manual.
- Roster: tile `[data-participant-id]` memberi id device per join (`spaces/…/devices/N`) dan nama di `span.notranslate`. Twin dikecualikan lewat nama display karena Meet tidak merender penanda diri struktural.
- Pelajaran Vexa (Apache-2.0, boleh di-port dengan atribusi):
  - korelasi energi dengan penanda DOM memakai skor meluruh, penugasan satu-ke-satu, margin, dan pelepasan;
  - nama diikat sekali dan tidak diubah;
  - `unknown` hanya boleh naik.
- Pelajaran LMA: selector cepat basi, jadi perlu daftar cadangan dan watchdog. Glow DOM saja tidak cukup di produksi.

## Desain

1. **Bukti nama berlapis per CSRC:**
   - **Eliminasi join:** satu device baru di roster dan satu CSRC baru tak terikat dalam jendela waktu yang sama.
   - **Eliminasi mute:** CSRC aktif saat hanya satu peserta remote yang tidak termute.
   - **Korelasi penanda DOM:** token class/style turunan tile yang menyala bersama aktivitas CSRC, dengan skor meluruh.
2. **Binder CSRC ↔ device:**
   - Penugasan satu-ke-satu dengan ambang keyakinan dan margin atas kandidat kedua.
   - Nama diikat sekali per CSRC dan tidak diubah. Konflik dicatat, dan nama kembali `unknown`.
   - Device keluar berarti binding dilepas. CSRC baru (rejoin) mulai dari nol.
   - Peserta dengan nama display sama menjadi ambigu dan tetap tanpa nama.
3. **Integrasi sebagai klaim:**
   - `participant_id` tetap `speaker-x`. Field baru `speaker_name` di journal/record.
   - Label ingestion: "Speaker A, shown as Rani, identity unverified".
   - Debrief menampilkan nama sebagai klaim tampilan.
   - Input model tidak berubah di milestone ini; persona dan c04 tetap seperti sekarang.
4. **Watchdog:** bila penanda DOM tidak pernah menyala selama CSRC aktif lebih dari ambang, sinyal ditandai mati, fault dicatat, dan nama baru tidak diberikan.
5. **Flag:** `FAKEYOU_CONVERSATION__SPEAKER_NAMES_ENABLED`, default mati sampai acceptance lulus. Saat mati, perilaku sama persis dengan sekarang.

## Batasan global

- Tanpa protokol privat Meet (data channel `collections`/protobuf), tanpa Meet Media API, tanpa dependency baru. Kode Attendee tidak disalin; kode Vexa boleh di-port dengan atribusi Apache-2.0.
- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`. Data berbahasa Indonesia hanya di fixture JSON. Dokumentasi Bahasa Indonesia.
- `.env` sudah memuat flag probe dan label suara menyala serta identitas `hard_owner`/`hard_twin`. Perubahan berikutnya hanya setelah backup `.env.backup-<tanggal>`, dan secret tidak boleh ditampilkan.
- Sesi live hanya di container worker (`fakeyou worker start … --audio --converse --persona .runtime/audio/persona.json`), jangan pernah di browser Mac karena peserta mendengar gema. Beri jeda bila Meet menahan di "Getting ready".
- Jangan mengubah artefak `.runtime` lama. Fixture baru disamarkan (Host/Guest/Twin, device dinomori ulang, obrolan di luar script dikosongkan).
- Semua gate yang sudah lulus dipertahankan, termasuk replay label probe-attr-8 dan csrc-live-3, gate semantik, laju c04, dan operasi worker. Tanpa `xfail`, tanpa melonggarkan gate, tanpa menambah latency agent.

## Task 0 — Baseline

- [ ] Catat HEAD. Jalankan `ruff check`, `ruff format --check`, `mypy src`, `pytest -m 'not live and not container and not browser'`, `pytest -m browser tests/test_attribution_probe.py tests/test_voice_activity.py`, build image, dan `pytest -m container`.
- [ ] Baca `voice_activity.py`, `assets/voice_activity.js`, `assets/attribution_probe.js`, `probe_replay.py`, `participant_observer.py`, `ingestion.py`, `cli/debrief.py`, `cli/join.py`, serta ketiga laporan di bagian Dasar.

## Task 1 — Probe penanda DOM dan bukti roster

- [ ] Tes merah (browser lokal, halaman uji):
  - probe merekam ringkasan seluruh turunan tile: daftar token `class` dan atribut `style` per jalur elemen, dalam bentuk ter-hash dan terbatas ukurannya;
  - probe merekam status mute per tile;
  - probe merekam waktu muncul dan hilangnya device di roster.
- [ ] Analisis offline `probe_replay`:
  - Untuk tiap token turunan per tile, hitung presisi dan recall nyala-bersama terhadap aktivitas CSRC peserta tile itu (level frame ≥0,05). Pemetaan CSRC → peserta diambil dari ground truth sesi.
  - Laporkan token terbaik per sesi dan stabilitasnya lintas tile serta lintas sesi.
- [ ] Sesi live di container: host dan sedikitnya dua manusia lain di ruangan berbeda. Skrip:
  - satu peserta sudah ada sebelum twin masuk, satu join setelah twin;
  - giliran solo bergantian;
  - mute/unmute;
  - satu overlap;
  - satu rejoin dengan nama sama.
  Catat ground truth per giliran.
- [ ] Gate lanjut ke Task 2: ada penanda dengan presisi ≥0,98 dan recall ≥0,5 terhadap aktivitas CSRC, atau eliminasi join+mute sendiri memberi presisi nama ≥0,98 dengan cakupan yang dilaporkan. Bila gate gagal, laporkan dan berhenti tanpa memasang nama produksi.

## Task 2 — Binder CSRC ↔ device

- [ ] Tes merah murni untuk:
  - eliminasi join;
  - eliminasi mute;
  - korelasi penanda dengan skor meluruh dan margin;
  - penugasan satu-ke-satu;
  - nama yang tidak pernah diubah;
  - konflik menjadi `unknown`;
  - pelepasan saat device keluar;
  - rejoin dengan nama sama (label baru, nama sama);
  - nama display kembar menjadi ambigu;
  - pengecualian twin.
- [ ] Implementasi di `voice_activity.py`, dengan atribusi Vexa bila logika di-port. Script `voice_activity.js` merekam token penanda hasil Task 1 dengan daftar cadangan.
- [ ] Replay offline atas sesi Task 1 sebagai fixture CI anonim: presisi nama ≥0,98 dan cakupan dilaporkan.

## Task 3 — Integrasi klaim nama dan watchdog

- [ ] Tes merah untuk:
  - `speaker_name` di journal dan record;
  - label ingestion "Speaker A, shown as …, identity unverified";
  - instruksi ekstraksi yang melarang menyamakan nama tampilan dengan identitas terverifikasi atau owner;
  - batasan dan tampilan debrief;
  - `fakeyou report` dengan cakupan nama dan alasan `unknown`;
  - watchdog sinyal mati (fault dan journal);
  - flag mati yang membuat perilaku sama persis.
- [ ] Implementasi di balik `speaker_names_enabled`. Input model tidak berubah.

## Task 4 — Verifikasi akhir dan acceptance

- [ ] Gate statis, non-live/non-container, browser, build image, dan container.
- [ ] Acceptance Meet di container dengan flag menyala (host plus dua manusia lain, skrip Task 1 versi baru). Diukur:
  - presisi nama ≥0,98 terhadap ground truth host;
  - cakupan nama;
  - overlap menjadi `unknown` ≥0,95;
  - label per orang stabil ≥0,95;
  - watchdog saat penanda sengaja hilang bila memungkinkan;
  - latency agent tidak bertambah.
- [ ] Bila acceptance lulus, nyalakan default `voice_labels_enabled` dan `speaker_names_enabled` lalu dokumentasikan. Bila tidak, biarkan mati dan laporkan.
- [ ] Commit lokal per task yang direview. Akhiri dengan daftar commit, command + exit code, artefak, tabel metrik, dan risiko.

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/speaker-names.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/reports/2026-10-05-csrc-research.md, docs/reports/2026-10-05-voice-labels.md, docs/reports/2026-10-05-attribution-probe.md, src/fakeyou/voice_activity.py, src/fakeyou/assets/voice_activity.js, src/fakeyou/assets/attribution_probe.js, src/fakeyou/probe_replay.py, serta referensi Vexa di /Users/pushmove/Desktop/jemboed/vexa (core/meetings/modules/gmeet-capture, zoom-capture/src/track-name-resolver.ts) sebelum mulai. Buat branch feat/speaker-names dari master terbaru (sudah memuat plan ini); jangan reset, buang perubahan, push, atau merge.

Tujuan: beri nama display dari roster Meet ke label suara per CSRC bila bukti kuat (eliminasi join, eliminasi mute, korelasi penanda DOM turunan tile terhadap aktivitas CSRC dengan skor meluruh, satu-ke-satu, margin), nama diikat sekali dan tidak diubah, unknown saat ambigu. Mulai dengan probe penanda DOM dan gate (penanda presisi >=0,98 recall >=0,5 atau eliminasi sendiri presisi nama >=0,98); bila gate gagal, laporkan dan berhenti tanpa memasang nama produksi. Nama adalah klaim tampilan yang tidak terverifikasi: masuk ke journal/record/ingestion/debrief, input model tidak berubah. Fitur di balik FAKEYOU_CONVERSATION__SPEAKER_NAMES_ENABLED default mati sampai acceptance lulus.

Aturan: Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di .py; dokumentasi Bahasa Indonesia; tanpa dependency baru; tanpa protokol privat Meet dan tanpa Meet Media API; kode Vexa boleh di-port dengan atribusi Apache-2.0, kode Attendee tidak disalin; .env boleh diubah setelah backup tanpa menampilkan secret; sesi live hanya di container worker (fakeyou worker start ... --audio --converse --persona .runtime/audio/persona.json), jangan pernah di browser Mac; jangan mengubah artefak .runtime lama; fixture baru disamarkan; semua gate yang sudah lulus dipertahankan termasuk replay label probe-attr-8 dan csrc-live-3, gate semantik, laju c04; tanpa xfail; tanpa menambah latency agent.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Setiap task direview reviewer terpisah sebelum commit lokal. Catat progres di .runtime/speaker-names/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Minta link Meet hanya setelah probe dan analisis offline siap; sesi live butuh host dan sedikitnya dua manusia lain di ruangan berbeda, dengan skrip di plan. Akhiri dengan commit lokal yang direview, tabel metrik gate dan acceptance (presisi nama, cakupan, overlap, stabilitas label), risiko, dan rekomendasi default flag.
```

Jalankan:

```sh
mkdir -p .runtime/speaker-names
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/speaker-names/muse-prompt.txt
```
