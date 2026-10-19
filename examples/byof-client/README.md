# Contoh client BYOF

Contoh ini menjalankan alur referensi dari luar layanan, lewat HTTP biasa, seperti frontend
sungguhan akan melakukannya. Ia tidak mengimpor paket `fakeyou`. Satu-satunya dependensi di
luar pustaka standar adalah `httpx2`, yang sudah ada di proyek.

## Prasyarat

- API berjalan dan dapat dijangkau. Di tumpukan Compose, `api` dan `redis` naik lewat
  `docker compose up -d redis api`, lalu `http://127.0.0.1:8000/v1/health` mengembalikan
  `{"status": "ok"}`.
- Owner token tersedia di env `FAKEYOU_API_TOKEN`. Token tidak pernah dicetak oleh contoh.
- Worker konsumen berjalan agar sesi benar-benar dikerjakan. Tanpa worker, sesi tetap di
  fase `joining` dan langkah `events` tidak pernah selesai.

Token sebaiknya diisi dari file lokal atau prompt, bukan dari argumen yang tercatat di
riwayat shell.

## Perintah

```sh
export FAKEYOU_API_TOKEN=...
uv run python examples/byof-client/client.py --base-url http://127.0.0.1:8000 --link https://meet.google.com/abc-defg-hij
```

Jika env `FAKEYOU_EXAMPLE_REPLY_KEY` terisi, contoh mengirim header
`X-Fakeyou-Key-Reply` pada pembuatan sesi. Ini hanya contoh cara mengirim override kunci per
sesi; nilainya tidak pernah dicetak.

Keluaran sukses berupa JSON berisi `steps`, `meeting_id`, `phase`, `terminal_reason`,
`events_received`, `ingestion`, `debrief`, dan `report`. Kegagalan menghentikan contoh dengan
kode katalog yang terbaca di stderr dan exit code 1. Token yang tidak diisi berakhir dengan
exit code 2.

## Langkah dan operasi OpenAPI

| Langkah | Operasi | Keterangan |
| --- | --- | --- |
| `create` | `POST /v1/sessions` | Mengirim persona dan brief contoh inline. Field `meeting_id` di brief dibuang, dan contoh membuat `meeting_id` sendiri. |
| `join` | `POST /v1/sessions/{meeting_id}/join` | Menjadwalkan join lewat stream perintah. |
| `events` | `GET /v1/sessions/{meeting_id}/events` | Mengikuti SSE sampai event `session_ended`. Satu reconnect disimulasikan: koneksi pertama ditutup setelah satu event, lalu dibuka lagi dengan header `Last-Event-Id`. |
| `stop` | `POST /v1/sessions/{meeting_id}/stop` | Idempoten setelah fase terminal, jadi aman dipanggil dari sinyal `session_ended`. |
| `ingest` | `POST /v1/sessions/{meeting_id}/ingestions`, lalu `GET /v1/sessions/{meeting_id}/ingestion` | Mengajukan job `ingest` dan membaca statusnya sampai selesai atau batas poll habis. |
| `debrief` | `POST /v1/sessions/{meeting_id}/debriefs`, lalu `GET /v1/sessions/{meeting_id}/debrief` | Mengajukan debrief dengan `force: false` dan mencetak statusnya. |
| `report` | `GET /v1/sessions/{meeting_id}/report` | Mencetak laporan sesi dari journal. |

Setiap respons error memakai envelope `{"error": {"code": ..., "message": ...}}`. Contoh
memeriksa envelope itu di setiap request dan menghentikan diri dengan kode yang sama.

## Menjalankan tesnya

```sh
uv run --no-sync pytest tests/test_api_byof_example.py -q
```

Tes memuat modul contoh dari path file, memastikan tidak ada impor `fakeyou`, lalu menjalankan
alur penuh terhadap aplikasi ASGI asli. Worker dalam tes disimulasikan oleh harness: ketika
`join` diterima, harness menandai sesi selesai dan menulis journal. Itu satu-satunya simulasi.
