# Plan atribusi participant: bukti berlapis di satu jam

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer kode dan reviewer atribusi terpisah dari penulis.

**Tujuan:** setiap transkrip dari meeting Google Meet diberi pembicara yang benar bila buktinya kuat. Bila buktinya lemah, transkrip diberi label anonim yang stabil ("Speaker A"), atau `unknown` saat overlap dan ketika bukti hilang. Nama yang salah lebih buruk daripada `unknown`.

**Status:** rencana, 5 Oktober 2026. Basis `master` pada `bf22ec3`. Buat branch `feat/participant-attribution`. Jangan push/merge.

**Pembaruan 5 Oktober:** gate Task 1 gagal untuk penamaan ([laporan probe](../reports/2026-10-05-attribution-probe.md)). CSRC sempat dikira terblokir CloakBrowser; ternyata tersedia lewat metadata frame terenkode ([riset CSRC](../reports/2026-10-05-csrc-research.md)). Milestone dipersempit menjadi label suara anonim per join di balik flag default mati ([laporan label suara](../reports/2026-10-05-voice-labels.md)); Task 2–6 untuk nama tidak dijalankan.

**Keputusan pengguna (5 Oktober):** bangun yang proper untuk jangka panjang dari pelajaran Attendee, Vexa, dan LMA; Meet Media API tidak dapat dipilih. Plan ini tidak memakai protokol privat Meet (data channel `collections`/protobuf) karena rapuh dan tidak terdokumentasi.

## Pelajaran yang dipakai

- **Gate 3 Oktober gagal** ([laporan](../reports/2026-10-03-media-gate.md)) karena tiga hal: capture mono campuran, tidak ada penanda pembicara aktif yang andal di DOM, dan stempel waktu STT berupa waktu tiba, bukan interval audio sumber.
- **Attendee** (`bots/google_meet_bot_adapter/google_meet_chromedriver_payload.js`, Elastic-2.0, hanya perilaku yang dipelajari): `RTCRtpReceiver.getContributingSources()` memberi `audioLevel` per contributing source (CSRC) di audio campuran SFU Meet. Diolah dengan hysteresis, hasilnya event mulai/selesai bicara per sumber. Ini API WebRTC standar. Pemetaan CSRC ke nama di Attendee memakai protokol privat dan tidak kita tiru.
- **Vexa** (`core/meetings/modules/gmeet-capture/`, Apache-2.0, boleh di-port dengan atribusi):
  - Indeks stream adalah slot yang dapat berpindah, bukan identitas.
  - Nama diikat per giliran dan tidak pernah diubah sesudahnya; `unknown` hanya boleh naik menjadi nama.
  - Korelasi energi dengan penanda DOM memakai skor yang meluruh, penugasan satu-ke-satu, dan ambang keyakinan. Aturan resolver Zoom (`zoom-capture/src/track-name-resolver.ts`) lebih matang: hanya memilih ketika satu sumber paling keras dan tepat satu kandidat, dengan margin hysteresis dan pelepasan saat idle.
  - Pengecualian diri sendiri dibuat struktural lewat `data-self-name`, bukan dari teks.
  - Class obfuscated tidak boleh dipelajari otomatis.
  - Jam yang benar dihitung dari jumlah sampel (`startMs + samples/SR`), bukan waktu tiba.
  - Akurasi live Vexa 85%, dan pada kondisi produksi glow DOM tidak pernah menyala. Satu penanda DOM saja tidak cukup.
- **LMA** (bot tidak mendukung Meet; hanya scraper DOM ekstensi `jscontroller="ES310d"`):
  - Suara bot sendiri dipisah lewat routing audio, dan proyek ini sudah punya sink `agent_output` terpisah.
  - Batas giliran dan nama adalah dua hal berbeda, digabung lewat overlap interval, bukan lewat "nama saat hasil STT tiba".
  - `unknown` dipakai secara eksplisit, tidak ada nama lengket.
  - Selector diperlakukan cepat basi: daftar cadangan dan watchdog yang menandai sinyal mati.
