# Meeting Simulator: E2E tanpa sesi live

Status: desain (2026-11-07), belum diimplementasi.
Pemilik keputusan: twin owner (agent), atas permintaan user agar validasi
tidak lagi bergantung pada sesi Meet live yang dijalankan manual.

## Masalah

Setiap perubahan perilaku sekarang baru terbukti lewat sesi Meet live:
user membuat link, admit twin, membacakan narasi, lalu kita membaca
jurnal secara manual. Ini lambat (±30 menit per sesi), butuh manusia,
tidak bisa diulang persis, dan tidak bisa menjangkau edge case secara
sistematis (dua orang bicara bersamaan, host mengeluarkan twin, chat dan
suara bersamaan, kamera beberapa orang, koneksi STT putus). Sesi
`b6-live-3` dan `b6-live-4` menemukan bug yang semuanya ada di batas
sistem — gerbang nama chat, resolusi kamera, izin aksi, bahasa jawaban —
bukan di logika yang sudah diuji unit.

## Yang sudah ada (inventaris, 2026-11-07)

| Lapisan | Harness | Nyata | Dipalsukan | Tidak menjangkau |
|---|---|---|---|---|
| Actor | `evals/_conversation_replay.py` | `open_conversation`, judge, model | STT, kernel, browser, TTS | perception, aksi, audio |
| Kernel | V0 `fakeyou replay` | kernel, transcript drain, turn feed, playback | model/judge/TTS terekam, waktu virtual | browser, perception, aksi, call state |
| Browser | tes `browser` | CloakBrowser, perception.js, `CommandRunner` di DOM stub | halaman Meet (stub), protokol (payload buatan) | percakapan, audio, kernel |
| Audio | tes `container` | PulseAudio, parec, playback | — | percakapan penuh |

Potongan yang bisa dipakai ulang: stub HTML Meet
(`tests/test_runtime_meeting.py`, `tests/_meet_support.py`), test hook
`__fakeyouRtc.emitTestChannel`/`emitTestTrack`
(`src/fakeyou/assets/rtc_bridge.js`), builder protobuf
(`tests/_meet_protocol_fakes.py`: `user_info`, `output`, `chat_message`,
`collection_payload`, `sync_body`), PulseAudio sink `meeting_audio` (yang
didengar twin) dan `agent_mic` (suara twin) di `docker/audio/default.pa`,
`FakeVisionModel`, dan replay V0 dengan waktu virtual.

Yang belum ada: satu harness yang menjalankan **worker utuh**
(`run_meeting_session` dengan browser asli, audio asli, kernel, perception,
percakapan, aksi) melawan meeting tiruan, dengan skenario berskrip dan
penilaian otomatis.

## Desain: tiga lapisan

### L1 — Meeting Simulator (pengganti utama sesi live)

Halaman **Fake Meet** yang dilayani lokal (`page.route` atau server statis
di container) dan worker yang **tidak dimodifikasi** join ke sana seperti
ke Meet asli. Simulator memainkan peran semua peserta manusia:

- **Join dan lobby:** stub halaman join ("Ask to join"), lalu status
  lobby → admitted lewat payload protokol own-status, atau ditolak /
  dikeluarkan host / diakhiri host untuk skenario keluar.
- **Roster:** `sync_body` dan `collection_payload` lewat data channel
  tiruan: peserta masuk, keluar, ganti nama, mulai/berhenti presentasi.
- **Ucapan peserta:** klip WAV diputar halaman ke sink `meeting_audio`,
  jadi twin mendengar lewat jalur produksi (parec → VAD → STT). Klip
  dibuat sekali dengan TTS dan disimpan sebagai fixture ber-hash,
  termasuk jeda di tengah kalimat, interupsi saat twin bicara, dan dua
  suara bertumpuk.
- **Chat masuk:** `chat_message` lewat data channel.
- **Layar dan kamera:** canvas yang digambar skrip → `emitTestTrack`
  (slide dengan teks tertentu, berganti, berhenti; tile kamera per
  peserta dengan warna baju yang diketahui).
- **Aksi twin:** DOM stub mencatat klik (react, angkat/turun tangan, mute,
  leave) dan mengubah tombolnya agar verifikasi `CommandRunner` berjalan
  seperti di Meet; kirim chat menambah pesan ke panel dan ke data channel
  sehingga echo diamati.
- **Suara twin:** halaman merekam `getUserMedia` (yang membaca
  `agent_mic`), jadi skenario bisa menilai *kapan* twin mulai dan
  berhenti bersuara, tidak hanya teksnya.

Skenario ditulis sebagai data (JSON di `evals/fixtures/`, teks Indonesia
boleh di sana): langkah bertimestamp (`say`, `chat`, `share`,
`camera`, `join`, `leave`, `kick`, `wait_for`), dan ekspektasi:

- **Deterministik** dari jurnal dan state simulator: `replied`, `silent`,
  `action` (react/hand/chat/leave dengan efek teramati), `route`
  (`strong`/`small` dan trigger), `owner_claim`, `language`, latensi
  (VAD stop → audio twin), "twin berhenti ≤ N ms sesudah dipotong".
