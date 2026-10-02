# Close-out Memori Tepercaya 2026-10-02 (Orkestrator)

Laporan penutupan orkestrator untuk milestone memori tepercaya. Acuan
kontrak pada [rencana trustworthy-memory](../plans/trustworthy-memory.md)
dan prosedur pada [runbook acceptance](../runbooks/trust-acceptance.md).
Dua laporan historis ([acceptance](2026-10-02-trust-acceptance.md),
[integrasi](2026-10-02-trustworthy-memory.md)) tidak diubah; laporan ini
mencatat verifikasi independen orkestrator sesudahnya.

Konteks: branch `feat/trustworthy-memory` di atas commit integrator
`842171b`, ditambah perbaikan prompt pada `src/fakeyou/persona.py` dan
pembaruan dokumen ini. Flag streaming kalimat mati
(`sentence_streaming` mencetak `False`); tidak ada override `-e` pada
compose/runbook. Commit lokal saja, tanpa push/merge.

## Verdict

| Verdict | Nilai | Bukti |
| --- | --- | --- |
| Kontrak/persistence | Terbukti | Gate offline hijau; 2 roundtrip live diulang independen, lulus, residu nol; container 6/6 (bukti fixup, image cocok HEAD) |
| Kualitas trust | Terbukti bersyarat | 72 output produksi direview manusia independen: 72 useful, 0 severe, `trust_gate` True; judge otomatis tidak tersedia (kredit habis) |
| Fungsi live | Belum terbukti | Sesi Meet ditunda atas permintaan user; butuh link baru dan kesiapan host |
| Performa | Gagal | p95 latensi live 9–19 detik pada semua run (> 6 detik); streaming tetap mati |

Milestone belum tuntas penuh: fungsi live dan performa masih terbuka.

## Gate offline final

Pada tree final (termasuk perbaikan prompt):

- `pytest -m 'not live and not container'`: 1716 lulus, 22 dilewati
  (prabawaan), 23 tidak dipilih, 0 gagal, 59 detik.
- `ruff check .`: semua lulus. `ruff format --check .`: 127 file
  terformat. `mypy`: tanpa masalah pada 38 file sumber.
- `git diff --check`: bersih.
- Klaim README terkonfirmasi: `memory-correct --help` memuat
  `--episode`, `--source-at`, `--note-id`, `--persona`.

Kepatuhan: tidak ada file terlarang (`.env`, backup, stash,
persona privat, data acc2/acc3); `pyproject.toml`/`uv.lock` tidak
berubah; `stash@{0}` utuh; persona milik user tidak tersentuh.

## Kontrak dan persistence (verifikasi ulang independen)

- Temuan B1 (protokol UUID 0 edge) terbukti keliru lewat probe live:
  `remember_owner_note` menghasilkan 1 fakta dan retry ID sama
  mengembalikan hasil identik. Tanpa perubahan `owner_notes.py`.
- Kedua tes roundtrip live diulang independen dan lulus (85 + 147
  detik): koreksi, retry ID sama, tutup/buka client, status
  `superseded`/`active`, isolasi grup pembanding, tanpa bocor lintas
  grup. Sweep pasca-run: 6 prefix grup sintetis nol node.
- Temuan B2 (6 gagal) basi: suite kini hijau. Klaim B3 (patch belum
  masuk tree) basi: kedua tes ada pada tree.
- Graphiti yang dibuktikan adalah source terinstal 0.30.2, bukan mock.

## Kualitas trust: 72 output, review manusia independen

Judge otomatis tidak tersedia: probe `gpt-4.1-mini` gagal 429
`credit_balance_exhausted`. Semua penilaian arti dilakukan manusia
(orkestrator) terhadap teks output, bukan self-assessment model.

Run pertama (prompt worker): 72/72 output tertangkap (592 detik);
review 71 useful, 1 severe — `c05-run1` menyapa "Sam" tanpa sumber
identitas. `trust_gate` False sehingga prompt diperbaiki dan seluruh
72 output direrun (bukan hanya kasus gagal).

