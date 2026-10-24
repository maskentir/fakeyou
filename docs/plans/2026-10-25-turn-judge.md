# Turn judge: satu model yang memutuskan kapan giliran selesai dan apakah twin bicara

Status: PR 1 (turn judge untuk keputusan partisipasi) dan PR 2 (commit awal saat hening) selesai; menunggu sesi live.

## Masalah

Dua keputusan inti di meeting, yaitu *kapan orang selesai bicara* dan *apakah twin perlu menjawab*, hari ini diambil
oleh mekanisme yang terpisah dan sebagian besar bukan model.

Angka dari sesi live `live-xnj-itkm-owt` (193 transkrip final, journal metadata saja):

| Yang diukur | Angka |
| --- | --- |
| Keputusan partisipasi oleh rule | 193 dari 193; decider Gemini tidak pernah ditanya |
| Balasan dari rule `followup_same_speaker` | 78 dari 102 balasan |
| Coverage atribusi pembicara (dasar "pembicara yang sama") | 0,52 |
| VAD lokal berhenti → transkrip final Muse | p50 2,3 s, p90 3,1 s |
| VAD lokal berhenti → kalimat pertama twin | p50 3,6 s, p90 4,4 s |
| Transkrip final → kalimat pertama twin (model) | p50 1,2 s |

Penyebabnya ada di kode:

- `participation_graph.py`: rule jalan duluan. Decider hanya ditanya kalau rule memutuskan `silent/not_addressed` **dan**
  `detect_turn_cue` (tabel kata kunci `turn_cues.json`) menangkap isyarat giliran.
- `foundation/participation.py`: setelah twin disapa, selama `activation_seconds` (30 s) setiap kalimat dari "pembicara
  yang sama" dibalas. Penentu "pembicara yang sama" adalah voice cluster, yang benar hanya setengah waktu.
- `ack_vocabulary.json` dan `turn_cues.json` memutuskan apa itu ack, interjeksi dan isyarat giliran lewat daftar kata.
- Akhir giliran sepenuhnya ditentukan `speechComplete` dari Muse (endpointing akustik mode DIARIZATION). Event
  `speechEnd` dari Muse diabaikan.
- Nama `Jev*` (`JevState`, `JevCache`, `kick_jev`) adalah sisa provider JEV; mekanismenya generik (prefetch keputusan di
  interim). Provider `jev` masih ada di `Literal` settings tapi `build_decider` menolaknya.

## Prinsip

