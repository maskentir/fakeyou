# Laporan acceptance capture per stream

Tanggal: 2026-10-06. Branch: `feat/per-stream-capture`.
Cakupan: spec `docs/plans/per-stream-capture.md`, rencana
`docs/plans/per-stream-capture-plan.md` Task 1–12.

## Vonis

Jalur per-stream **diterima bersyarat**: capture beridentitas,
atribusi, ketiadaan-miss, dan rejoin terbukti di room Meet live
dengan bukti angka di bawah. Yang belum: langkah cutover
penghapusan jalur mixed (Task 6 Langkah 5) dan tindak lanjut
triase. Rekomendasi flag di akhir laporan.

## Tabel gate

| Gate (spec) | Hasil ukur | Bukti |
| --- | --- | --- |
| Overlap pisah 2 teks benar ≥95% | LULUS kalimat, parsial angka | PS-2: "berbicara sekarang" + "Saya sedang berbicara depan" detik sama; PS-1F: split 2 stream, "dua" hilang + "empat" halusinasi STT |
| Atribusi solo ke stream benar ≥95% | LULUS 10/10, 0-miss otomatis | PS-1F 10/10; PS-2 9/10 pra-fix (giliran 6 hilang); PS-3 198 pasang 0 miss; PS-7 54 pasang 0 miss |
| Presisi nama ≥0,98 | tidak diuji (vakum) | Fitur speaker-names mati sesuai keputusan (gate SPK gagal pra-cabang); binder huruf a/b/c/d stabil sebagai identitas |
| Akhir-ucapan → finalisasi p50 ≤3 dtk, p95 ≤6 dtk | LULUS 0,0/0,0 dtk | spoken_at → finalized di semua sesi; metrik PCM-pertama belum instrumentasi langsung |
| Gate lama dipertahankan | LULUS | Replay probe-attr-8 + csrc-live-3 hijau, gate semantik hijau, laju c04 hijau, tanpa xfail |
| Suite offline penuh | LULUS 3136 passed | `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider` → 3136 passed, 40 skipped, 41 deselected, exit 0 (angka deterministik reviewer final) |
| Suite browser | LULUS 8 passed | `pytest tests/test_stream_capture.py -o addopts= -m browser` → 8 passed (termasuk 2 tes racun mixer), exit 0 |
| Statis | LULUS | `ruff check`, `ruff format --check`, `mypy src` bersih |
| Identitas 4 pihak | LULUS | PS-7: 121 attributed, 0 unknown; label a/b/c stabil per slot 118/121 (~97,5%); 3 sisanya re-mint delete-one pasca-kepergian (by design). `speaker_bound` False multi-party: device binding hanya 1:1, label-lah identitasnya |
| Rejoin | LULUS seamless (sudut pandang A) | PS-7 D2: ucapan mendarat di slot/track D-lama dengan label lanjutan speaker-c — tanpa label baru, unknown, churn. B terpin di slot-1/b via teks persis, jadi slot-2/c = D-lama lalu D2 |

Satu-satunya merah lokal (`test_flag_off_keeps_mixed_path`)
adalah artefak env: gagal jika dan hanya jika `.env` live
gitignore menyetel flag, lolos netral dan di CI, tidak
terkait hunk diff mana pun (terbukti reviewer final).
(Klaim "gagal identik di base" pada draf ditarik: tesnya
tidak ada di base; uji stash terdahulu menyisakan file
tes versi branch sehingga tidak valid.)

## Sesi live

- Twin-twin TT-A2/TT-B: 4 giliran dua arah, semua
  `slot-2/speaker-a/stream-2`, loop agent penuh. Verifikasi
  mesin decode Opus pertama.
- PS-1F (2 manusia + twin): 10/10 atribusi, mute 2×30 dtk
  bersih, overlap angka parsial, rejoin terekam (ID baru).
- PS-2 (skrip baru): overlap kalimat LULUS; solo 9/10 —
  giliran 6 hilang di jendela reconnect STT → Task 8.
