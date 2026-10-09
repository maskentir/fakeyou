# Eval Koreksi Shadow (Lapis 4, Offline)

Harness: `evals/test_shadow_correction_eval.py`, fixture:
`evals/fixtures/memory-correction-v1.json` (sama seperti Lapis 3: 3 jendela +
1 koreksi atribusi + 10 query: 6 berubah, 2 stabil, 2 abstention). Harness
memakai ulang `answer_queries` (terbatas per grup) + fungsi metrik murni
Lapis 3 tanpa mengubah berkas Lapis 3. Rencana induk:
[phase-2-followups](../plans/phase-2-followups.md) (Tugas 2).

## Konsep dual-run

- Grup A (`prod-sim`): ingest awal saja, tanpa koreksi. Mensimulasikan
  recall produksi sebelum perbaikan diarizasi.
- Grup B (`shadow`): ingest awal + `ingest_corrections` Opsi C (invalidasi
  bedah lewat Cypher sempit). Konvensi nama grup:
  `<prod>-shadow-<capwaktu>` (contoh: `shd-a1b2c3d4-shadow-20260115120000`),
  selalu lolos validasi `group_id` (huruf, angka, `_`, `-`, maks 128).
- Kedua sisi menjawab 10 query fixture yang sama lewat
  `describe_facts` (baca terisolasi per grup), lalu harness menyusun
  artefak diff-report JSON.

## Menjalankan

Cepat (tanpa backend, bagian dari suite default):

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest \
  evals/test_shadow_correction_eval.py -q -p no:cacheprovider
```

Live (Neo4j sekali pakai + LLM asli; prasyarat Docker dan `.env` sama
seperti runbook Lapis 3 di
[eval koreksi memori](memory-correction-eval.md)):

```sh
set -a; source .env; set +a
export FAKEYOU_MEMORY__STORE__URI=bolt://localhost:7688
export FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false
.venv/bin/pytest evals/test_shadow_correction_eval.py -m live -q -p no:cacheprovider -s
```

Setiap run memakai `group_id` unik, jadi satu container bisa dipakai untuk
run berulang. Tanpa Neo4j/kunci, tes live di-skip biasa (bukan xfail,
bukan INVALID). Tes live menulis artefak `shadow-diff-report.json` ke
direktori sementara pytest dan mencetak lokasinya.

## Membaca diff-report

Tiap baris `rows` membandingkan satu `query_id`:

| Kunci | Isi |
| --- | --- |
| `query_id` | id query fixture (`q01`–`q10`) |
| `prod` | jawaban grup A (tanpa koreksi) |
| `shadow` | jawaban grup B (setelah koreksi) |
| `token_match` | peta `prod`/`shadow` ke benar/salah pencocokan token emas (`present` hadir, `absent` absen) |
| `stale_served` | peta `prod`/`shadow` ke benar/salah penutur basi masih tersaji |

Blok `aggregate` merangkum 6 query berubah:

| Kunci | Isi |
| --- | --- |
| `changed` | jumlah query berubah (fixture v1: 6) |
| `superseded_prod` | jawaban basi di sisi prod (cacah; ekspektasi `>= 1`) |
| `superseded_shadow` | jawaban basi di sisi shadow (cacah; ekspektasi `0`) |
| `superseded_rate_prod` | fraksi basi sisi prod |
| `superseded_rate_shadow` | fraksi basi sisi shadow |

Contoh potongan artefak:

```json
{
  "aggregate": {
    "changed": 6,
    "superseded_prod": 6,
    "superseded_rate_prod": 1.0,
    "superseded_rate_shadow": 0.0,
    "superseded_shadow": 0
  },
  "rows": [
    {
      "prod": "Riko Nadia rollout",
      "query_id": "q01",
      "shadow": "Nadia rollout",
      "stale_served": {"prod": true, "shadow": false},
      "token_match": {"prod": false, "shadow": true}
    }
  ]
}
```

## Gate sementara

Shadow menang bila `superseded_shadow == 0 < superseded_prod` pada
fixture. Bila baseline prod menayangkan nol basi (`superseded_prod == 0`),
run live GAGAL sebagai INVALID (penanda basi tidak tertanam, bukan bukti
koreksi) — cermin aturan anti-vacuus Lapis 3. Harness juga menegaskan
`stale_served` sisi prod identik sebelum dan sesudah run shadow.

## Batas (tetap terblokir di sini)

Belum ada sampler query produksi, scheduler + toko metrik promosi,
LLM-judge buta terkalibrasi + adjudikasi manusia, kebijakan tulis shadow
ke Neo4j produksi + isolasi saga lintas grup, metering biaya, dan bukti
non-kebocoran lintas grup di DB bersama. Semua item itu dirujuk kembali
ke rencana induk dan tidak dikerjakan lewat harness ini.
