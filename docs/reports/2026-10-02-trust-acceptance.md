# Acceptance Memori Tepercaya 2026-10-02

Laporan fixup reviewer B1–B9 untuk milestone memori tepercaya. Acuan kontrak
pada [rencana trustworthy-memory](../plans/trustworthy-memory.md) dan prosedur
pada [runbook acceptance](../runbooks/trust-acceptance.md).

Konteks: branch `feat/trustworthy-memory`, HEAD `731e7a5`, working tree kotor
(hasil worker + fixup ini, belum commit). Flag streaming kalimat mati
(`sentence_streaming` mencetak `False`).

## Verdict

| Verdict | Nilai | Bukti |
| --- | --- | --- |
| Kontrak/persistence | Terbukti pada cakupan tes | Gate offline hijau; 2 roundtrip live lulus; residu nol; container 6/6 |
| Kualitas trust | Belum terbukti | Replay contract-only; judge belum divalidasi; nol output produksi dinilai |
| Fungsi live | Belum terbukti | Nol sesi Meet; butuh link baru dan kesiapan host |
| Performa | Belum terbukti | Nol sampel latensi live |

Kelulusan tes unit atau korpus sintetis tidak menyatakan twin siap menggantikan
owner tanpa pengawasan.

## Gate offline

`pytest` default: 1703 lulus, 21 dilewati (prabawaan), 31 tidak dipilih,
0 gagal, 37 detik. Enam kegagalan temuan B2 sudah hijau: 1 aturan bahasa pada
`evals/test_trust_eval.py` dan 5 command dokumen pada runbook. `ruff check`,
`ruff format --check`, `mypy`, dan `git diff --check` bersih.

## Temuan B1: protokol UUID terbukti benar

Diagnosis awal menyatakan jalur owner note (pre-create episode lalu
`add_episode` memakai UUID yang sama) tidak menghasilkan fact edge. Probe A/B
terkontrol fixup memakai teks identik membantah klaim itu:

- `add_episode` tanpa UUID: 1 edge + 2 node.
- Pre-create episode lalu `add_episode` memakai UUID yang sama: 1 edge + 2
  node, fakta identik.
- `remember_owner_note` end-to-end: 1 fakta, episode sesuai ID operasi.

Kegagalan awal berasal dari teks catatan, bukan protokol. Kalibrasi memakai
grup sintetis terpisah (2 sampel per bentuk, semua disapu bersih):

| Bentuk teks | Edge per sampel |
| --- | --- |
| `Orchid demo moves to Thursday {run}.` | 0, 0 |
| `Rani moved the {project} customer demo to Friday.` | 0, 1 |
| `Rani announced that the {project} customer demo moves to Friday morning.` | 1, 1 |
| `Rani announced that the {project} customer demo moves to Monday morning.` | 1, 1 |
| `Rani announced that the {project} customer demo moves to Thursday morning.` | 1, 1 |
| `Comparison marker {group} prefers jasmine tea.` | 0, 0 |
| `The comparison team prefers jasmine tea on Friday.` | 0, 0 |
| `The comparison team ordered jasmine tea for the Friday tasting.` | 0, 0 |
| `Rani announced the comparison team serves jasmine tea on Friday morning.` | 1, 1 |

Fragmen pendek dan relasi statis (`prefers`, `ordered`) tidak menghasilkan
fact edge; kalimat lengkap transitif stabil. Satu galat transien
`LLM returned an empty response` pulih sendiri selama kalibrasi.

Tindak lanjut: tanpa perubahan `owner_notes.py`. Teks tes live diganti ke
bentuk kalimat lengkap yang terkalibrasi; runbook operator mewajibkan kalimat
lengkap dan retry memakai `--note-id` yang sama bila ekstraksi kosong.
Diagnosis awal tetap tersimpan sebagai arsip.

## Roundtrip live

Dua tes yang masuk tree dari patch worker lulus dalam satu run (231 detik):

- `test_owner_note_correction_roundtrip_survives_reopen`: koreksi, retry ID
  sama, tutup/buka client, status `superseded`/`active`, retry pasca-reopen.
- `test_trust_roundtrip_correction_survives_client_reopen`: isolasi grup
  pembanding, ingest meeting, skema `memory_evidence/v1`, `source_at` asli
  terjaga, koreksi, reopen, retry, tanpa catatan kontrol ganda, grup pembanding
  tidak berubah, tanpa bocor lintas grup.

Sweep pasca-run: 6 prefix grup sintetis (`trust_roundtrip_`, `trustfix_`,
`trustfix2_`, `trustfix3_`, `trustfix4_`, `test_owner_notes_live_`) semuanya
nol node. Namespace tes terpisah; tanpa sisa.

## Container

Image `fakeyou-meeting-worker:local` cocok dengan 6 file trust pada host
(6/6 hash sama, diverifikasi fixup). `fakeyou memory-correct --help` dalam
image exit 0 dengan flag `--episode`, `--source-at`, `--note-id`.

## Temuan lain

- B3: kedua patch tes bersama masuk tree (`test_library_contracts.py` +
  `test_providers.py`), termasuk 1 assertion anti-variabel mati. Patch task5
  mentah menunjuk `tmp/tp_new.py` sehingga diterapkan manual.
- B4: `RECALL_RULES` diperbarui ke format `memory_evidence/v1` (fakta + sumber
  bertanggal + kind), aturan tanggal sumber lawan tanggal kegiatan, dan guard
  sapaan nama; 3 regresi persona baru.
- B5: `memory-correct` didokumentasikan pada README; laporan ini menutup
  close-out.
- B6: plan memakai `read_note_states` sesuai gate factory-verb.
- B7: format blok kode plan dan runbook hijau.
- B9: tanpa perubahan. Campuran superseded+transkrip tanpa note pengganti
  aktif sudah dikeluarkan penuh sesuai plan; regresi
  `test_recall_excludes_pending_superseded_foreign_and_missing_sources`
  membuktikan `meet-x` tidak tampil dan `excluded.superseded` tepat.

## Gerbang terbuka

- Sesi Meet: link uji baru, kesiapan host, 2 sesi/20 jawaban, interupsi/stop,
  4 verdict live.
- Judge: validasi kunci `EVAL_JUDGE_API_KEY` dan review independen output
  model produksi; klaim lulus via self-assessment dilarang.
- Replay trust model produksi `test_live_trust_replay_captures_72_rated_pairs`.
- Commit integrator (file eksplisit, lokal, tanpa push/merge).
