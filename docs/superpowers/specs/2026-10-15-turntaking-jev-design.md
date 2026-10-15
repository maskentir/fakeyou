# Desain turn-taking generik: rule guard + Jev System One pre-generate

Tanggal: 2026-10-15
Status: disetujui seksi 1–4 via brainstorming, menunggu review spec
Konteks: `src/fakeyou/foundation/participation.py`, `src/fakeyou/conversation/conversation.py` (`ConversationLoop._decide`), `src/fakeyou/persistence/brief.py`, `docs/plans/multiparty-participation.md`

## 1. Tujuan

Sistem sekarang hanya tahu dua hal: siapa memanggil nama twin (addressed) dan siapa sedang bicara lewat klaster suara (followup_same_speaker). Itu cukup untuk pertanyaan langsung, terbukti live menjawab jujur soal status yang tidak ada, tetapi belum cukup untuk standup bergiliran dan belum generik untuk meeting lain.

Twin harus menjadi representative manusia aslinya di meeting apapun, bukan cuma pengganti standup dengan urutan kaku. Artinya tiga gap harus ditutup sekaligus:

1. Urutan giliran dinamis: siapa sudah bicara, siapa berikutnya, kapan giliran sendiri, tanpa pola yang bisa ditebak (`next`, `Harun, you're up`, `silakan`, `gimana Harun?`).
2. Pemahaman konteks: bedakan pertanyaan ke twin vs obrolan dua orang lain, dari beberapa giliran terakhir.
3. Tes sebelum live: corpus transkrip nyata berlabel untuk ukur salah bicara (false-speak) dan salah diam (false-silent).

## 2. Arsitektur (disetujui)

Hybrid rule-guard + model kecil pre-generate, bukan full-LLM per giliran dan bukan aturan saja.

- Rule guard tetap jalur cepat, nol latency baru: nama/vocative jelas langsung diproses, floor occupied langsung silent. Ini mempertahankan kontrak `multiparty-participation.md`: resolver pure, unknown adalah hasil sah, tanpa tebakan nama.
- Kasus abu-abu ke decider semantik System One (Jev, TypeSafe AI): `choice` speak/silent/clarify plus `noul` pendukung, dijawab paralel dalam satu request, dengan probabilitas terkalibrasi. Keputusan di pre-generate saat orang lain masih bicara, sehingga tidak menambah latency jalur jawab.
- Responder existing (Gemini / reply LLM yang dikonfigurasi) tetap System Two untuk menghasilkan ucapan. Jev tidak menggantikan generasi teks; ia hanya memutuskan partisipasi. Ini best practice sesuai positioning TypeSafe: Jev untuk keputusan terstruktur cepat, LLM untuk penalaran dan generasi terbuka.
- Roster brief hanya hint opsional, bukan urutan kaku. Urutan aktual dilacak live dari dinamika meeting, sehingga desain generik untuk standup maupun meeting lain.
- Fallback konservatif: Jev down, timeout, atau confidence rendah menghasilkan silent, bukan tebakan bicara.

Referensi Jev yang dipakai: endpoint `POST /v1/systemone`, field `model` memilih model (`jev-latest` default stabil, `jev-preview` untuk rilis baru), tiga tipe question (Choice, Score, Noul), semua question dalam satu request dijawab paralel dengan overhead waktu nyaris tetap, latensi 70–500 ms, harga $0.042 per juta token input dan output gratis, probabilitas terkalibrasi untuk selective prediction. Pelajaran evaluasi independen: akurasi zero-shot tinggi pada keputusan pendek dan well-scoped, tetapi probabilitas binary butuh threshold yang di-tune per aplikasi (threshold 0.5 fixed terbukti buruk), dan performa turun pada label halus atau rubric generatif. Maka threshold tidak di-hardcode 0.5.

## 3. Komponen (disetujui)

### 3.1 TurnTracker pure

File baru `src/fakeyou/foundation/turn_tracker.py`, pure tanpa IO dan tanpa model call, dapat diuji tanpa provider.

State immutable per sesi:

- `spoken_ids`: urutan participant yang sudah bicara (hanya ID observed pada epoch yang sama; dua `unknown` tidak dianggap speaker sama).
- `floor_holder`: siapa sedang memegang floor dari sinyal `others_speaking` dan event terakhir.
- `turn_cues`: hasil cue giliran dari teks: eksplisit (`next`, `you're up`, `silakan`, penyebutan nama di posisi vocative atau objek giliran) vs implisit (pertanyaan terbuka setelah semua kecuali twin sudah bicara).
- `my_turn_likelihood`: turunan dari cue + spoken coverage + apakah twin sudah bicara di putaran ini. Bukan urutan kaku `A, Harun, C`; roster brief hanya menaikkan prior, tidak menentukan.

Cue dideteksi dengan grammar terbatas yang diuji (seperti `is_vocative` existing), bukan LLM. Contoh yang harus lulus: `Harun, you're up`, `next`, `silakan Harun`, `gimana Harun, ada update?`. Contoh yang tidak boleh dianggap giliran: menyebut twin dalam kutipan atau kalimat tentang twin.

### 3.2 JevDecider adapter

Modul baru (misal `src/fakeyou/conversation/participation_decider.py`) dengan interface sempit:

- Input `state`: N transkrip terakhir (teks + `participant_id` + epoch + voice cluster ringkas), agenda/konteks brief, `turn_state` dari TurnTracker, nama twin/alias.
- Satu request Jev berisi questions paralel:
  - `decision`: Choice speak / silent / clarify.
  - `addressed_to_me`: Noul, ucapan terakhir ditujukan ke twin.
  - `others_conversing`: Noul, bagian dari obrolan dua orang lain.
  - `needs_clarify`: Noul, referen ambigu dan floor kosong sehingga klarifikasi netral layak.
- Output: keputusan + probabilitas per opsi + confidence. Confidence dipakai untuk threshold otonom vs eskalasi ke clarify/silent.
- Adapter tidak mengarang teks jawaban; ia hanya mengembalikan keputusan terstruktur yang divalidasi tipenya. Tidak ada parsing string bebas.

### 3.3 Settings

Bagian baru `FAKEYOU_PARTICIPATION__*` mengikuti pola `Settings` existing:

- `model` (default `jev-latest`), `base_url`, `api_key` peran sendiri (tanpa pinjam key lintas peran, sesuai kontrak global).
- `window_turns` default 6, `speak_threshold` awal 0.70 dan `clarify_threshold` awal 0.50; keduanya wajib di-tune ulang dari corpus berlabel sebelum live (bukan 0.5 fixed).
- `pre_generate_enabled` (default true).

`check` menyamarkan secret seperti peran lain. Key tidak dikenal tetap ditolak.

## 4. Data flow + pre-generate (disetujui)

1. Final transkrip selalu direkam dulu ke record/ingestion sebelum policy. Silent tidak menghapus ucapan peserta.
2. Rule guard dulu: vocative jelas atau floor occupied diputus sinkron, tanpa panggil Jev.
3. Kasus abu-abu: pakai keputusan Jev yang sudah di pre-generate saat interim atau saat orang lain bicara. Key kesegaran = tuple N ID transkrip terakhir + `turn_state`; cocok persis berarti fresh.
4. Kalau stale atau missing (Jev belum selesai, timeout, atau konteks berubah), pilih silent konservatif dan jadwalkan pregen berikutnya. Jangan block jalur jawab menunggu Jev.
5. Journal mencatat `policy_decided` dengan `rule_vs_jev`, confidence, `turn_state`, dan key kesegaran. Report menghitung pemakaian rule vs Jev, jumlah stale/missing, dan distribusi confidence.

Pre-generate memakai pola `_Pregeneration` existing sebagai acuan, tetapi untuk keputusan (kecil, cepat), bukan untuk kalimat jawaban. Pembatalan generasi usang mengikuti hardening cancellation yang sudah ada.

## 5. Error handling (disetujui)

