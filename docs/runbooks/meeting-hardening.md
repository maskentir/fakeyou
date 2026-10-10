# Hardening Meeting: Resume, Stop, dan Verdict

Prosedur operator untuk leave cepat, resume ingestion, tes stop, probe
fuzzy, dan cara membaca verdict. Acuan kontrak pada
[rencana hardening](../plans/meeting-hardening.md).

## Leave lalu resume ingestion

`join` tidak lagi menjalankan ingestion. Setelah leave, record
difinalisasi dan status ingestion menjadi `pending` beserta hash
final. Jalankan ingestion sebagai tahap operator tersendiri:

```sh
fakeyou memory-ingest --meeting-id MEETING_ID --persona PERSONA \
  --owner-id OWNER --twin-id TWIN
fakeyou report --meeting-id MEETING_ID
```

`memory-ingest` memakai episode native sebagai sumber kebenaran
progress (`resumed_from`); status file bukan penggantinya. Partial
atau cancel menghasilkan `interrupted` dan exit 1; kegagalan
provider menghasilkan `failed` dan exit 1; `completed` menghasilkan
exit 0. Ulangi command yang sama untuk resume; record yang berubah
sejak hash final ditolak eksplisit. Jangan meng-ingest record yang
masih ditulis sesi aktif; satu lock eksklusif per record menolak
writer kedua.

## Tes stop

Jalankan `join` dengan `--barge-in`, lalu hentikan sesi ketika
`request_started` model sudah ada tetapi belum ada
`synthesis_pcm_emitted` pada journal. Bukti yang wajib ada:

- `agent_cancel_requested` dan `agent_cancelled` dengan reason
  (`new_final`, `interim`, atau `close`).
- Nol PCM/sintesis baru sesudah stop; tidak ada fallback model atau
  respons usang.
- Record final tersimpan; status file menjadi `pending`, sedangkan
  `ingestion_outcome` pada report terbaca `unknown` sampai ingestion
  pertama berjalan (lihat bawah).

Cancel yang dibuktikan ialah coroutine/request lokal dan audio;
jangan mengklaim komputasi provider berhenti tanpa acknowledgement
provider. Bedakan audio drop (playback) dari cancel model.

## Probe fuzzy

Ucapkan "old shield" yang dimaksudkan sebagai Orchid, plus kontrol
"Orchid" yang jelas. Model boleh klarifikasi bila ambigu. Probe ini
tidak memaksa recall tiap giliran dan tidak mengubah gate replay
lama; catat trade-off recall apa adanya.

## Membaca verdict

`fakeyou report --meeting-id MEETING_ID` menampilkan dua outcome
terpisah plus bukti cancellation:

- `session_outcome`: `completed` (exit 0), `failed`, atau `unknown`
  bila `session_finished` tidak ada.
- `ingestion_outcome`: `completed`, `interrupted`, `failed` (ada
  fault `memory_ingest`), atau `unknown` bila belum pernah ada
  `memory_ingest_finished`. Status absent/korup bukan completed.
- `cancellation`: jumlah `requested` lawan `completed`.

Outcome ingestion tidak mengubah outcome meeting. Sapaan-saja
dijawab rute netral lokal (`Halo.`/`Hello.`) tanpa memanggil model;
input campuran tetap masuk agent. Klaim nama bukan bukti identitas
atau kewenangan owner.
