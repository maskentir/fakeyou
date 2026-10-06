# Arsitektur Jangka Panjang Per-Stream Capture

Status: riset — belum diimplementasi. Cabang `feat/per-stream-capture`.
Basis bukti: acceptance `docs/reports/2026-10-06-per-stream-acceptance.md`,
4 berkas riset klaster (`/tmp/lt-arch-identity.md`, `/tmp/lt-arch-quality.md`,
`/tmp/lt-arch-merge.md`, + verifikasi kode inline), review integrasi final.
Riset chatter agent (`/tmp/lt-arch-chatter.md`) dan infra
(`/tmp/lt-arch-infra.md`) diselesaikan terpisah sebagai follow-up di luar
scope dokumen ini, sesuai arahan: yang dipecahkan dulu adalah isu per-stream
capture.

## 1. Scope

Masuk: tap encoded + decode, transport drain, sesi STT per stream,
merge beridentitas, substrat identitas speaker, telemetri capture.
Keluar (follow-up terpisah): kebijakan chatter/sapaan agent, reliabilitas
infra (SQLite, preflight, join/admit), debat produk.

## 2. Peta isu yang dialami → akar terverifikasi

| # | Isu live | Akar (terverifikasi baca kode) | Status kini |
|---|----------|--------------------------------|-------------|
| I1 | Nol transkrip PS-1..PS-1E | Meet consume encoded frame tanpa writeback (repro offline); tap PCM mentah tak pernah menerima byte | Diperbaiki Task 7 (encoded-tap + Opus decode, `stream_capture.js:209-280`); tanpa telemetri kesehatan tap |
| I2 | Turn 6 hilang di jendela reconnect (PS-2) | Socket STT mati + audio tak ter-buffer antar sesi | Diperbaiki Task 8 (`audio.py:86-163` continuity buffer + expedite); reconnect tetap reaktif |
| I3 | Inversi urutan permanen (risiko reviewer #1) | `merge_transcripts` sort-per-timestamp (`conversation.py:85-86`) tetapi callback emit-habis tiap frame (`cli/join.py:165-182`); tanpa holdback/watermark; timestamp = jam STT/wall, bukan jam capture (`conversation.py:59`) | Terbuka; tanpa tes |
| I4 | Kolaps identitas PS-4 (semua-unknown + semua-a) | CSRC mixer bersama + roster 1 meracuni binder | Diperbaiki Task 11 (CSRC sync-only, `stream_capture.js:49-67`) |
| I5 | Name-filter memakan twin (PS-5 post=1) | Self-filter roster presisi rendah + contention tak tertangani | Diperbaiki Task 12 (single-match + demand gate + retire-one) |
| I6 | Relabel sunyi + `[moved]` ValueError (risiko reviewer #2-3) | `_follow_csrc` pindah tenure tanpa journal (`voice_activity.py:136-152`); guard `labels` tidak menjaga unpack `matches` (`:148`); `_bind` 1:1-only mati multi-party (`:177-181`); delete-one me-re-mint slot yang masih hadir (`:160-169`, dipin tes `:900-920`); dormant-drop membuang identitas (`:102-105`) | Terbuka; dipin "by design" |
| I7 | Rejoin ganti label (a=Annisa, c=Annisa rejoin) | Label lahir dari slot-tenure, bukan orang (`voice_activity.py:79-134`); dua substrat identitas paralel (binder stream vs `_csrc_labels` mixed) | Terbuka struktural |
| I8 | Nama display gagal gate (0,93/0,16) | Penanda DOM + eliminasi join + sinyal mute buta | Fitur OFF; tanpa jalur |
| I9 | Kata pendek hilang/halusinasi ("dua"/"empat") | Provider + tanpa biasing angka + VAD default memotong onset (`start_secs=0.2`, `audio.py:641-643,812-814`) | Terbuka; anekdot tunggal |
| I10 | Ucapan pertama pasca-hening tak andal | Tanpa pre-roll, keepalive, atau uji kesiapan STT (`audio.py:427-463` reaktif) | Terbuka |
| I11 | Slot macet sunyi | Decoder key basi tanpa supervisor | Diperbaiki `15063bc` (fallback + freshness); tanpa supervisor umum |
| I12 | Biaya N× Silero tak terukur | Satu `SileroVADAnalyzer` default per slot (`stream_capture.py:442-466`) | Terbuka |

## 3. Best practice (ringkas, dengan sumber)

- **Watermark streaming** (Flink/Dataflow): tahan output sampai watermark
  lewat; data telat = side-output tercatat, bukan salah-urutan sunyi.
  [Streaming 101](https://www.oreilly.com/radar/the-world-beyond-batch-streaming-101/),
  [Flink time](https://nightlies.apache.org/flink/flink-docs-master/docs/concepts/time/).
- **Jitter buffer voice** (WebRTC): holdback 50-200 ms untuk susun-ulang
  sebelum decode. [RFC 8108 §5](https://www.rfc-editor.org/rfc/rfc8108.html).
- **Seq + capture clock di sumber**: frame tap keyed `(slot, seq,
  capture_ts)` monoton; gap/duplikat terdeteksi di Python.
- **Diarisasi = segmentasi + embedding + clustering**; identitas di ruang
  embedding, bukan slot transport.
  [pyannote-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1).
- **Voiceprint terdaftar + cosine ≥ ambang** untuk nama stabil lintas sesi;
  di bawah ambang → `unknown`, jangan tebak. ECAPA-TDNN.
- **CSRC = sinyal fusi, bukan kunci identitas** (stabil per join, berubah
  tiap rejoin). [W3C webrtc-pc #1983](https://github.com/w3c/webrtc-pc/issues/1983).
- **Bind-once, unknown-hanya-naik** untuk klaim nama; salah-nama > anonim.
- **Silero tetap** (TPR 87,7% @5% FPR, ~1 ms); energi-only tak layak produksi;
  VAD tahu hening, bukan komplesi → lapisi endpointing semantik.
- **Endpointing = threshold terukur**: pisahkan `endpointing` dari
  `utterance_end`; 300 ms memotong ~22% jawaban panjang (Deepgram).
- **Warmup jalur**: pre-roll ~400-500 ms + buffer selama handshake ASR +
  `warm()` eksplisit sebelum transcribe.
- **Biasing provider**: manfaatkan language/keyword/context biasing Muse;
  pin versi model + harness regresi (provider bisa bergeser diam-diam).

## 4. Arsitektur target (5 lapis + observabilitas)

```
Meet receivers ──► L0 TAP ──► L1 TRANSPORT ──► L2 STT/stream ──► L3 MERGE ──► L4 IDENTITY
 (encoded)        (decode +    (seq + budget +   (sesi per slot,   (watermark +   (fusi →
                   fallback +   heartbeat)       warmup,           holdback,       speaker-uuid,
                   health)                       continuity)       side-output)    label tampil)
                                            OBSERVABILITAS (journal + report + oracle)
```

- **L0 Tap.** Encoded-tap + decoder Opus per key dipertahankan; ditambah
  sinyal kesehatan kontinu `tap_healthy/decode_fresh/fallback_active` dan
  supervisor: slot macet sunyi → fault + rescan otomatis (generalisasi
  `15063bc`, menutup I11/I1-regresi).
- **L1 Transport.** Drain JS menyertakan `(seq, capture_ts)` monoton per slot;
  Python memvalidasi gap/duplikat (`drain_gap` tercatat); budget throughput
  Task 3 ditegakkan + backpressure tercatat (batas `pending` + kebijakan
  buang). `capture_ts` hanya untuk urutan relatif per slot (waspada skew jam
  halaman vs container).
- **L2 STT per stream.** Satu sesi per slot dipertahankan; ditambah pre-roll
  ring ~500 ms (kompensasi `start_secs`), keepalive + reconnect proaktif
  (I10), biasing angka/istilah + `language_bias` (I9), `VADParams` di settings
  + tune dari data (I9), ukur biaya CPU N× Silero sebelum tambah slot (I12).
- **L3 Merge.** Watermark per stream keyed by `capture_ts` + holdback kecil
  yang BESARNYA DITENTUKAN DARI UKURAN (distribusi keterlambatan final di
  oracle), bukan tebakan; final pasca-watermark → emit + `order_inversion_detected`
  side-output. Alternatif bila holdback melanggar gate latensi: publish
  segera + repair-on-read di `MeetingRecord` (eventual order). Menutup I3/I2.
- **L4 Identitas.** Tabel fusi: sinyal (slot, CSRC sync, roster+nama DOM,
  track, level) → baris `speaker-uuid` (keyakinan, margin, status
  confirmed/provisional/unknown, riwayat). Aturan: satu-ke-satu + ambang +
  margin; bind-once per uuid; konflik → unknown + fault. Letter label
  dipertahankan sebagai label tampil. Tenure `_follow_csrc`/delete-one menjadi
  SINYAL fusi, bukan keputusan (menutup I6/I7). Jalur nama: `display_claim`
  (tak-terverifikasi, fusi keyakinan tinggi) vs `verified_name` (hanya via
  enrollment voiceprint eksplisit) — tidak pernah dari DOM saja (I8).
- **Observabilitas.** Setiap lapis: knob settings + metrik journal/report +
  oracle harness. Metrik baru: delay endpointing speechEnd→final (p50/p95),
  churn interim, WER/tepat-kata suite pendek+angka (perluas oracle PS-3/PS-7
  dari atribusi ke isi kata), tingkat inversi, rasio false-trigger/no-speech,
  waktu-tunggu tap, ukuran/umur continuity buffer.

## 5. Rencana fase

### Fase 0 — pre-merge (quick-win, tiap butir SDD: tes merah → fix → reviewer)

1. `[moved]` guard: tolak bila `len(matches) != 1` + tes regresi (I6).
2. Jurnal `tenure_moved/tenure_released` (label, slot, CSRC, roster_size) +
   fault saat slot hadir berganti label — relabel sunyi jadi alarm (I6).
3. Sertakan CSRC di journal jalur stream (I6, audit setara mixed).
4. Perbaiki dormant-drop: kembalikan ke dormant bila slot asal terisi (I6).
5. Dokumentasikan `speaker_bound` sebagai "1:1 saja" (I6).
6. Tes inversi late-arrival (merah → dokumen perilaku kini) + jurnal
   `order_inversion_detected` nol-ubah-perilaku (I3, observasi dulu).
7. Ekspos `VADParams` ke settings tanpa ubah default (I9, knob dulu).
8. Vocabulary angka/istilah ke `speech_keywords` (I9).
9. Metrik endpointing + tap/fallback ke journal/report (observabilitas).
10. Suite harness ucapan-pendek + angka WER/tepat-kata (I9, ukur anekdot).

### Fase 1 — struktural (cabang lanjutan, desain + harness per butir)

11. Seq + capture clock end-to-end + `drain_gap` (L1).
12. Watermarked merge + holdback terukur, ATAU record-repair — putuskan dari
    angka inversi/keterlambatan Fase 0, dengan bukti nol-tambah-latensi agent
    (L3; gate latensi dipertahankan).
13. Supervisor tap + rescan otomatis (L0).
14. Pre-roll + keepalive + reconnect proaktif + uji kesiapan STT (L2).
15. Tabel fusi identitas menggantikan slot-tenure-only; satukan substrat
    stream + mixed ke uuid (L4).
16. Endpointing semantik client-side + pra-generate dari interim (L2/L3).

### Fase 2 — strategis (milestone riset tersendiri)

17. Voiceprint store + enrollment eksplisit (butuh dependency model baru;
    kebijakan privasi/biometrik diputuskan dulu).
18. Pass diarization offline (pyannote/community) + RTTM sebagai koreksi
    pasca-sesi (`corrected_speaker_uuid`, tanpa ubah live record).
19. Jalur `verified_name` + kalibrasi ambang presisi ≥0,98 di room live.

## 6. Checklist kesiapan push/merge

- [ ] Fase 0 lengkap: tiap butir tes-merah-dulu + reviewer terpisah + commit.
- [ ] Gate hijau penuh: suite serial + browser + ruff + mypy (output aktual).
- [ ] Flag default tetap OFF; cutover commit terpisah setelah approval.
- [ ] Bukti nol-tambah-latensi (p50/p95 speechEnd→first-pcm vs base).
- [ ] Laporan acceptance dimutakhirkan dengan angka Fase 0.
- [ ] Push/merge dilakukan pemilik repo (agen tidak push/merge).

## 7. Risiko dan belum terjawab

- Holdback vs gate latensi: belum ada angka; Fase 0 butir 6/9 wajib
  menghasilkan distribusi keterlambatan + tingkat inversi SEBELUM pilih desain.
- Tanpa voiceprint, rejoin orang-sama vs orang-baru tak terbedakan —
  semua heuristik slot/CSRC/device hanya menunda salah-label.
- `page_ms` sebagai capture clock sementara belum terbukti monoton.
- Error rate kata pendek belum pernah diukur (anekdot "dua"/"empat" tunggal).
- Ambang energi JS tak terkalibrasi; VAD bersama vs N× Silero belum dibandingkan.
- Follow-up di luar scope: chatter agent (`/tmp/lt-arch-chatter.md`),
  infra reliabilitas (`/tmp/lt-arch-infra.md`) — riset selesai, eksekusi
  terpisah.