- Jev timeout, error transport, respons tidak valid, atau confidence di bawah threshold: silent + fault `participation_decider` di journal. Tidak ada retry di hot path, tidak ada timer yang berbicara spontan.
- Clock nonfinite, epoch mismatch, reconnect reset, roster hilang, duplicate display names: TurnTracker menghasilkan `unknown`, JevDecider tidak menerima ID tebakan. Unknown tanpa alamat eksplisit tetap silent.
- Klarifikasi hanya dari rute agent existing dengan konteks ambigu, berupa satu pertanyaan netral. Tidak ada template yang mengarang kandidat fakta.
- Kewenangan tidak naik karena keputusan partisipasi. Aturan brief/persona/memory existing tidak berubah.

Breaking changes diizinkan karena berdampak (persetujuan pengguna):

- `ParticipationContext` tambah field `turn_state` (dan bukan boolean saja).
- `ConversationLoop._decide` mengembalikan decision terstruktur (bukan bool), dengan alasan `rule_addressed`, `rule_floor_occupied`, `jev_speak`, `jev_silent`, `jev_clarify`, `jev_stale_silent`.
- `MeetingBrief` v1 ke v2: tambah `roster_hint` opsional (daftar nama opsional tanpa urutan mengikat). v1 tetap dibaca (kompatibel baca), ditulis sebagai v2.

## 6. Testing: corpus berlabel sebelum live (disetujui)

Tanpa corpus ini, keputusan semantik tidak boleh live.

- Kumpulkan transkrip nyata (dari sesi uji dengan host setuju, namespace uji, bukan `acc2`/`acc3`), lalu beri label per final: `speak` / `silent` / `clarify`, plus target pertanyaan dan alasan (obrolan orang lain, cue giliran, follow-up pindah penutur, overlap, rename, leave/rejoin).
- Metrik utama: false-speak rate (bicara saat harusnya diam, termasuk menjawab target peserta lain dan occupied floor) dan false-silent rate (diam saat pertanyaan explicit-address ke twin tidak dijawab). Target awal mengikuti gate multiparty: nol jawaban untuk target peserta lain dan occupied floor pada sampel; explicit-address yang dapat dijawab memperoleh reply/clarify ≥90%; silent universal tidak boleh lulus. Threshold Jev di-tune dari corpus ini, bukan 0.5.
- Suite:
  - `tests/test_turn_tracker.py`: pure, tanpa provider (cue eksplisit/implisit, kutipan bukan giliran, unknown tidak mewarisi aktivasi, epoch/reconnect reset).
  - `tests/test_participation.py` diperluas: matriks rule + `turn_state`, pregen fresh vs stale.
  - `evals/test_participation_decider_eval.py`: fixture corpus + adapter fake Jev untuk wiring, mode live opt-in untuk Jev asli dengan manifest tersamar seperti pola replay benchmark.
- Semua utterance silent tetap tercatat di denominator report agar precision tidak terlihat bagus secara artifisial.

## 7. Peta file

| Pemilik | File |
| --- | --- |
| Tracker pure | `src/fakeyou/foundation/turn_tracker.py`, `tests/test_turn_tracker.py` baru |
| Policy existing | `src/fakeyou/foundation/participation.py`, `tests/test_participation.py` perluasan |
| Decider Jev | `src/fakeyou/conversation/participation_decider.py`, `evals/test_participation_decider_eval.py` |
| Integrasi loop | `src/fakeyou/conversation/conversation.py`, `src/fakeyou/orchestration/assembly.py`, `src/fakeyou/config/settings.py` |
| Brief | `src/fakeyou/persistence/brief.py` (v1→v2 roster_hint), `examples/meeting-brief.example.json` |
| Observabilitas | `src/fakeyou/observability/report.py`, taksonomi event baru |

## 8. Yang sengaja tidak dilakukan

Tidak ada chat, raise hand, kamera/avatar, approval kontrak, remote control owner, atau operasi multi-meeting otomatis dalam milestone ini. Tidak ada migrasi per-channel audio atau pemetaan owner dari display name. Streaming jawaban per kalimat tetap desain terpisah.
