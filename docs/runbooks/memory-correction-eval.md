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
  terkoreksi tanpa token basi (deterministik; tanpa judge).
- `over_invalidation`: edge jendela stabil yang ikut terinvalidasi (harus 0).
- `entity_merge_rate`: penutur terkoreksi live + koreksi menjadi satu node.
- `abstention_rate`: query abstention yang menjawab kosong (diagnostik).

## Status

Gate HIJAU di atas Opsi C (invalidasi bedah `ingest_corrections`): 3 run
live beruntun lolos `invalidation_recall` 1,0 + `superseded_serve_rate` 0
+ `over_invalidation` 0 pada fixture v1 (±3–4 menit/run). `corrected_answer_accuracy`
dan `entity_merge_rate` bersifat diagnostik (dilaporkan, bukan gate) karena
cakupan ekstraksi LLM per jendela kecil bervariasi antar-run — detail dan
angka: `.superpowers/sdd/phase-2-memory-correction/task-3-report.md`.
Harness menulis `ingestion.json` (peta jendela→episode) setelah ingest awal,
meniru langkah persistensi CLI produksi yang dipakai `ingest_corrections`
untuk menemukan edge asli yang harus diinvalidasi.
