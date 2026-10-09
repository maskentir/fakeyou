# Deep Research: Memanfaatkan Graphiti untuk Koreksi Diarization

Tanggal: 2026-10-09. Bahasa: Indonesia. Diverifikasi dari
`graphiti-core==0.30.2` terpasang di `.venv` + kode repo; klaim eksternal
ditandai bila tak terverifikasi.

## 1. Cara kerja Graphiti yang relevan

Versi terpasang 0.30.2, driver Neo4j (`bolt://`, lihat
`FAKEYOU_MEMORY__STORE__URI`). Alur tiap `add_episode`:

1. Ekstraksi LLM: node + edge dari `episode_body`, dipandu
   `custom_extraction_instructions` bila ada.
2. Dedup LLM (`dedupe_edges`): untuk tiap edge baru, cari kandidat duplikat
   (hybrid search antar node yang sama) dan kandidat invalidasi (hybrid
   search seluruh grup); LLM mengembalikan `contradicted_facts`.
3. Gate waktu (`resolve_edge_contradictions`): kandidat yang ditandai
   kontradiksi HANYA diinvalidasi bila `valid_at` lama < `valid_at` baru
   (strict). `valid_at` diisi LLM dari teks fakta + `reference_time`
   episode. Tanpa kedua timestamp, tidak ada invalidasi.
4. Hapus: `remove_episode(uuid)` adalah hard delete (edge buatan episode
   itu + node yatim + episode). Riwayat hilang dari graf.

API berguna lain: `retrieve_episodes`, `get_nodes_and_edges_by_episode`,
`add_triplet`, `previous_episode_uuids` / saga chaining,
`entity_types` kustom (tak dipakai repo hari ini).

## 2. Integrasi repo hari ini

`src/fakeyou/ingestion.py`: transkrip dipecah jendela; tiap jendela satu
episode (`EpisodeType.message`, body `"speaker: teks"` per baris,
`reference_time` = awal jendela, dirantai saga per meeting, instruksi
ekstraksi kustom mewajibkan tiap fakta menyimpan speaker sebagai subjek).
Ingestion status hanya menghitung jendela — UUID episode per jendela
TIDAK disimpan. Recall memakai `search_memory_evidence` per `group_id`.

## 3. Opsi memakai Graphiti untuk koreksi diarization

Konteks: setelah `diarize run` menulis sidecar, sebagian entri punya
`corrected_speaker_uuid` yang berbeda dari label live. Fakta yang sudah
ter-ingest dengan subjek salah perlu dikoreksi.

### Opsi A: ingest ulang + harapkan invalidasi otomatis

Ingest ulang jendela terkoreksi sebagai episode baru di grup/saga sama.
 graf mendeteksi "Annisa bilang X" menentang "Harun bilang X" dan
menutup `invalid_at` edge lama. Riwayat utuh, nol kode bedah.

Risiko (alasan perlu spike, bukan asumsi):

- Gate `valid_at` strict: fakta koreksi dan fakta salah merujuk ujaran
  yang SAMA (waktu meeting sama). Bila LLM memberi keduanya `valid_at`
  identik, syarat `<` gagal → TIDAK terinvalidasi, graf menyimpan dua
  fakta bertentangan. `reference_time` episode koreksi (jalan belakangan)
  mungkin menggeser `valid_at` baru ke depan — perilaku LLM, bukan
  kontrak kode.
- LLM mungkin tidak menandai beda-subjek sebagai kontradiksi (dua fakta
  "mirip tapi beda"), terutama bila nama ter-resolve ke node berbeda
  ("Harun" vs "speaker-A").

### Opsi B: remove_episode + ingest ulang

Hapus episode jendela lama, ingest versi terkoreksi. Deterministik,
tanpa ketergantungan penilaian LLM. Kelemahan: riwayat hilang dari graf
(kapan diyakini apa). Audit tetap ada di luar graf (`record.jsonl` +
sidecar koreksi), jadi kehilangan ini terbatas.

### Opsi C (rekomendasi): invalidasi bedah deterministik

1. Saat ingestion, simpan pemetaan jendela → UUID episode (kolom baru di
   status ingestion, additive).
2. Saat koreksi: `get_nodes_and_edges_by_episode` untuk jendela yang
   berubah → set `invalid_at` (dan `expired_at`) edge-edge itu ke waktu
   koreksi → ingest episode koreksi (`source_description` mis.
   `"Google Meet <id> transcript (corrected)"`) dirantai saga.
3. Riwayat utuh (kapan salah, kapan dikoreksi), tanpa judi LLM.

Opsi C memakai Graphiti sebagai temporal store sesuai desainnya, dengan
penulis koreksi yang deterministik dan teruji (tanpa LLM di path tulis;
LLM hanya mengekstrak episode koreksi seperti biasa).

## 4. Resolusi entitas: prasyarat yang sering dilupakan

Ketiga opsi butuh "Harun" (koreksi) dan "Harun"/"speaker-A" (live)
me-resolve ke SATU node orang. Dedup node adalah LLM (nama + konteks),
dibantu bila:

- Episode memakai nama stabil (di sinilah `verified_name` membayar:
  label terverifikasi > label slot > "unknown").
- Format entity-first: nama diulang di tiap klaim, bukan pronomina
  ("Harun memutuskan X", bukan "Ia memutuskan X"). Instruksi ekstraksi
  kita sudah mengarah ke sini; spike harus memverifikasi hasil resolve.

## 5. Biaya

Tiap episode = beberapa panggilan LLM (ekstrak node/edge, dedup,
timestamp). Jendela terkoreksi membayar ekstraksi 2x (asli + koreksi).
Dengan Opsi C, hanya jendela yang BERUBAH yang di-ingest ulang —
hemat vs ingest ulang seluruh sesi. Estimasi kasar menyusul setelah spike
menghitung edge/jendela.

## 6. Desain spike verifikasi (syarat sebelum implementasi)

Prasyarat: Neo4j sekali pakai (container) + kunci LLM yang dipakai
`Graphiti` di repo.

1. Ingest 2 jendela mainan ("Harun: saya memutuskan X", "Annisa: setuju").
2. Opsi A: ingest koreksi ("Annisa: saya memutuskan X"); cek apakah edge
   lama dapat `invalid_at`. Catat ya/tidak + `valid_at` keduanya.
3. Opsi C: invalidasi bedah via API driver; ingest koreksi; verifikasi
   recall hanya mengembalikan fakta baru + riwayat lama terbaca dengan
   `invalid_at` terisi.
4. Uji resolve: "speaker-A" + "Harun" di episode berbeda → satu node?
5. Keputusan: A bila invalidasi otomatis terbukti andal (≥10/10 kasus uji
   beda-subjek); jika tidak, C.

## 7. Rekomendasi

Rute implementasi follow-up konsumen: spike §6 dulu (±1 sesi + biaya
LLM kecil), lalu wiring ingestion memakai Opsi C bila spike gagal
membuktikan A (hipotesis awal: C — gate `valid_at` strict terlalu rapuh
untuk ujaran yang waktunya identik). Debrief memakai
`load_corrected_record` langsung (tanpa Graphiti). Jangan membangun
otomatisasi job sebelum konsumen terbukti membaca koreksi.

## 8. Pertanyaan terbuka

- Apakah LLM timestamp extractor memberi `valid_at` = waktu ujaran atau
  `reference_time` untuk fakta "X bilang Y"? (jawab di spike)
- Ambang kontradiksi dedup untuk beda-subjek: andal atau flaky?
  (jawab di spike)
- Retensi episode salah-subjek setelah invalidasi: biarkan (audit) atau
  purge terjadwal? (keputusan pemilik)