- Kode yang ada: `src/fakeyou/participants.py` (`SpeechWindow`, `SpeakerSample`, `attribute_speaker`, resolver konservatif dengan cakupan ≥95%) dan `src/fakeyou/participant_observer.py` (polling DOM roster). Record sudah punya `participant_id` dan `speaker_evidence_id`. Kebijakan partisipasi ada di `src/fakeyou/participation.py`.

## Desain

1. **Satu jam.** Semua bukti dicap pada jam yang sama dan offset-nya diukur:
   - jam halaman: `performance.timeOrigin + performance.now()`;
   - jam proses Python: monotonik;
   - jam audio: jumlah sampel PCM yang masuk ke VAD/STT.
   Jendela ucapan (`SpeechWindow`) diambil dari segmen VAD sumber, bukan dari waktu tiba transkrip.
2. **Bukti aktivitas per sumber (lapisan utama).** Script in-page, dipasang sebelum Meet dimuat, menyimpan `RTCPeerConnection`. Script itu mensampel `getContributingSources()` dan `getSynchronizationSources()` pada setiap receiver audio sekitar 50 ms, mengirim sampel `{source, audioLevel, at}`, dan mengubahnya menjadi aktivitas per sumber dengan hysteresis. Sumber dengan ID stabil menjadi label anonim "Speaker A/B/…".
3. **Bukti nama (lapisan kedua).** DOM roster (`data-participant-id`, nama tile, tanda diri struktural) ditambah penanda bicara/mute dari beberapa kandidat selector (rantai LMA, daftar Vexa, toggle yang terlihat di gate 3 Oktober). Setiap kandidat dilacak tingkat hidupnya. Bukti eliminasi juga dipakai: peserta yang termute tidak dapat menjadi sumber aktif, dan bila hanya satu peserta remote yang tidak termute, dialah kandidat tunggal.
4. **Binder.** Pengikat sumber → participant memakai korelasi berjendela dengan skor meluruh, penugasan satu-ke-satu, margin hysteresis, dan pelepasan saat idle atau rejoin. Diri sendiri dikecualikan secara struktural. Hasilnya `bound` (nama dan keyakinan), `anonymous` (label stabil tanpa nama), atau `unknown` (overlap, bukti hilang, konflik).
5. **Atribusi per transkrip.** `SpeechWindow` sumber digabung dengan aktivitas sumber lewat overlap interval. Nama ditetapkan di awal giliran dan tidak diubah sesudahnya; `unknown` hanya boleh naik.
6. **Watchdog.** Bila VAD aktif tetapi tidak ada aktivitas sumber, atau penanda DOM mati, saat lebih dari satu peserta hadir, sinyal ditandai mati. Fault dicatat di journal dan atribusi jatuh ke `unknown`.
7. **Kewenangan tidak berubah.** Nama display adalah klaim peserta, bukan autentikasi. Atribusi tidak pernah memberi izin owner. Aturan persona, ingestion, dan debrief yang ada tetap berlaku. Label ingestion memakai nama display sebagai klaim, misalnya "peserta bernama X (tidak terverifikasi)", dan `speaker_identity` evidence tetap menyatakan tidak terverifikasi.
8. **Tanpa beban di jalur jawaban.** Atribusi berjalan asinkron dan tidak menambah latency agent.

Di luar plan ini: capture audio per stream dan STT per stream (pola Vexa) untuk memisahkan ucapan yang tumpang tindih. Hasil gate menentukan apakah itu perlu milestone sendiri.

## Batasan global

- Tanpa dependency baru. Script in-page dikirim sebagai asset JS seperti payload yang ada; tanpa protokol privat Meet. Kode Vexa boleh di-port dengan atribusi Apache-2.0; kode Attendee tidak disalin.
- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- `.env` boleh diubah untuk konfigurasi pengembangan setelah backup `.env.backup-<tanggal>`; jangan menampilkan secret. Jangan mengubah artefak `.runtime` lama. Run baru di `.runtime/participant-attribution/`.
- Semua gate yang sudah lulus dipertahankan, termasuk gate semantik, laju c04, dan operasi worker. Tanpa `xfail` dan tanpa menghapus kasus.
- Fitur atribusi di balik flag konfigurasi yang default mati sampai acceptance lulus. Saat mati, perilaku sama persis dengan sekarang.

## Task 0 — Baseline

