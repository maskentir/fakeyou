# Deep Research: Testing & Eval untuk Konsumen Koreksi Diarization

Tanggal: 2026-10-09. Bahasa: Indonesia. Tujuan: fondasi riset sebelum plan
implementasi follow-up konsumen (debrief + ingestion memakai
`corrected_speaker_uuid`), supaya fitur teruji mengikuti best practice dan
bukan fitur sampah.

Ruling riset: dikerjakan inline satu agen, bukan apparatus multi-agent
`research-deep`/`research` (butuh outline.yaml + agent Task yang tak ada di
harness ini; pertanyaan sempit dan koheren lebih baik dijawab langsung
dengan sumber terverifikasi).

## 1. Apa yang bikin fitur memori jadi sampah (definisi kerja)

Dari literatur + pengalaman repo ini, fitur memori LLM gagal secara senyap
bila: (a) perilaku LLM (ekstraksi/resolusi/invalidasi) diasumsikan tanpa
diuji; (b) tak ada metrik regresi — perubahan prompt/model merusak diam2;
(c) eval hanya "kelihatan benar" sekali jalan, tanpa fixture berversi yang
bisa di-rerun; (d) decay tak termonitor (fakta basi tetap disajikan).
Riset ini dirancang menutup keempatnya.

## 2. Fondasi repo: kuat di unit, kosong di live-eval

Pola yang sudah ada (`tests/fakes.py::make_graphiti`,
`tests/test_ingestion.py`):

- Fake Graphiti autospec + skrip (`facts`, `invalidated`,
  `query_sensitive`, gate async, `removed`) — mendukung TDD tanpa Neo4j/LLM.
- Tes perilaku nyata: chaining saga, idempotensi resume, timeout/stop,
  event jurnal per jendela, instruksi ekstraksi (atribusi + kosakata).
- Recall: filter `_invalidated` sudah ada — koreksi via `invalid_at`
  otomatis dihormati pembaca.

Kesenjangan untuk koreksi:

- Fake belum memodelkan `get_nodes_and_edges_by_episode` (perlu untuk uji
  Opsi C) dan belum ada skrip kontradiksi/invalidasi otomatis (perlu bila
  Opsi A dipilih).
- UUID episode per jendela tidak disimpan di status ingestion — tanpa ini
  koreksi bedah tak bisa menarget (temuan laporan Graphiti §2).
- Nol eval live: tak ada harness yang menjalankan Neo4j + LLM asli
  terhadap fixture berversi. Semua perilaku LLM hari ini unverified.

## 3. Lansekap benchmark: pinjam metriknya, bukan datasetnya

LongMemEval (ICLR 2025, 500 pertanyaan, 5 kemampuan): extraction,
multi-session reasoning, temporal reasoning, knowledge-update (KU),
abstention. Metrik standar: recall@5/recall@10/NDCG + akurasi jawaban
via LLM-judge. Untuk koreksi kita yang relevan: KU (fakta berubah —
"berapa tingkat sistem menyajikan nilai basi setelah fakta berubah",
diukur eksplisit oleh datapace/agent-memory-benchmark) dan abstention
(tidak mengarang saat bukti dihapus).

Deep Memory Retrieval (paper Graphiti): QA retrieval atas riwayat panjang;
Zep/Graphiti 94,8%. Relevan sebagai pembanding arsitektur, bukan harness.

Keputusan: jangan adopsi dataset LongMemEval utuh (domain chat umum,
mahal, tak menguji atribusi speaker meeting). Adopsi TAKSONOMI metriknya
dan bangun fixture domain sendiri (meeting ID-EN + koreksi atribusi).

## 4. Strategi testing 4 lapis (rekomendasi)

Lapis 1 — unit dengan fake (CI, tiap commit): wiring koreksi —
pemetaan jendela→episode tersimpan; invalidasi hanya menyentuh edge
jendela berubah; episode koreksi dirantai saga dengan
`source_description` bertanda; record tak tersentuh; idempotensi
(koreksi 2x = 1x). Semua deterministik, tanpa LLM.