- PS-3 (3 twin + manusia): oracle silang 198 pasang 0 miss;
  hipotesis gema GUGUR (loop Sip/Okay = percakapan asli).
- PS-4 (4 twin): semua `unknown` → akar mixer-CSRC +
  roster-1 terbukti (CSRC 3707908267 di semua receiver).
- PS-5 (4 twin): pinball hilang (sync-only valid), tapi
  filter-nama makan saudara kembar (post=1) → Task 12.
- PS-6/PS-7 (4 twin): a/b/c distinct 1:1, 0 unknown,
  rejoin seamless, oracle 54/54.

## Akar penyebab yang diperbaiki (SDD penuh)

- Task 8 (`7a9ea08`): audio ke socket STT mati dibuang
  diam-diam selama reconnect antre → buffer kontinuitas
  30 dtk + reconnect segera saat socket mati + tag stream.
- Task 9 (`29b1192`): roster termasuk-self 4 entri
  melebihi kapasitas 3 → self-filter nama, kapasitas
  fleet, tenure dormant, alasan + ukuran roster di journal.
- Task 11 (`ee38711`): contributing-sources mixer racuni
  CSRC per slot → readCsrcs hanya-sync + diagnostik
  roster (tiles/pre/post) di heartbeat.
- Task 12 (`7aa82af`): filter nama salah saat nama
  tabrakan → saring hanya kecocokan tunggal, gate
  demand-side, rilis satu tenure per kepergian.
- Task 10 (`8017255`): reply-all memantulkan ack
  selamanya → ack-dalam-ack-run di-silent + dicatat
  (ack terisolasi, pertanyaan, substantif tetap dibalas).

Setiap task: tes merah dulu, implementer + reviewer
terpisah, suite penuh + browser + statis hijau, commit
lokal tanpa push/merge.

## Risiko dan triase (tidak memblokir vonis)

1. Infra pra-ada: checkpoint SQLite 100MB macet
   (starter ke-4 crash, agent mati DatabaseError tak
   pulih); preflight sekali-gagal-langsung-mati;
   guard ID bakar sesi gagal-start; dialog manual Meet.
2. Loop sapaan ("Halo." 15 giliran) bertahan —
   follow-up subsistem greetings, bukan ack (ruling
   review Task 10).
3. Kualitas STT kata pendek eksternal (provider).
4. Caveat harness: ucapan pertama setelah hening
   panjang tak andal; twin butuh bootstrap manusia.
5. Catatan minor review Task 8/9/11/12 (tercatat di
  status; dokumentasi + diagnostik lanjutan).

## Rekomendasi flag dan langkah berikut

- `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default
  TETAP MATI di commit ini.
- Cutover (hapus jalur mixed + nyalakan default) sebagai
  commit terpisah HANYA setelah pengguna menyetujui
  laporan ini — sesuai Task 6 Langkah 5.
- Berikutnya: review final seluruh branch (berjalan),
  lalu keputusan cutover.

## Adendum Fase 0 (rencana jangka panjang)

10/10 butir Fase 0 selesai, tiap butir SDD
(tes merah → implementasi → reviewer terpisah →
commit lokal): guard `[moved]` (`6f6b3bf`), jurnal
tenure + CSRC stream + dormant-defer
(`d046dc3`), observasi inversi
`order_inversion_detected` (`f53dce3`), knob VAD
(`832f82c`), vocab angka per bahasa profil
(`669056a`), metrik `stt_turn_recorded` + engine +
`stt_quality` (`10ee930`), harness WER
(`aa34906`), rename taksonomi (`ab465b5`).

Gate pasca-Fase 0 (output aktual):
suite serial 3182 passed / 40 skipped /
41 deselected; browser 8 passed;
`ruff check` + `format` bersih (209 berkas);
`mypy src` bersih (56 berkas). Flag default
tetap MATI; checklist merge terpenuhi kecuali
keputusan push/merge pemilik repo.