- [ ] Catat HEAD/status; jalankan gate statis, `pytest -m 'not live and not container'`, build image, dan container test.
- [ ] Baca `participants.py`, `participant_observer.py`, `participation.py`, `record.py`, `conversation.py` (`TranscriptEvent`), `voice.py`/`audio.py` (VAD dan STT), `browser.py`/`meet.py` (init script dan page), serta laporan gate 3 Oktober dan acceptance multipihak.

## Task 1 — Probe dan gate bukti (tanpa penamaan produksi)

- [ ] Buat probe in-page yang merekam ke artefak, tidak ke produksi:
  - sampel CSRC/SSRC per receiver audio;
  - roster DOM dan semua kandidat penanda bicara/mute;
  - tiga jam (halaman, Python, sampel audio) beserta offset-nya;
  - segmen VAD sumber dan transkrip.
- [ ] Sesi probe live dengan host dan sedikitnya satu manusia lain plus twin. Skrip: giliran solo, bergantian cepat, overlap, mute/unmute, leave/rejoin, dan nama yang mirip bila memungkinkan. Ground truth dicatat host per giliran. Minta link Meet baru hanya setelah probe offline siap.
- [ ] Ukur dan laporkan:
  - apakah setiap peserta mendapat CSRC yang stabil, dan apa yang terjadi saat rejoin;
  - lag CSRC terhadap segmen VAD (median/p95);
  - akurasi tiap kandidat penanda DOM;
  - akurasi eliminasi mute;
  - presisi dan cakupan binder dari replay offline atas rekaman probe.
- [ ] Gate lanjut ke Task 2, diukur atas giliran solo:
  - presisi nama ≥98%;
  - overlap menjadi `unknown` ≥95%;
  - label anonim stabil dalam satu sesi ≥95%.
  Bila gate gagal, laporkan dan berhenti; jangan memasang penamaan produksi.

## Task 2 — Jam dan jendela ucapan sumber

- [ ] Tes merah: `SpeechWindow` diturunkan dari segmen VAD pada jam sampel audio dan dipetakan ke jam halaman dengan offset terukur; transkrip tanpa segmen sumber menjadi `unknown` dengan alasan `bad_clock`.
- [ ] Implementasi jam sampel dan pemetaan offset; catat offset dan driftnya di journal.

## Task 3 — Pengumpul bukti in-page dan observer

- [ ] Tes merah memakai halaman uji lokal untuk: init script yang menangkap receiver sebelum Meet dimuat, sampler CSRC, roster dan penanda dengan pengecualian diri struktural, serta pengiriman event berjam halaman ke Python.
- [ ] Implementasi sebagai asset JS dan perluasan `ParticipantObserver`. Daftar kandidat selector diurutkan dan setiap kandidat dilacak tingkat hidupnya.

## Task 4 — Binder dan atribusi

- [ ] Tes merah murni untuk:
  - aturan pilih saat satu sumber paling keras dan satu kandidat;
  - margin hysteresis, skor meluruh, dan penugasan satu-ke-satu;
  - pelepasan saat idle atau rejoin;
  - pengecualian diri, eliminasi mute, dan overlap menjadi `unknown`;
  - nama yang tidak pernah diubah dalam satu giliran, dengan `unknown` yang hanya boleh naik;
  - label anonim stabil.
  Sertakan fixture dari rekaman probe Task 1 (nama disamarkan).
- [ ] Implementasi binder dan perluasan `attribute_speaker`. Kontrak `unknown` yang konservatif tetap dipertahankan.

## Task 5 — Watchdog, integrasi, dan flag

- [ ] Watchdog sinyal mati dengan fault dan journal.
- [ ] Integrasi ke `TranscriptEvent`, record, journal, `fakeyou report` (cakupan atribusi, alasan `unknown`, keyakinan), ingestion (label nama display sebagai klaim), dan debrief, semua di balik flag default mati.
- [ ] Persona: aturan yang membedakan "pembicara bernama X menurut tampilan" dari identitas terverifikasi. Ukur ulang probe c04 dan corpus trust/brief; laju c04 tidak boleh memburuk dan gate semantik tetap lulus.

## Task 6 — Verifikasi akhir dan acceptance