Lapis 2 — fixture emas ekstraksi (CI, tanpa LLM bila di-cache):
pasangan (episode_body → edge yang diharapkan) berversi di
`evals/fixtures/`. Hari ini tak ada; bangun dengan merekam SATU run
live lalu manusia memverifikasi ("golden master" yang dikurasi, bukan
snapshot buta). Regresi prompt/model tertangkap diff.

Lapis 3 — eval live berversi (malam hari / pra-merge, bukan tiap commit):
Neo4j sekali pakai + LLM asli, fixture meeting sintetis ID-EN dengan
koreksi atribusi yang diketahui. Metrik §5. Setiap kegagalan baru menjadi
fixture Lapis 1/2.

Lapis 4 — shadow produksi (pasca-merge): job koreksi berjalan ganda —
tulis ke grup bayangan, tidak memengaruhi recall produksi; bandingkan
recall produksi vs bayangan atas query sampel; LLM-judge buta menilai
pasangan jawaban; manusia mengadili sampel. Naik ke produksi penuh bila
selisih shadow ≥ ambang selama 2 minggu.

## 5. Metrik eval koreksi (headline + diagnostik)

Headline (gate ship, target TDD):

- `superseded_serve_rate` = 0: atas N query yang jawabannya berubah oleh
  koreksi, nol jawaban menyajikan fakta basi. (Adopsi datapace.)
- `invalidation_recall` = 1,0: tiap edge dari jendela yang berubah mendapat
  `invalid_at` (Opsi C) atau ter-resolve sebagai duplikat/kontradiksi
  (Opsi A). Cek deterministik dari graf — tanpa judge.
- `corrected_answer_accuracy`: LLM-judge (terkalibrasi §6) menilai jawaban
  recall atas query fixture cocok jawaban emas terkoreksi.

Diagnostik (evolusi, bukan gate):

- `entity_merge_rate`: "Harun" live + "Harun" koreksi → satu node.
- `over_invalidation`: edge jendela TAK berubah yang ikut terinvalidasi
  (harus 0; menangkap kecerobohan bedah).
- `abstention_rate`: query yang buktinya dihapus → "tidak tahu".
- Biaya: token LLM + detik per jendela terkoreksi; latensi recall p50/p95
  sebelum/sesudah (pagen `reembed_group` bila embedding berubah).

Angka N: mulai 30 query fixture (10 berubah-subjek, 10 stabil/regresi,
5 abstention, 5 multi-jendela); Wilson 95% menyusul bila efek kecil.
JANGAN klaim presisi statistik dari N=30 — ini smoke regresi, bukan
sertifikasi (pelajaran butir 19: gate pada CI, bukan titik).

## 6. LLM-judge: terakhir, terkalibrasi, bukan satu-satunya

Urutan pembuktian per metrik: cek deterministik dulu (`invalid_at`
terisi? UUID menempel? teks jawaban mengandung subjek benar — pencocokan
string), judge hanya untuk kesetaraan semantik jawaban. Bila memakai
judge: kalibrasi contra sampel manusia (target Cohen κ ≥ 0,8 ala
praktik klinis 2025), juri 2 famili model untuk kasus sengketa, dan
catat versi model judge di artefak eval. Judge tak tervalidasi =
rubber-stamp — explicitly forbidden.

## 7. Spike pra-plan yang disempurnakan (menggantikan §6 laporan Graphiti)

Prasyarat: Neo4j sekali pakai + kunci LLM repo. Fixture: 3 jendela
mainan ID (2 stabil, 1 salah-subjek) + 10 query (6 jawab-berubah,
2 stabil, 2 abstention).

1. Baseline: ingest, ukur corrected_answer_accuracy (ekspektasi: 6 gagal).
2. Opsi A: ingest koreksi; ukur invalidation_recall +
   superseded_serve_rate; catat `valid_at` lama vs baru per edge.
3. Reset; Opsi C: invalidasi bedah + ingest koreksi; ukur metrik sama +
   over_invalidation.