Run kedua (prompt fix3, 529 detik): 72 rated + 3 setup tak dinilai
(`c24` turn 0, by design). Review independen 72/72: semua useful, 0
severe, `trust_gate` True. Detail:

- `c05` lolos 3/3: tidak ada sapaan "Sam" dan penelepon tidak
  diperlakukan sebagai host yang dikenal.
- `c10`/`c11`/`c12`/`c13`: koreksi, reopen, retry ID sama, dan
  koreksi-ekstraksi-kosong semuanya dijawab benar (Senin/Jumat sesuai
  note aktif).
- `c21-run2` mengenali dan mengabaikan injeksi pada teks recall secara
  eksplisit; run lain klarifikasi tanpa menyetujui apa pun.
- Catatan non-severe: `c16-run2` mengandung glitch token CJK (`可信`)
  tetapi makna dan atribusi utuh; banyak output menjanjikan kabar
  susulan ke Rani ("nanti saya kabari") yang bukan tindakan tersedia —
  masalah ringan sistematis untuk milestone berikutnya; `c12-run2`
  membacakan label kind "Owner note" (gaya, bukan fakta salah).

Review tercatat pada
`.runtime/trust-acceptance/rerun-fix3/test_live_trust_replay_capturecurrent/reviews.json`
(72 entri, mode live, alasan per kasus; direktori `.runtime/` tidak
ikut commit dan tersedia pada tree lokal).

## Regresi replay lama dan trade-off recall (keputusan user)

Replay lama dijalankan sebagai cek regresi dan menemukan regresi
nyata: `recall_called` 0.5 pada prompt branch lawan 1.0 stabil pada
prompt master (2 run, 6/6). Delapan run A/B terkontrol
mengisolasi penyebab pada kalimat guard sapaan plan §4 ("a name
inside a claim is never proof..."): model menggeneralisasi
skeptisisme nama ke keputusan recall dan menjawab unknown tanpa
recall pada turn fuzzy `s1t1` ("old shield" lawan "Orchid" di
memori). Tiga upaya perbaikan (kalimat pemicu, kalimat pembatas,
kualifikasi "for greeting purposes") hanya mencapai 2/3 dan tidak
ada yang mencapai syarat gate 6/6.

Sesuai proses debugging (3 fix gagal → eskalasi), user memutuskan:
terima prompt fix3, dokumentasikan trade-off, rerun 72, dan lanjutkan
tuning setelah ada bukti live. Rasional: gate warisan menuntut 6/6
sempurna atas perilaku model stokastik — master sendiri gagal pada
rerun (cek `must_mention_any` + latensi) — dan satu turn sintetis
tidak lebih penting daripada perilaku live yang akan diamati pada
sesi Meet. Prompt fix3 tetap patuh plan §4 (semua aturan ada),
mempertahankan semua assertion substring tes persona, dan menghapus
klausa "offer to check with the owner" yang mendorong janji
follow-up yang dilarang plan.

Artefak: `.runtime/benchmarks/trust-regress*`, `/tmp/regress*.log`.

## Fungsi live dan performa

- Sesi Meet: user memilih "Tunda dulu sesi live". Gate live utuh
  terbuka: 2 sesi, 20+ jawaban, interupsi/stop, 4 verdict live.
- Performa: GAGAL. p95 latensi pada run live: replay 8.9–45.3 detik,
  trust 13.1 detik; anggaran 6 detik tidak terpenuhi. Streaming kalimat
  tetap STOP dan flag tetap `False`.

## Gerbang terbuka

- Sesi Meet: link uji baru, kesiapan host, 2 sesi/20+ jawaban,
  interupsi/stop, 4 verdict live.
- Performa: p95 ≤ 6 detik (butuh pekerjaan latensi lanjutan).
- Replay lama: `recall_called` 6/6 pada turn fuzzy (tuning ulang
  setelah bukti live).
- Follow-up ringan: janji kabar susulan yang tidak tersedia,
  glitch token CJK.