- [ ] Gate statis + non-live/non-container, build image, container test.
- [ ] Acceptance Meet dengan flag menyala, host, dan sedikitnya satu manusia lain:
  - laporan cakupan atribusi, presisi nama terhadap ground truth host, dan overlap menjadi `unknown`;
  - label anonim stabil, rejoin, dan watchdog saat sinyal sengaja dihilangkan bila memungkinkan;
  - latency agent tidak bertambah;
  - pertanyaan "Rani yang bilang …?" saat pembicaranya memang terlihat bernama lain dijawab sesuai bukti tanpa memberi kewenangan.
- [ ] Bila acceptance lulus, tetapkan default flag menyala dan dokumentasikan; bila tidak, biarkan mati dan laporkan.
- [ ] Commit lokal per task yang direview. Akhiri dengan daftar commit, command + exit code, artefak, tabel metrik (presisi, cakupan, overlap, stabilitas label, lag), risiko, dan apakah capture per stream perlu milestone sendiri.

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/participant-attribution.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/reports/2026-10-03-media-gate.md, docs/reports/2026-10-03-multiparty-acceptance.md, src/fakeyou/participants.py, src/fakeyou/participant_observer.py, serta referensi yang dirujuk plan di /Users/pushmove/Desktop/jemboed/vexa (core/meetings/modules/gmeet-capture, zoom-capture/src/track-name-resolver.ts), /Users/pushmove/Desktop/jemboed/attendee (bots/google_meet_bot_adapter, hanya perilaku) dan /Users/pushmove/Desktop/jemboed/amazon-transcribe-live-meeting-assistant (lma-browser-extension-stack meet.js) sebelum mulai. Buat branch feat/participant-attribution dari master terbaru (sudah memuat plan ini); jangan reset, buang perubahan, push, atau merge.

Tujuan: atribusi pembicara yang proper untuk jangka panjang tanpa Meet Media API dan tanpa protokol privat Meet. Bukti berlapis di satu jam: aktivitas per sumber dari getContributingSources/getSynchronizationSources (API WebRTC standar) sebagai lapisan utama dan label anonim stabil; nama dari DOM roster, penanda bicara/mute berlapis, dan eliminasi mute; binder dengan korelasi meluruh, satu-ke-satu, margin hysteresis, pelepasan idle/rejoin, pengecualian diri struktural, nama tidak diubah dalam giliran, unknown saat overlap atau bukti lemah; jendela ucapan dari VAD pada jam sampel audio; watchdog sinyal mati. Mulai dengan probe dan gate (presisi nama >=98% pada giliran solo, overlap menjadi unknown >=95%, label anonim stabil >=95%); bila gate gagal, laporkan dan berhenti tanpa memasang penamaan produksi. Fitur di balik flag default mati sampai acceptance lulus; nama display adalah klaim, bukan autentikasi.

Aturan: Python Inggris tanpa komentar/docstring baru dan tanpa string Bahasa Indonesia di .py; dokumentasi Bahasa Indonesia; tanpa dependency baru; kode Vexa boleh di-port dengan atribusi Apache-2.0, kode Attendee tidak disalin; boleh mengubah .env untuk konfigurasi pengembangan setelah backup dan tanpa menampilkan secret; jangan mengubah artefak lama; semua gate yang sudah lulus dipertahankan termasuk laju c04; tanpa xfail; tanpa menambah latency agent.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Setiap task direview oleh reviewer terpisah sebelum commit lokal. Catat progres di .runtime/participant-attribution/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Akhiri dengan commit lokal yang direview, tabel metrik gate dan acceptance (presisi nama, cakupan, overlap, stabilitas label, lag jam), risiko, dan rekomendasi apakah capture audio per stream perlu milestone sendiri. Minta link Meet baru hanya setelah probe dan gate offline siap; sesi live butuh host dan sedikitnya satu manusia lain.
```

Jalankan:

```sh
mkdir -p .runtime/participant-attribution
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/participant-attribution/muse-prompt.txt
```

Salin blok prompt ke `.runtime/participant-attribution/muse-prompt.txt` terlebih dahulu; periksa `muse exec --help` bila opsi berbeda.