4. Entity: "speaker-A"→"Harun" merge? catat.
5. Keputusan: A bila invalidation_recall = 1,0 dan superseded = 0 dalam
   3 run ulang (stabilitas LLM); jika tidak, C. Biaya/run dicatat.

Keluaran spike: pilihan opsi + fixture Lapis 3 v1 + angka baseline.
Tanpa spike ini, plan implementasi menebak perilaku LLM — tepatnya pola
fitur-sampah yang ingin dihindari.

## 9. Hasil spike (2026-10-09, Neo4j sekali pakai + LLM/granite produksi)

Tiga jendela ID + koreksi ("Harun→Annisa menugaskan Budi"), 3 run per
opsi. Opsi A dan C SAMA-SAMA mencapai invalidation_recall 1,0 (3/3):

- Opsi A: episode koreksi dengan `reference_time` = waktu koreksi → edge
  baru `valid_at`=now > lama=T0 → gate strict lolos → lama diinvalidasi.
  Kekhawatiran §3 laporan Graphiti (valid_at identik) TIDAK terjadi
  selama aturan desain dipatuhi: episode koreksi wajib memakai waktu
  jalan koreksi, BUKAN waktu jendela asli.
- Opsi C: update Cypher langsung (`SET invalid_at/expired_at`) 3/3
  deterministik. Catatan: `edge.save()` mentah CRASH
  (`setRelationshipVectorProperty`, embedding null saat dibaca) — produksi
  wajib Cypher sempit, bukan save().
- Recall mentah Graphiti TETAP mengembalikan fakta basi (2/6 run di
  peringkat 1) — filtering terjadi di lapisan repo (`_invalidated` di
  `describe_facts`), bukan di Graphiti. Eval WAJIB mengukur di lapisan
  repo, bukan `search` mentah.
- Resolusi entitas: Harun/Annisa merge 1 node lintas episode (6/6).
- Instabilitas ekstraksi: edge per episode bervariasi antar run identik
  (fix: 4/2/2/2/1/1; satu w2 0-vs-1). Eval menegaskan properti, bukan count.
- Temuan samping: episode satu-ujaran satu-entitas mengekstrak NOL edge
  (6/6) — jendela produksi multi-ujaran aman, tapi fakta dari ujaran
  terisolasi bisa hilang. Follow-up kualitas ekstraksi terpisah.

Keputusan spike: Opsi A diterima BERSYARAT (aturan reference_time +
eval regresi invalidasi); Opsi C sebagai fallback bila flakiness muncul
di fixture produksi. Alasan: A nol kode bedah + riwayat otomatis, dan
3/3 stabil di sini — dengan sliced eval mengawasi gate waktu.

## 8. Bentuk plan yang direkomendasikan (pratinjau, bukan plan)

- Task 0: spike §7 (bukan kode produk; artefak eval berversi).
- Task 1: penyimpanan pemetaan jendela→episode + tes Lapis 1.
- Task 2: penulis koreksi (opsi pemenang) + tes Lapis 1 + fixture emas.
- Task 3: wiring debrief `load_corrected_record` + tes (tanpa Graphiti).
- Task 4: harness eval Lapis 3 + gate metrik + docs; shadow Lapis 4
  sebagai follow-up eksplisit (butuh infra produksi).

## Sumber utama

- Kode repo: `tests/fakes.py`, `tests/test_ingestion.py`,
  `src/fakeyou/memory.py`, `src/fakeyou/ingestion.py`
- LongMemEval (arXiv:2410.10813, ICLR 2025) via ringkasan ferrosa-memory
  + harness laputa/memonative/myco-brain
- datapace-ai/agent-memory-benchmark (metrik superseded-value)
- Zep/Graphiti DMR 94,8% (paper arXiv:2501.13956 via sitasi sekunder)
- Praktik LLM-judge: letsdatascience (arsitektur eval), audit-llm
  (validasi judge), MDPI perioperative κ≈0,81