1. **Model yang memutuskan, kode yang menjaga.** Pertanyaan bahasa ("sudah selesai?", "ini untuk siapa?", "perlu
   dijawab?") dijawab model. Kode hanya menegakkan batas keras yang tidak boleh dilanggar dan mengatur waktu.
2. **Tidak ada tabel kata kunci untuk keputusan.** `turn_cues.json` dan `ack_vocabulary.json` tidak lagi memutuskan apa pun.
3. **Satu panggilan, dua jawaban.** Selesai-atau-belum dan bicara-atau-diam adalah satu penilaian atas percakapan yang
   sama, jadi satu panggilan structured output.
4. **Spekulatif, bukan menunggu.** Penilaian dihitung saat orang masih bicara (pada interim yang stabil), sehingga
   saat hening datang jawabannya sudah siap. Model ringan butuh ~0,8 s per panggilan (probe 2026-10-25), terlalu lama
   kalau baru dimulai setelah hening.
5. **Muse final tetap sumber kebenaran untuk record.** Commit awal hanya mempercepat *kapan twin mulai menjawab*;
   transkrip yang disimpan tetap transkrip final.

## Desain

### 1. `TurnJudge` (menggantikan participation decider dan rule partisipasi)

Satu model ringan (role baru `turn_judge`, default model yang sama dengan `reply`: `gemini-3.1-flash-lite`) dengan
`create_agent(response_format=ProviderStrategy(TurnJudgement))`:

```text
TurnJudgement
  heard:      kalimat pembicara terakhir, ditulis ulang sesuai maknanya   (seperti `understood` di ReplyPlan)
  finished:   bool   pembicara sudah menyelesaikan gilirannya
  addressee:  twin | someone_else | everyone | unclear
  action:     reply | clarify | stay_silent
  confidence: 0..1
```

Masukan: jendela percakapan terakhir dari `record.jsonl` (siapa bicara, termasuk twin), nama twin dan alias, teks
interim/final terbaru, dan apakah twin sedang bicara. `heard` diletakkan pertama karena pada `ReplyPlan` field serupa
menaikkan akurasi bahasa dari 0,44 ke 1,0.

Penilaian di-cache per `(id jendela, teks ternormalisasi)` di modul `turn_judge`. Kode `Jev*` lama dihapus, tidak dipindahkan.

### 2. Batas keras di kode (bukan keputusan bahasa)

- Twin tidak mulai bicara saat orang lain sedang bicara (`others_speaking`, VAD aktif).
- `confidence` di bawah ambang (`reply_threshold`, `clarify_threshold`) berarti diam.
- Kalau judge gagal atau telat: fallback hanya **sapaan langsung ke nama twin** → jawab; selain itu diam. Ini pencocokan
  nama, bukan tabel intent, dan hanya berlaku saat model tidak tersedia.
- Batas jumlah interjeksi proaktif yang sudah ada (`max_interjections_per_10min`) tetap.

### 3. Endpointer: commit awal saat hening

Sinyal akustik yang dipakai: `speech_stopped` dari VAD lokal dan `speechEnd` dari Muse (mulai dijurnal dan dipakai).

```text
interim stabil   → judge dijalankan spekulatif untuk teks itu
hening ≥ hold    → ambil penilaian untuk teks interim terakhir
  finished ∧ action=reply/clarify ∧ confidence ≥ ambang  → commit awal: giliran dimulai dari teks interim
  finished ∧ stay_silent                                 → tidak ada apa-apa
  belum selesai / penilaian belum siap                   → tunggu final Muse (perilaku hari ini)
final Muse tiba
  giliran STT yang sama sudah di-commit awal  → simpan final ke record, tidak memulai giliran kedua
  belum di-commit                              → jalur normal, judge dipanggil (hasil cache bila teksnya sama)
bicara lagi setelah commit awal
  twin belum bersuara  → batalkan giliran itu diam-diam
  twin sudah bersuara  → barge-in yang sudah ada
```

`hold` default 300 ms dan bisa diatur. Giliran STT dicocokkan lewat `turnId` Muse, bukan teks.

### 4. Yang dihapus

- `choose_participation` dan jalur `followup_same_speaker` / jendela `activation_seconds`.
- `detect_turn_cue` beserta `turn_cues.json`; `is_bare_ack` / `is_interjection` beserta `ack_vocabulary.json` sebagai
  penentu keputusan.
- Provider `jev` di `ParticipationDeciderSettings` dan seluruh nama `Jev*`.
- Role `participation_decider` diganti `turn_judge` (breaking change, key `.env` berubah).

## Evaluasi sebelum sesi live

- **Fixture berlabel** `evals/fixtures/turn-judge.json`: percakapan sintetis Indonesia dan Inggris (bukan kutipan sesi
  nyata) berisi kalimat utuh, potongan di tengah frasa, sapaan ke twin, sapaan ke orang lain, ack, pertanyaan ke semua
  orang, dan obrolan lanjutan yang bukan untuk twin. Label: `finished`, `addressee`, `action`.
- **Metrik dan gerbang:**

| Metrik | Gerbang |
| --- | --- |
| Commit awal pada kalimat yang belum selesai (memotong orang) | ≤ 2% |
| Twin menjawab padahal bukan untuknya | ≤ 5% kasus berlabel `stay_silent` |
| Sapaan langsung ke twin tidak dijawab | 0 (severe) |
| Latensi judge p50 / p90 | ≤ 0,9 s / ≤ 1,5 s |

- Replay percakapan yang sudah ada tetap harus 0 severe dan 0 regresi.
- **Target sesi live:** hening → kalimat pertama twin p50 ≤ 1,8 s (dari 3,6 s); balasan yang tidak menyasar ≤ 1 per
  10 menit; kalimat orang terpotong ≤ 5%.

## Urutan PR

1. **Turn judge untuk keputusan partisipasi pada final.** Ganti rule dan decider dengan `TurnJudge`, hapus tabel kata
   kunci dan jalur followup, rename role. Fixture + eval live. Bisa diukur sendiri: LIVE-01 seharusnya hilang di sini.
2. **Endpointer commit awal.** Judge spekulatif di interim, sinyal `speechEnd`, rekonsiliasi dengan final Muse lewat
   `turnId`, pembatalan saat bicara lagi. Eval latensi dan potong-orang.
3. **Sesi live** dengan naskah fase, scorecard ke `docs/reports/`.

## Yang perlu diputuskan owner

- Model judge: default `gemini-3.1-flash-lite` (role `reply`). Probe 20 kasus: 17/20 benar tanpa konteks percakapan,
  1 commit prematur, ~0,8 s sekuensial. Alternatif `gemini-3.8-flash` lebih akurat tapi lebih lambat.
- Ambang `hold` 300 ms: lebih kecil berarti lebih cepat tapi lebih sering salah di jeda napas.

## Hasil PR 1 (2026-10-25)

Eval judge live (`evals/test_turn_judge_eval.py`, `gemini-3.1-flash-lite`, 3 run × 27 kasus):

| Metrik | Hasil | Gerbang |
| --- | --- | --- |
| Sapaan langsung ke twin terlewat | 0 | 0 |
| Kasus yang harus dijawab tapi terlewat | 0% | ≤ 10% |
| Twin menjawab padahal bukan untuknya | 0% | ≤ 5% |
| Kalimat belum selesai dinilai selesai | 14% (j17, 3/3 run) | gerbang PR 2: ≤ 2% |
| Latensi judge p50 / p90 | 1,13 s / 1,45 s | target 0,9 s / 1,5 s |

Replay percakapan live dengan judge asli (9 run × 15 skenario): semua keluarga 1,0, 0 severe, 0 regresi. Latensi per
langkah naik sekitar 1 s karena replay tidak punya interim, jadi judge baru jalan saat final tiba.

Laporan: `docs/reports/2026-10-25-turn-judge.json`, `docs/reports/2026-10-25-conversation-replay-turn-judge.json`.

## Hasil PR 2 (2026-10-25)

- `orchestration/early_turns.py` (`EarlyTurns`): setelah VAD lokal berhenti dan hening `commit_hold_seconds` (default
  0,3 s), giliran dimulai dari teks interim terakhir bila judge menilai `finished` dan memutuskan menjawab. Final Muse
  untuk giliran itu tetap disimpan ke record, tapi tidak memulai giliran kedua (`early_turn_verified`).
- Bicara lagi setelah commit (interim tumbuh dari kata-kata yang sama) membatalkan giliran itu (`early_turn_cancelled`,
  `speech_resumed`); final yang jauh lebih panjang dari teks commit juga membatalkannya (`final_says_more`) lalu
  dijawab utuh. Pembicara berikutnya tidak membatalkan giliran yang sedang berjalan.
- Aturan `finished` diperketat: kalimat yang hanya meminta orang lain menunggu tidak dihitung selesai.
- Mode per-stream belum memakai commit awal.

Eval judge live (5 run × 27 kasus): sapaan terlewat 0, jawaban terlewat 0%, salah jawab 0%, kalimat belum selesai
dinilai selesai 0%, commit prematur (dinilai selesai **dan** dijawab padahal belum selesai) 0%, latensi p50 0,95 s /
p90 1,13 s. Laporan: `docs/reports/2026-10-25-turn-judge-early-commit.json`.

Perkiraan dari journal `live-xnj-itkm-owt`, belum diukur live: hening → kalimat pertama twin turun dari p50 3,6 s ke
sekitar 1,5 s (0,3 s hold + p50 1,2 s model; penilaian judge sudah berjalan sejak interim).

## Hasil PR 2b (2026-10-25): barge-in decided by the judge

### Cache yang bisa menilai beberapa interim

`JudgeCache` dulu hanya punya satu slot: interim yang baru membatalkan penilaian yang sedang jalan, jadi judge tidak
pernah menilai dua baris sekaligus dan tidak ada penilaian lama yang bisa dipakai lagi. Sekarang cache menyimpan satu
penilaian per jendela (`dict` key → task), tidak saling membatalkan, dan menyimpan maksimal 16 penilaian (yang selesai
dulu yang dibuang). Final sebuah giliran memakai kembali penilaian interim terakhirnya kalau teksnya sama.
`ModelTurnJudge` membatasi panggilan yang berjalan bersamaan (4) supaya beberapa interim tidak menumpuk tanpa batas.

### Barge-in

- Barge-in tidak lagi dari jumlah kata saja. Interim yang panjang cukup engañap twin, lalu **judge** yang memutuskan:
  `addressee = twin`, bukan backchannel, dan `confidence ≥ barge_in_threshold` (default 0,6). Syarat `finished` tidak
  dipakai, karena orang yang sedang bicara ke twin harus bisa menahan twin bahkan kalau kalimatnya belum selesai.
- `addressee = everyone` tidak diambil giliran: twin boleh menyelesaikan kalimatnya.
- Kalau judge gagal atau telat, giliran tetap diserahkan (`decided_by = judge_unavailable`), supaya twin tidak bicara
  di atas orang.
- Jurnal `barge_in_detected` menambah field `decided_by` (`judge`, `judge_unavailable`, atau kosong untuk jalur lama).
- Setting baru: `CONVERSATION__BARGE_IN_JUDGE_ENABLED` (default true, false mengembalikan barge-in dari jumlah kata) dan
  `TURN_JUDGE__BARGE_IN_THRESHOLD` (default 0,6).

### Evaluasi

- Fixture berlabel `evals/fixtures/turn-judge-barge-in.json` (18 kasus sintetis, bukan kutipan sesi nyata): sapaan ke
  twin, perintah ke twin, follow-up, jawaban atas twin, fragmen ke twin, dan yang tidak untuk twin (orang lain, ack,
  obrolan antar orang, logistics ruang, pertanyaan ke semua orang, nama yang disebut orang lain, self-talk).
- Gerbang (`evals/test_barge_in_judge_eval.py`): twin tidak melepas giliran padahal baris itu untuknya ≤ 10%; twin
  melepas giliran padahal baris itu bukan untuknya ≤ 5%.
- Nama yang salah dengar ("Randy" untuk "Rani") tetap dihitung untuk twin, sama seperti di fixture partisipasi (j08):
  twin melepas giliran lalu bisa bertanya siapa yang dipanggil.

Eval live (`gemini-3.1-flash-lite`, 3 run × 18 kasus, `docs/reports/2026-10-25-turn-judge-barge-in.json`):

| Metrik | Hasil | Gerbang |
| --- | --- | --- |
| Baris untuk twin tapi twin tidak melepas giliran | 0% | ≤ 10% |
| Baris bukan untuk twin tapi twin melepas giliran | 0% | ≤ 5% |
| Kegagalan | 0 | 0 |
| Latensi judge p50 / p90 | 1,10 s / 1,30 s | p50 ~1 s (batas bawah twin melepas giliran) |

- Yang perlu diukur live di sesi Meet: berapa detik twin masih bicara setelah orang mulai bicara dengannya, dan
  apakah `barge_in_detected` dengan `decided_by = judge` memang mengurangi kalimat twin yang terpotong di tengah.

