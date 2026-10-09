# Eval Koreksi Memori (Lapis 3)

Harness: `evals/test_memory_correction_eval.py`, fixture:
`evals/fixtures/memory-correction-v1.json` (3 jendela + 1 koreksi atribusi +
10 query: 6 berubah, 2 stabil, 2 abstention).

## Prasyarat live

- Neo4j sekali pakai (jangan memakai Neo4j produksi/compose):
  `docker run -d --rm --name eval-neo4j -p 127.0.0.1:7475:7474
  -p 127.0.0.1:7688:7687 -e NEO4J_AUTH="neo4j/<kata sandi>" neo4j:5-community`,
  lalu timpa URI saat menjalankan tes
  (`FAKEYOU_MEMORY__STORE__URI=bolt://localhost:7688`).
- Kunci LLM repo dari `.env` (`FAKEYOU_MEMORY__LLM__API_KEY`,
  `FAKEYOU_MEMORY__EMBEDDER__API_KEY`, `FAKEYOU_MEMORY__STORE__PASSWORD`).
  `Settings` membaca `.env` otomatis, tetapi `EMBEDDING_DIM` dibaca saat
  impor — jadi `.env` wajib termuat di environment proses sebelum pytest
  (`set -a; source .env; set +a` atau `uv run --env-file .env`).
- Layanan embedding lokal bila `FAKEYOU_MEMORY__EMBEDDER__BASE_URL`
  menunjuk ke sana: `docker compose --profile embeddings up -d --wait
  embeddings`, cek `curl -sf http://127.0.0.1:8080/health`.

## Menjalankan

Cepat (tanpa backend, bagian dari suite default):

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest \
  evals/test_memory_correction_eval.py -q -p no:cacheprovider
```

Live (Neo4j + LLM asli, satu run ±2–4 menit):

```sh
set -a; source .env; set +a
export FAKEYOU_MEMORY__STORE__URI=bolt://localhost:7688
export FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false
.venv/bin/pytest evals/test_memory_correction_eval.py -m live -q -p no:cacheprovider -s
```

Setiap run memakai `group_id` unik, jadi satu container bisa dipakai untuk
run berulang. Tanpa Neo4j/kunci, tes live di-skip biasa (bukan xfail).

## Metrik (diukur di lapisan repo `describe_facts`, bukan `search` mentah)

- `invalidation_recall`: fraksi edge jendela berubah yang membawa
  `invalid_at` (deterministik dari graf; gate = 1,0).
- `superseded_serve_rate`: fraksi jawaban query-berubah yang masih menyajikan
  fakta basi (pencocokan string nama penutur basi; gate = 0).
- `corrected_answer_accuracy`: fraksi 8 query terjawab yang memuat token emas
  terkoreksi tanpa token basi (deterministik; tanpa judge; diagnostik sampai
  median-of-3 `>= 0,75` pada run valid, lalu soft gate `>= 0,5`).
- `over_invalidation`: edge jendela stabil yang ikut terinvalidasi (gate = 0,
  hanya dievaluasi bila run valid; run invalid gagal sebagai INVALID).
- `entity_merge_rate`: penutur terkoreksi live + koreksi menjadi satu node
  (diagnostik; promosi ke gate `== 1.0` hanya setelah 3 run valid beruntun
  semuanya `1.0` dengan `fixed_edges >= 1` — riwayat belum ada, belum promosi).
- `abstention_rate`: query abstention yang menjawab kosong (diagnostik, tidak
  dipromosikan).

## Status

Gate HIJAU di atas Opsi C (invalidasi bedah `ingest_corrections`): 3 run
live beruntun lolos `invalidation_recall` 1,0 + `superseded_serve_rate` 0
+ `over_invalidation` 0 pada fixture v1 (±3–4 menit/run) menurut aturan
gate pra-Task 6 (tanpa syarat validitas; run 1 INVALID menurut aturan baru —
lihat kolom `valid`). `corrected_answer_accuracy`
dan `entity_merge_rate` bersifat diagnostik (dilaporkan, bukan gate) karena
cakupan ekstraksi LLM per jendela kecil bervariasi antar-run — detail dan
angka: `.superpowers/sdd/phase-2-memory-correction/task-3-report.md`.
Harness menulis `ingestion.json` (peta jendela→episode) setelah ingest awal,
meniru langkah persistensi CLI produksi yang dipakai `ingest_corrections`
untuk menemukan edge asli yang harus diinvalidasi.

> **Retry (Q3, mendarat di Task 1 follow-up):** idempotensi per-jendela
> sudah ada — status menyimpan `correction_windows` (jendela → episode
> koreksi) + `correction_run`; retry setelah gagal di tengah melewati
> jendela yang sudah sukses (0 duplikat) dan memulihkan orphan se-run
> yang sama. `corrected_at` tetap hanya ditulis saat run selesai penuh.

## Validitas run (anti-vacuus)

Setiap run live wajib valid sebelum gate dinilai. Syarat valid
(`summary["validity"]`): `changed_edges >= 1` DAN `stable_edges >= 1` DAN
`fixed_edges >= 1` DAN `pre_stale_hit` hadir (jawaban pra-koreksi menyajikan
fakta basi). Run yang tidak memenuhi syarat GAGAL sebagai INVALID — tidak
pernah lolos. Tanpa Neo4j/kunci, tes live tetap di-skip (bukan INVALID);
INVALID hanya berlaku saat data live hadir tetapi kosong.

## Angka headline (fixture v1, Opsi C, 3 run hijau beruntun)

| Run | Durasi | valid | inv_recall | sup_serve | akurasi* | merge* | changed_edges | over_inv |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 (`memcorr-d1b16808`) | 177 dtk | TIDAK† | 1,0 | 0,0 | 0,0 | 1,0 | 2 | 0 |
| 2 (`memcorr-abd2f4ad`) | 220 dtk | ?‡ | 1,0 | 0,0 | 0,75 | 1,0 | 2 | 0 |
| 3 (`memcorr-bc151535`) | 170 dtk | ?‡ | 1,0 | 0,0 | 0,75 | 1,0 | 2 | 0 |

\* Diagnostik, bukan gate: akurasi/merge bervariasi antar-run karena cakupan
ekstraksi LLM per jendela mainan (run 1: episode koreksi 0 edge; run 2–3:
changed 6/6, stabil 0/2). Detail forensik: laporan Task 3 di
`.superpowers/sdd/phase-2-memory-correction/task-3-report.md`.

† Run 1 INVALID di bawah aturan baru: episode koreksi 0 edge berarti
`fixed_edges = 0`. ‡ Run 2–3 pra-Task 6 tidak mencatat `stable_edges` /
`fixed_edges` / `pre_stale_hit`, jadi status validnya tak diketahui dan tidak
dihitung sebagai riwayat promosi. Riwayat 3 run valid untuk promosi
`entity_merge_rate` dan soft gate akurasi belum ada — keduanya tetap
diagnostik sampai bukti 3 run terkumpul (rerun live dijadwalkan terpisah).

Biaya/run: waktu dinding ±3–4 menit/run (3 ingest + 1 koreksi + 20 recall +
3 fetch episode; model muse-spark-1.2 + TEI lokal). Token LLM tidak
termeterai — Graphiti tidak mengekspos pemakaian, harness hanya mencatat
durasi; estimasi tetap: 1 episode koreksi per jendela berubah + tanpa judge.
