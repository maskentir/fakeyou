# Kernel Characterization Baseline

Status: PR 0 dari
[desain meeting kernel](../plans/2026-10-27-twin-meeting-kernel-design.md).
Tanggal: 2026-10-27. Tidak ada perubahan perilaku production.

## Corpus

Skenario hidup di `evals/fixtures/kernel-scenarios.json` dan dijalankan oleh
`tests/test_session_characterization.py`:

- 26 skenario: 13 executable melawan komponen production saat ini
  (participation decision, Meet roster, contribution gate) dan 13 catalog
  dengan target kernel serta PR yang membuatnya executable.
- 10 berstatus `preserve`: outcome class kernel harus sama dengan saat ini.
- 16 berstatus `intentional_change`: outcome kernel memang harus berbeda.
  Klaim sukses palsu, roster pasti dari sync parsial, dan sole responder
  dari daftar nama tidak menjadi parity target.

Dua perbedaan terpenting yang dikunci corpus:

- `k-p3`: logistics check saat ini dijawab karena `sole_human_participant`
  diturunkan dari daftar nama. Target kernel diam bila presence belum
  complete.
- `k-r2`: sync yang hanya memuat twin saat ini terbaca sebagai ruangan
  kosong yang pasti. Target kernel adalah count range dengan
  `complete=false` ketika voice track tak terpetakan aktif.

## Baseline terukur

Angka berikut diambil dari artefak yang sudah ada di repo, bukan run baru:

| Metrik | Baseline | Sumber |
| --- | --- | --- |
| Turn judge false reply | 0,014 (105 judgements) | `docs/reports/2026-10-26-turn-judge.json` |
| Turn judge missed named | 0 | `docs/reports/2026-10-26-turn-judge.json` |
| Turn judge latency | p50 1126,5 ms, p90 1302,5 ms | `docs/reports/2026-10-26-turn-judge.json` |
| First audio | p50 <= 3,5 s, p95 <= 6,5 s (target beku) | `src/fakeyou/session_targets.json` |
| Attribution coverage | >= 0,9 (target beku) | `src/fakeyou/session_targets.json` |
| Meet action | p95 <= 12 s, sukses >= 95% (SLO D6) | `docs/plans/2026-10-26-twin-sophistication-design.md` |
| Stop to silence | p95 < 1 s (budget D4) | `docs/plans/2026-10-26-twin-sophistication-design.md` |

Rollback thresholds untuk ramp kernel dibekukan di field
`rollback_thresholds` pada fixture corpus. Nilainya mengikuti gate eval
yang sudah ada (`false_reply <= 0,05`, `missed_answer <= 0,10`,
`premature_commit <= 0,02`, missed named 0) ditambah nol severe invariant
violations. `judge_p90_ms_max` 2000 ms adalah baseline p90 terukur plus
margin; ia bukan target produk akhir.

## Stage latency

Taksonomi yang sudah tersedia di `observability/report.py` dipertahankan:
`speech_stopped_to_first_pcm`, `final_to_agent`, `final_to_reply`,
`reply_to_first_pcm`, `final_to_first_sentence`, dan
`first_sentence_to_first_pcm`. Meet action sudah memiliki phase timings
(prepare/typed/sent/confirmed dan queue) di journal.

Gap yang menjadi pekerjaan PR 2 dan PR 7, bukan PR 0:

- `final_received -> intent_decided` sebagai metrik report tersendiri.
- `intent_decided -> evidence_settled`.
- `speech_commit -> first_pcm` yang terikat plan/command ID.
- `stop_detected -> silence` sebagai metrik scorecard.

## Mutation testing

Tidak ada mutation runner di dependency saat ini dan PR 0 tidak menambah
tool baru. Sebagai gantinya, branch kritis yang akan disentuh migrasi
sudah dipin oleh fault tables yang ada: `tests/test_participation.py`,
`tests/test_roster.py`, `tests/test_timeline.py`, dan
`tests/test_meet_actions_ledger.py`, ditambah corpus baru di atas.
Keputusan adopsi mutation runner ditunda sampai modul keputusan murni
kernel ada di PR 2; saat itu surviving branch gaps dinilai ulang.

## Gate PR 0

- Corpus valid, ID unik, dan mencakup kesepuluh area desain.
- Skenario `preserve` memiliki outcome class yang sama antara current dan
  target; skenario lain wajib berbeda secara eksplisit.
- Skenario executable mereproduksi perilaku production saat ini, termasuk
  dua perilaku yang ditandai sebagai perubahan disengaja.
- Tidak ada file production yang berubah.