- **Semantik** lewat judge LLM yang sama dengan `conversation_replay`
  (`means`, `severity`), hanya untuk isi jawaban.

Model: tiga mode per run — `live` (STT/LLM/TTS asli; dipakai untuk gate
rilis), `recorded` (model dan judge terekam ala V0, deterministik, untuk
CI), dan campuran (STT asli, LLM terekam) untuk mengisolasi regresi
audio. Satu skenario live diulang 3 kali karena keluaran model
berfluktuasi; gate pakai rasio, bukan satu kali lulus.

Tempat berjalan: container worker (Linux, PulseAudio, CloakBrowser)
dengan marker baru `simulator`; perintah `fakeyou simulate <katalog>`
menulis laporan per skenario (lulus/gagal, kutipan jurnal, latensi).

### L2 — Smoke di Meet asli (mingguan dan sebelum rilis)

L1 tidak bisa mendeteksi perubahan UI atau protokol Google Meet. Satu
"peserta penguji" otomatis — browser kedua dengan profil persisten akun
Google uji (login sekali oleh owner) — membuat meeting lewat `meet.new`,
meng-admit twin, memutar 5–8 langkah kunci lewat mikrofon virtual,
mengetik chat, share tab, lalu menilai jurnal twin dengan penilai yang
sama dengan L1. Tujuannya hanya mendeteksi drift Meet, bukan menguji
perilaku. Risiko: deteksi otomasi Google dan pembatasan join berulang
(tercatat di A3: `blocked_by_platform` sesudah ±6 join) — maka hanya
mingguan, satu meeting baru per run.

### L0 — Yang sudah ada tetap jalan per PR

Eval judge, `conversation_replay`, replay V0, dan tes unit. L1 mode
`recorded` ditambahkan ke CI per PR begitu stabil.

## Katalog skenario awal (tujuan: semua yang pernah diuji manual + edge case)

1. Sapaan dengan nama di posisi kedua ("Hai Rani, …"), follow-up tanpa nama.
2. Recall memory, klaim owner sudah setuju, permintaan approve, pertanyaan
   keputusan owner, klaim tentang orang lain.
3. Barge-in: "wait, stop" saat twin bicara; backchannel ("mm-hm") tidak
   memotong; dua orang bicara bersamaan.
4. Kontrol sesudah giliran twin ("shorter please", "louder", "again"),
   dalam en dan id; bahasa jawaban mengikuti bahasa yang didengar.
5. Baris untuk orang lain ("Budi, …") → diam; room check tanpa nama.
6. Layar: slide benar, slide dengan fakta salah (koreksi proaktif), slide
   sama dua kali (dedupe), slide tanpa fakta relevan (tidak menarasikan).
7. Chat: pertanyaan diketik tanpa nama lengkap, chat untuk orang lain,
   minta kirim jawaban ke chat, chat dan suara bersamaan.
8. Kamera: satu peserta ("warna baju saya"), beberapa peserta tanpa nama
   (harus jujur tidak bisa melihat), dengan nama.
9. Aksi: react, angkat/turun tangan, mute, keluar; dengan dan tanpa izin
   brief (alasan penolakan yang benar).
10. Kehadiran: jumlah peserta, siapa presenter, peserta masuk/keluar.
11. Lifecycle: lobby lama, ditolak, dikeluarkan host, meeting diakhiri,
    browser crash dan rejoin (A2), STT putus dan pulih, sesi 60 menit.
12. Kejujuran: "are you a real person", look gagal tidak diklaim berhasil.

Setiap bug yang ditemukan sesi live sejak `b6-live-3` menjadi skenario
regresi pertama di katalog.

## Urutan irisan

- **S1:** Fake Meet minimal — join, admitted, roster dua peserta, satu
  ucapan WAV → twin menjawab; penilai deterministik membaca jurnal. Bukti
  bahwa worker utuh berjalan tanpa Meet asli.
- **S2:** chat masuk/keluar dan aksi DOM dengan verifikasi efek.
- **S3:** layar dan kamera lewat track canvas, vision live atau
  `FakeVisionModel`.
- **S4:** rekaman suara twin dan metrik waktu (latensi, berhenti saat
  dipotong).
- **S5:** mode `recorded` untuk CI; katalog lengkap.
- **S6:** L2 smoke di Meet asli.

Gate L1: setiap skenario katalog lulus ≥ 2 dari 3 run live dan 3 dari 3
recorded; satu run katalog penuh ≤ 20 menit; nol langkah manusia.

## Alternatif yang ditolak

- **Memalsukan di level kernel saja (replay V0 diperluas):** cepat, tetapi
  justru melewatkan batas tempat bug `b6-live-4` berada (gerbang chat,
  resolusi kamera, izin aksi, perception).
- **Hanya L2 (bot di Meet asli):** realistis, tetapi rapuh, lambat,
  bergantung akun Google, dan dibatasi Google; tidak cocok untuk ratusan
  edge case.
